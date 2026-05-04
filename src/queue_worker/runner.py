"""Queue-worker runner.

State machine:
  chilling  → check usage at HH:00 (top of every hour);
              if usage_left >= 30% AND reset < 70min → burning
  burning   → process tasks one-by-one until reset time (burn_until);
              usage check after each task finish;
              after the currently-running task finishes past the deadline,
              transition back to chilling.

Reset anchor: a `next_reset_at` epoch is tracked across cycles. The canonical
re-anchor is the post_reset check at T+5min. Other checks (manual, hourly
fallback, pre_reset before post_reset has succeeded) can re-anchor inside a
±15min sanity band — wildly different proposals are logged and rejected, so
a single noisy fetch can't repaint truth. Burn decisions use min(fetched,
anchor) so a stale anchor cannot fire a burn that fresh evidence already
contradicts.

run_once mode bypasses the state machine and drains the queue once
(used by CLI --once and web "Run All (once)" button).
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from .config import PROJECT_ROOT
from .task import Task, augment_task, now_iso, parse_task
from .lock import recover_stale_locks, release_task_lock, RUNNING_DIR
from .queue_ops import (get_pending_tasks, resolve_run_order, move_task,
                        augment_stall, begin_task)
from .injector import cleanup_claude_md, BackupInfo
from .executor import execute_task, ExecuteResult
from .logger import TaskLogger
from .usage_check import BURN_USAGE_THRESHOLD_PCT, BURN_RESET_WINDOW_MIN


# ── State ────────────────────────────────────────────────────────────────────

STATE_CHILLING = 'chilling'
STATE_BURNING  = 'burning'

CheckKind = Literal[
    'hourly', 'pre_reset', 'post_reset', 'manual', 'cold_start', 'fallback',
    't_minus_60',
]
_ActionKind = Literal['reanchor', 'clamp', 'skip']


@dataclass
class RunnerState:
    state: str = STATE_CHILLING
    burn_until: Optional[float] = None      # epoch seconds
    last_check_at: Optional[float] = None   # epoch seconds
    last_check_pct: Optional[int] = None
    last_check_reset_minutes: Optional[int] = None
    last_check_status: Optional[str] = None
    last_check_error: Optional[str] = None

    # Reset-anchor model (sanity-banded around post_reset's canonical reading).
    # The done flags identify the *current* cycle implicitly — they reset to
    # False on every re-anchor and stay True until the next re-anchor.
    next_reset_at: Optional[float] = None              # epoch of next predicted reset
    post_reset_done_for_cycle: bool = False
    pre_reset_done_for_cycle: bool = False             # diagnostic
    t_minus_60_done_for_cycle: bool = False            # diagnostic
    last_anchor_kind: Optional[str] = None             # diagnostic
    last_anchor_at: Optional[float] = None             # diagnostic
    last_check_error_code: Optional[str] = None        # stable code for /api/status


_runner_state = RunnerState()
_state_lock = threading.Lock()
_usage_check_lock = threading.Lock()   # serializes usage checks (prevents concurrent
                                       # `claude -p "hi"` kicks and CSV row interleave)
_loop_wake = threading.Event()         # set on state change → wakes the runner loop

# Pending one-shot reset-anchored checks, guarded by _state_lock. Always read/
# written through the helpers below; never touch the list outside the lock.
_scheduled_checks: list[tuple[float, CheckKind]] = []

# Persistence — only the durable parts of RunnerState (anchor + cycle flags).
# burn_until, last_check_*, scheduled_checks are transient and intentionally
# excluded so a restart re-derives them on the next check.
STATE_FILE = PROJECT_ROOT / 'state' / 'runner_state.json'
_PERSISTED_FIELDS = (
    'next_reset_at',
    'post_reset_done_for_cycle', 'last_anchor_kind', 'last_anchor_at',
)

# Drop scheduled slots whose due time would already be in the past (or
# imminent) — happens on cold start when reset_minutes == 0, and after
# restarts where prior arm times are stale.
SCHED_DUE_FLOOR_S = 30

# Sanity band for re-anchoring. A proposed anchor more than this far from
# the existing anchor is logged but rejected (unless the existing anchor is
# itself "impossible" — see ANCHOR_*_S below).
ANCHOR_DRIFT_TOLERANCE_S = 15 * 60

# An anchor more than 30 min past its predicted reset, with no post_reset
# success recorded for that cycle, is treated as dead — clamp to None and
# the next successful check re-anchors as cold_start.
ANCHOR_STALE_GRACE_S = 30 * 60

# An anchor more than 6h ahead is impossible (Claude sessions are 5h) and
# means parse error or clock skew — clamp to None.
ANCHOR_MAX_FUTURE_S = 6 * 60 * 60

# A check that fires when now > anchor + this is internally relabelled
# 'fallback' (drift recovery). Same threshold as the stale grace.
FALLBACK_OVERDUE_S = 15 * 60

# When a reset-anchored slot (t_minus_60, pre_reset, post_reset) is within
# this many seconds of an HH:00 hourly tick, suppress the hourly. T-60 lands
# at HH:MM where MM matches the user's reset minute; on most cycles that's
# within minutes of an HH:00 tick. Firing both wastes a check and clutters
# the CSV.
SUPPRESS_HOURLY_WINDOW_S = 10 * 60


def get_runner_state() -> dict:
    """Thread-safe snapshot of runner state for API consumers."""
    with _state_lock:
        d = asdict(_runner_state)
    d['scheduled_checks'] = _list_scheduled()
    return d


def _snapshot() -> RunnerState:
    """Thread-safe consistent snapshot for the runner loop's own decisions."""
    with _state_lock:
        return dataclasses.replace(_runner_state)


def _update_state(**fields) -> None:
    """Plain field update + wake. Anchor mutations go through
    _apply_anchor_update_locked, which holds the lock across both."""
    with _state_lock:
        for k, v in fields.items():
            setattr(_runner_state, k, v)
    _loop_wake.set()


def wake_runner() -> None:
    """Public hook for external triggers (e.g., add_task) to wake the loop."""
    _loop_wake.set()


# ── Persistence ─────────────────────────────────────────────────────────────

_PERSISTED_TYPES = {
    'next_reset_at': (type(None), int, float),
    'post_reset_done_for_cycle': (bool,),
    'last_anchor_kind': (type(None), str),
    'last_anchor_at': (type(None), int, float),
}


def _load_persisted_state() -> None:
    """Restore the durable anchor fields from disk, if present.

    Failures (missing file, corrupt JSON, type mismatch) are swallowed and
    we start cold — the next check will re-anchor as cold_start. Type
    validation prevents a hand-edited '{"next_reset_at":"oops"}' from
    poisoning later arithmetic.
    """
    global _last_persisted_blob
    try:
        raw = STATE_FILE.read_text(encoding='utf-8')
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
    except FileNotFoundError:
        return
    except Exception:
        return
    with _state_lock:
        for k, allowed_types in _PERSISTED_TYPES.items():
            if k in data and isinstance(data[k], allowed_types):
                setattr(_runner_state, k, data[k])
        # Always re-arm if we have an anchor — _arm_scheduled_locked drops
        # past slots itself, so a restart at T+2 still picks up the canonical
        # T+5 post_reset slot even though the anchor itself is in the past.
        if _runner_state.next_reset_at is not None:
            _arm_scheduled_locked(_runner_state.next_reset_at)
        # Seed the hash gate so the first post-load persist (which would
        # write byte-identical content) is correctly skipped.
        payload = {k: getattr(_runner_state, k) for k in _PERSISTED_FIELDS}
        _last_persisted_blob = json.dumps(payload, indent=2)


_last_persisted_blob: Optional[str] = None


def _persist_state_locked() -> None:
    """Write durable fields to STATE_FILE. Caller MUST hold _state_lock.
    Skips the write when the serialized payload is byte-identical to the
    last successful write — re-anchors that land on the same value are
    common and don't justify a tmp+rename round trip every time."""
    global _last_persisted_blob
    payload = {k: getattr(_runner_state, k) for k in _PERSISTED_FIELDS}
    blob = json.dumps(payload, indent=2)
    if blob == _last_persisted_blob:
        return
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.json.tmp')
        tmp.write_text(blob, encoding='utf-8')
        tmp.replace(STATE_FILE)
        _last_persisted_blob = blob
    except Exception:
        pass  # persistence is best-effort; never crash on disk failure


# ── Scheduled-check queue (guarded by _state_lock) ──────────────────────────

def _arm_scheduled_locked(predicted_reset_at: float) -> None:
    """Replace pending entries with a fresh (T-60, T-10, T+5) trio.

    Slots due within SCHED_DUE_FLOOR_S of now are dropped — handles the
    cold-start edge where reset_minutes==0 would otherwise enqueue an
    already-past pre_reset. T-60 lands roughly at the same time the next
    HH:00 hourly tick would; the runner suppresses the hourly when an
    anchored slot is within SUPPRESS_HOURLY_WINDOW_S so we don't double-fire.
    """
    now = time.time()
    _scheduled_checks.clear()
    for offset, kind in (
        (-60 * 60, 't_minus_60'),
        (-10 * 60, 'pre_reset'),
        (+5 * 60, 'post_reset'),
    ):
        due = predicted_reset_at + offset
        if due > now + SCHED_DUE_FLOOR_S:
            _scheduled_checks.append((due, kind))  # type: ignore[arg-type]
    _scheduled_checks.sort(key=lambda e: e[0])


def _pop_due_scheduled(now: float) -> Optional[tuple[float, CheckKind]]:
    """Return the earliest entry with due ≤ now (and remove it), else None."""
    with _state_lock:
        if not _scheduled_checks:
            return None
        due, kind = _scheduled_checks[0]
        if due > now:
            return None
        _scheduled_checks.pop(0)
        return (due, kind)


def _peek_next_scheduled_due() -> Optional[float]:
    """Earliest pending due-epoch, or None."""
    with _state_lock:
        return _scheduled_checks[0][0] if _scheduled_checks else None


def _hourly_suppressed(target_at: float, now: float) -> bool:
    """True if the hourly tick at `target_at` is redundant.

    Two cases:
      1. A reset-anchored slot is still PENDING within ±SUPPRESS_HOURLY_WINDOW_S
         of `target_at` — it'll fire imminently anyway.
      2. A check JUST RAN within SUPPRESS_HOURLY_WINDOW_S (e.g., t_minus_60 at
         08:55, hourly tick at 09:00) — the anchored slot has already been
         popped from the queue, but `last_check_at` still proves we have fresh
         data; firing again is a duplicate.
    """
    with _state_lock:
        for due, _kind in _scheduled_checks:
            if abs(due - target_at) <= SUPPRESS_HOURLY_WINDOW_S:
                return True
        last_check = _runner_state.last_check_at
    if last_check is not None and (now - last_check) < SUPPRESS_HOURLY_WINDOW_S:
        return True
    return False


def _requeue(entry: tuple[float, CheckKind]) -> None:
    """Push an entry back after a contention skip (do_usage_check returned False).

    If the contended check already re-anchored, the queue may have a fresh
    entry of this kind for the new cycle. Don't double-up — drop the stale
    requeue in that case.
    """
    due, kind = entry
    new_due = max(time.time() + SCHED_DUE_FLOOR_S, due)
    with _state_lock:
        if any(k == kind for _, k in _scheduled_checks):
            return
        _scheduled_checks.append((new_due, kind))
        _scheduled_checks.sort(key=lambda e: e[0])


def _list_scheduled() -> list[dict]:
    with _state_lock:
        return [{'due_at': d, 'kind': k} for d, k in _scheduled_checks]


# ── Task execution ───────────────────────────────────────────────────────────

def run_and_finalize(task: Task, log: TaskLogger) -> ExecuteResult:
    """Execute a task and write result metadata. Shared by runner and web."""
    try:
        result = execute_task(task, log)
    except Exception as e:
        log.error(f'task {task.id}: executor crashed: {e}')
        result = ExecuteResult('failed', None, f'executor crash: {e}')

    augment_task(task.yaml_path, {
        'finished_at': now_iso(),
        'status': result.status,
        'stall_reason': result.stall_reason,
        'stall_detail': result.stall_detail,
        'duration_minutes': round(result.duration_minutes, 1),
        'tokens_used': result.tokens_used,
        'cost_usd': round(result.cost_usd, 6) if result.cost_usd is not None else None,
    })
    move_task(task, result.status)

    token_str = f' | tokens: {result.tokens_used}' if result.tokens_used else ''
    cost_str = f' | ${result.cost_usd:.4f}' if result.cost_usd is not None else ''
    log.task(task.id,
             f'-> {result.status} ({result.duration_minutes:.1f}min){token_str}{cost_str}'
             + (f' [{result.stall_reason}]' if result.stall_reason else ''))
    return result


def _process_one_task(log: TaskLogger,
                      execute_lock: Optional[threading.Lock],
                      deadline: Optional[float] = None) -> bool:
    """Pick the next ready task and run it. Returns True if a task executed.

    Acquires execute_lock BEFORE selecting the task so a concurrent web run
    cannot snipe the task we're about to begin. If `deadline` is given and
    has already passed by the time we hold the lock, returns False without
    starting a new task — used to honor the burn-window deadline.
    """
    acquired = False
    if execute_lock:
        execute_lock.acquire()
        acquired = True
    try:
        if deadline is not None and time.time() >= deadline:
            return False

        all_tasks = get_pending_tasks(log)
        if not all_tasks:
            return False
        tasks = resolve_run_order(all_tasks, log)
        if not tasks:
            log.info(f'{len(all_tasks)} task(s) pending but all blocked on dependencies')
            return False

        task_meta = tasks[0]
        try:
            task = begin_task(task_meta.id, log)
        except FileNotFoundError:
            # Task was removed/moved concurrently between selection and begin.
            return False
        run_and_finalize(task, log)
        return True
    finally:
        if acquired:
            execute_lock.release()


def _process_queue(log: TaskLogger,
                   execute_lock: Optional[threading.Lock] = None) -> None:
    """Drain all ready tasks in sequence (used by run_once)."""
    while _process_one_task(log, execute_lock):
        pass
    log.info('queue drained or all blocked')


# ── Usage check ──────────────────────────────────────────────────────────────

def _decide_anchor_action(snap: RunnerState, kind: CheckKind,
                          proposed: Optional[float], now: float) -> tuple[_ActionKind, CheckKind]:
    """Decide whether to re-anchor and which kind to record.

    Returns (action, effective_kind) where action is one of:
      'reanchor'  — overwrite next_reset_at with proposed
      'clamp'     — existing anchor is impossible/dead; clear and treat
                    proposed as cold_start
      'skip'      — leave anchor alone (proposal logged elsewhere)
    """
    if proposed is None:
        return ('skip', kind)

    # Promote 'hourly' to 'fallback' if we're past the canonical post_reset
    # and it never landed for this cycle. t_minus_60 also fires before reset,
    # so it gets the same fallback promotion when the anchor is overdue.
    effective = kind
    if (
        kind in ('hourly', 'pre_reset', 't_minus_60')
        and snap.next_reset_at is not None
        and now > snap.next_reset_at + FALLBACK_OVERDUE_S
        and not snap.post_reset_done_for_cycle
    ):
        effective = 'fallback'

    # Cold start: nothing to compare against.
    if snap.next_reset_at is None:
        return ('reanchor', 'cold_start')

    # Impossible-anchor clamp: existing anchor is stale or absurd.
    overdue = now - snap.next_reset_at
    if (
        (overdue > ANCHOR_STALE_GRACE_S and not snap.post_reset_done_for_cycle)
        or (snap.next_reset_at - now) > ANCHOR_MAX_FUTURE_S
    ):
        return ('clamp', effective)

    # Normal sanity-band gate.
    if effective in ('post_reset', 'manual', 'fallback'):
        if abs(proposed - snap.next_reset_at) <= ANCHOR_DRIFT_TOLERANCE_S:
            return ('reanchor', effective)
        # Out of band — log and skip. post_reset is canonical but a single
        # wildly-different post_reset is more likely a parse hiccup than a
        # real 60-min drift.
        return ('skip', effective)

    # pre_reset: only re-anchor if post_reset hasn't already nailed it.
    if effective == 'pre_reset' and not snap.post_reset_done_for_cycle:
        if abs(proposed - snap.next_reset_at) <= ANCHOR_DRIFT_TOLERANCE_S:
            return ('reanchor', 'pre_reset')

    # t_minus_60 is mid-cycle and never re-anchors. The done flag flip below
    # is purely diagnostic (UI shows it) and does not affect anchor logic.
    return ('skip', effective)


def _apply_anchor_update_locked(action: _ActionKind, effective_kind: CheckKind,
                                proposed: Optional[float], snap: RunnerState,
                                now: float, log: TaskLogger) -> None:
    """Apply the anchor decision under _state_lock. Caller MUST hold the lock.

    Returns None. Mutates _runner_state, _scheduled_checks, and persists when
    durable fields actually change. Designed to keep the do_usage_check
    critical section flat.
    """
    if action == 'reanchor' and proposed is not None:
        _set_anchor_locked(proposed, effective_kind, now, log)
        return

    if action == 'clamp':
        # _decide_anchor_action returns 'skip' when proposed is None, so a
        # 'clamp' action always carries a fresh proposed anchor — clear stale
        # state, then re-anchor as a cold start.
        _runner_state.next_reset_at = None
        _runner_state.post_reset_done_for_cycle = False
        _runner_state.pre_reset_done_for_cycle = False
        log.info('anchor: impossible/dead — clamping and re-anchoring as cold_start')
        _set_anchor_locked(proposed, 'cold_start', now, log)  # type: ignore[arg-type]
        return

    # action == 'skip' — done flags must NOT be flipped here. They mean
    # "this cycle's canonical re-anchor for that kind LANDED", and flipping
    # them on a rejected fetch would freeze recovery: hourly stops promoting
    # to fallback, pre_reset stops being allowed to re-anchor, and the
    # stale-anchor clamp stops firing.
    if proposed is not None and snap.next_reset_at is not None:
        delta_min = (proposed - snap.next_reset_at) / 60.0
        log.info(
            f'anchor: rejecting proposed={datetime.fromtimestamp(proposed).strftime("%H:%M:%S")} '
            f'(kind={effective_kind} delta={delta_min:.1f}min, outside ±15min band)'
        )


def _set_anchor_locked(proposed: float, effective_kind: CheckKind,
                       now: float, log: TaskLogger) -> None:
    """Re-anchor and re-arm. Caller MUST hold _state_lock."""
    _runner_state.next_reset_at = proposed
    _runner_state.post_reset_done_for_cycle = (effective_kind == 'post_reset')
    _runner_state.pre_reset_done_for_cycle = (effective_kind == 'pre_reset')
    _runner_state.t_minus_60_done_for_cycle = False  # new cycle resets the diagnostic flag
    _runner_state.last_anchor_kind = effective_kind
    _runner_state.last_anchor_at = now
    _arm_scheduled_locked(proposed)
    _persist_state_locked()
    log.info(
        f'anchor: kind={effective_kind} next_reset_at='
        f'{datetime.fromtimestamp(proposed).strftime("%H:%M:%S")}'
    )


def _effective_burn_minutes(fetch_min: Optional[int],
                            anchor_at: Optional[float], now: float) -> Optional[float]:
    """Return min(fetch, anchor) in minutes — whichever is shorter wins.

    Used to decide burn so a stale anchor cannot fire a burn that fresh
    evidence already contradicts (and vice-versa). A past-due anchor is
    treated as expired (skipped, not pinned to 0) — otherwise a stale
    anchor would force effective=0 and trigger a 0-second burn window
    that flickers state without doing useful work.
    """
    candidates: list[float] = []
    if fetch_min is not None:
        candidates.append(float(fetch_min))
    if anchor_at is not None and anchor_at > now:
        candidates.append((anchor_at - now) / 60.0)
    return min(candidates) if candidates else None


def do_usage_check(log: TaskLogger, kind: CheckKind = 'hourly') -> bool:
    """Run a usage check and update runner state.

    Returns True if the check ran (regardless of success/error), False if it
    was skipped because another check is already in progress.

    Honors:
    1. Only one usage check runs at a time. Prevents concurrent kicks and
       interleaved CSV writes. Concurrent calls are silently dropped — caller
       can requeue if the check came from the scheduled queue.
    2. A check during an active burn window does NOT downgrade state — the
       check still records the latest pct/reset and may re-anchor, but
       burn_until is left alone so the runner finishes the burn naturally.
    3. Transient errors (network, parse failure, Cloudflare block) record the
       error but leave the state machine alone — never clobber an active burn.
    4. Re-anchor decisions are sanity-banded (see _decide_anchor_action).
    """
    if not _usage_check_lock.acquire(blocking=False):
        log.info(f'usage check ({kind}) already in progress, skipping')
        return False

    try:
        log.info(f'checking Claude usage (kind={kind})...')
        try:
            from .usage_check import check_usage_once
            result = check_usage_once(log_fn=log.info)
        except Exception as e:
            from .usage_check_http import redact
            err = redact(str(e))
            log.error(f'usage check failed: {err}')
            _update_state(
                last_check_at=time.time(),
                last_check_error=err,
                last_check_status='ERROR',
                last_check_error_code='dispatcher_crash',
            )
            return True

        finish = result.finished_at or time.time()
        now = time.time()
        snap = _snapshot()
        proposed = (
            finish + result.reset_minutes * 60
            if result.reset_minutes is not None else None
        )
        action, effective_kind = _decide_anchor_action(snap, kind, proposed, now)

        # Build the field updates to apply atomically below.
        # error_code and backend come from the dispatcher (usage_check.py); the
        # HTTP backend module already redacts session keys + emails before they
        # reach UsageCheckResult.error.
        fields: dict = {
            'last_check_at': now,
            'last_check_pct': result.pct,
            'last_check_reset_minutes': result.reset_minutes,
            'last_check_status': result.status,
            'last_check_error': result.error,
            'last_check_error_code': result.error_code,
        }
        if kind == 't_minus_60':
            fields['t_minus_60_done_for_cycle'] = True

        # Burn decision uses min(fetch, anchor) — the safer of the two.
        anchor_for_burn = (
            None if action == 'clamp' else snap.next_reset_at
        )
        eff_min = _effective_burn_minutes(result.reset_minutes, anchor_for_burn, now)
        usage_left = (100 - result.pct) if result.pct is not None else None

        in_active_burn = (
            snap.state == STATE_BURNING and snap.burn_until and now < snap.burn_until
        )
        if in_active_burn:
            log.info(
                f'check during burn: pct={result.pct}% — preserving burn until '
                f'{datetime.fromtimestamp(snap.burn_until).strftime("%H:%M:%S")}'  # type: ignore[arg-type]
            )
        elif (eff_min is not None and usage_left is not None
              and usage_left >= BURN_USAGE_THRESHOLD_PCT
              and eff_min < BURN_RESET_WINDOW_MIN):
            burn_until = now + eff_min * 60
            log.info(
                f'NEED TO BURN TOKEN! pct={result.pct}% effective_reset={eff_min:.1f}min '
                f'(fetch={result.reset_minutes}min, anchor={"-" if anchor_for_burn is None else f"{(anchor_for_burn-now)/60:.1f}min"}) '
                f'burn until {datetime.fromtimestamp(burn_until).strftime("%H:%M:%S")}'
            )
            fields.update(state=STATE_BURNING, burn_until=burn_until)
        else:
            log.info(
                f'chilling: pct={result.pct}% status={result.status} '
                f'effective_reset={eff_min}'
            )
            fields.update(state=STATE_CHILLING, burn_until=None)

        # Apply state, anchor, and scheduled re-arm in one critical section.
        with _state_lock:
            for k_, v_ in fields.items():
                setattr(_runner_state, k_, v_)
            _apply_anchor_update_locked(action, effective_kind, proposed, snap, now, log)

        _loop_wake.set()
        return True
    finally:
        _usage_check_lock.release()


# ── Clock-aligned scheduling ────────────────────────────────────────────────

# Hourly base cadence: every check fires at HH:00. The reset-anchored T-10
# pre_reset and T+5 post_reset checks are queued via _scheduled_checks and
# fire whenever they come due, regardless of state (chilling or burning).
USAGE_CHECK_MINUTE = 0


def _next_usage_check_at(after: float) -> float:
    """Return epoch of the next HH:USAGE_CHECK_MINUTE:00 strictly after `after`."""
    dt = datetime.fromtimestamp(after)
    candidate = dt.replace(minute=USAGE_CHECK_MINUTE, second=0, microsecond=0)
    if candidate <= dt:
        candidate += timedelta(hours=1)
    return candidate.timestamp()


# ── Main entry point ─────────────────────────────────────────────────────────

def start_runner(log: TaskLogger,
                 run_once: bool = False,
                 stop_event: Optional[threading.Event] = None,
                 execute_lock: Optional[threading.Lock] = None,
                 tick_seconds: int = 30) -> None:
    """
    Main runner.

    run_once=True: drain the queue once and exit (bypasses state machine).
    Otherwise: state machine — chilling by default, burning when usage check says so.
    stop_event: if set, the loop exits gracefully at the next tick.
    execute_lock: shared lock that serializes task execution with web-triggered runs.
    tick_seconds: how often the burn loop wakes to check the burn deadline / run
        the next ready task. The default is fine for production; tests override it.
    """
    log.info('queue-worker started')
    _load_persisted_state()
    _recover_stale(log)
    from .queue_ops import reconcile_filenames
    try:
        reconcile_filenames(log)
    except Exception as e:
        # Best-effort — startup must continue even if one malformed YAML
        # would cause the helper to raise. The runner can still pick up
        # well-formed pending tasks.
        log.error(f'queue integrity check failed: {e}')

    if run_once:
        log.info('run-once: processing queue')
        _process_queue(log, execute_lock)
        log.info('run-once: complete')
        return

    # Initial usage check so we don't wait until the next HH:00 before doing anything
    do_usage_check(log, kind='hourly')

    while not (stop_event and stop_event.is_set()):
        try:
            _loop_wake.clear()
            now = time.time()
            snap = _snapshot()

            # First: drain any reset-anchored scheduled checks that are due.
            # Runs in BOTH chilling and burning — the canonical post_reset
            # anchor must not be skipped just because a long task is in flight.
            entry = _pop_due_scheduled(now)
            if entry:
                _due, sched_kind = entry
                ran = do_usage_check(log, kind=sched_kind)
                if not ran:
                    _requeue(entry)
                continue

            # Hourly base cadence — only fired from chilling. We never let
            # an hourly check pre-empt task execution mid-burn.
            #
            # Suppress when a reset-anchored slot (t_minus_60, pre_reset,
            # post_reset) is within ±SUPPRESS_HOURLY_WINDOW_S of the hourly
            # tick: the anchored slot will fire regardless and a duplicate
            # check minutes apart wastes a request and clutters the CSV.
            if snap.state == STATE_CHILLING:
                last_check = snap.last_check_at or 0
                next_check_at = _next_usage_check_at(last_check)
                if now >= next_check_at:
                    if _hourly_suppressed(next_check_at, now):
                        log.info(
                            f'hourly tick suppressed (anchored slot within '
                            f'±{SUPPRESS_HOURLY_WINDOW_S//60}min)'
                        )
                        # Fake a "completed" hourly so the next tick pushes
                        # forward instead of immediately re-firing.
                        _update_state(last_check_at=now)
                        continue
                    if do_usage_check(log, kind='hourly'):
                        continue

            # Burning: process tasks until the deadline
            if snap.state == STATE_BURNING:
                burn_until = snap.burn_until or 0
                if now >= burn_until:
                    log.info('burn window closed — returning to chilling')
                    _update_state(state=STATE_CHILLING, burn_until=None)
                    continue

                if _process_one_task(log, execute_lock, deadline=burn_until):
                    do_usage_check(log, kind='hourly')
                    continue
                # Queue empty or blocked — fall through to sleep, stay burning.

            # Sleep until the next interesting moment, or until something wakes us.
            sleep_for = _next_wakeup_seconds(snap, now, tick_seconds)
            if _loop_wake.wait(timeout=sleep_for):
                continue
            if stop_event and stop_event.is_set():
                return
        except Exception as e:
            log.error(f'runner loop error (continuing): {e}')
            time.sleep(10)


def _next_wakeup_seconds(snap: RunnerState, now: float,
                         tick_seconds: int) -> float:
    """How long to sleep before the next loop iteration.

    Chilling: min(next HH:00, soonest scheduled-check due, 600s tick cap).
    Burning: min(soonest scheduled-check due, tick_seconds).
    """
    sched_due = _peek_next_scheduled_due()
    if snap.state == STATE_CHILLING:
        last_check = snap.last_check_at or 0
        candidates = [_next_usage_check_at(last_check) - now, 600.0]
        if sched_due is not None:
            candidates.append(sched_due - now)
        return max(1.0, min(candidates))
    # Burning
    candidates = [float(tick_seconds)]
    if sched_due is not None:
        candidates.append(sched_due - now)
    return max(1.0, min(candidates))


# ── Crash recovery ───────────────────────────────────────────────────────────

def _recover_stale(log: TaskLogger) -> None:
    stale = recover_stale_locks()
    if stale:
        log.warn(f'{len(stale)} stale lock(s) found from previous crash')

    for s in stale:
        log.warn(f'  stale: {s.task_id} (pid {s.pid} dead)')

        if s.claude_md_written and s.project_dir:
            backup = BackupInfo(
                had_original=s.backed_up_original,
                backup_path=(Path(s.project_dir) / f'CLAUDE.md.queue-worker-bak-{s.pid}')
                             if s.backed_up_original else None,
            )
            try:
                cleanup_claude_md(s.project_dir, backup)
                log.info(f'  cleaned up CLAUDE.md for {s.task_id}')
            except Exception as e:
                log.error(f'  failed to clean CLAUDE.md for {s.task_id}: {e}')

        running_yaml = RUNNING_DIR / f'{s.task_id}.yaml'
        if running_yaml.exists():
            try:
                task = parse_task(str(running_yaml))
                augment_stall(task, 'crash', f'PID {s.pid} was dead on startup')
                move_task(task, 'unfinished')
            except Exception as e:
                log.error(f'  could not recover task yaml for {s.task_id}: {e}')

        release_task_lock(s.lock_path)

    if RUNNING_DIR and RUNNING_DIR.exists():
        stale_ids = {s.task_id for s in stale}
        for yaml_file in RUNNING_DIR.glob('*.yaml'):
            task_id = yaml_file.stem
            if task_id in stale_ids:
                continue
            if not (RUNNING_DIR / f'{task_id}.lock').exists():
                log.warn(f'  orphaned task in running/ (no lock): {task_id}')
                try:
                    task = parse_task(str(yaml_file))
                    augment_stall(task, 'crash', 'No lock file found on startup')
                    move_task(task, 'unfinished')
                except Exception as e:
                    log.error(f'  could not recover orphaned task {task_id}: {e}')
