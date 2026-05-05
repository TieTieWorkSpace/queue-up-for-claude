import json
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

# Serializes any operation that mutates a pending task's filesystem location
# OR its YAML contents. Held by:
#   - begin_task (pending → running rename)
#   - api_edit_task (pending YAML rewrite)
#   - api_unfinish (pending → unfinished rename)
# Without this, the runner can move pending/X.yaml between when a web handler
# reads it and when it writes back, leaving duplicate task copies.
PENDING_MUTATION_LOCK = threading.Lock()

from .task import Task, parse_task, augment_task, now_iso, make_task_id, expand_path
from .logger import TaskLogger

QUEUE_STATUSES = ['pending', 'running', 'done', 'unfinished', 'failed']

# Recognized values for Task.run_policy.
RUN_POLICY_THIS_SESSION = 'this_session'
RUN_POLICY_NEXT_SESSION = 'next_session'
RUN_POLICY_TONIGHT = 'tonight'
VALID_RUN_POLICIES = {
    RUN_POLICY_THIS_SESSION,
    RUN_POLICY_NEXT_SESSION,
    RUN_POLICY_TONIGHT,
}

# "Tonight" means the next 02:00 strictly in the future, in the machine's
# local timezone. At 03:00 → tomorrow 02:00. At 01:00 → today 02:00.
TONIGHT_HOUR_LOCAL = 2

# Claude usage sessions are a fixed 5-hour window. Used as a fallback when no
# usage check has populated runner state yet.
SESSION_LENGTH_MINUTES = 300

# Buffer added past a computed session-end so we don't fire literally at the
# instant of reset (the new session needs a moment to actually open).
NEXT_SESSION_BUFFER_SECONDS = 60


def compute_eligible_at(policy: Optional[str], runner_state: dict,
                         now: Optional[datetime] = None) -> Optional[str]:
    """Return ISO8601 UTC eligible_at for a run_policy, or None if always eligible.

    `now` is injectable for tests; defaults to current UTC time.
    """
    if not policy or policy == RUN_POLICY_THIS_SESSION:
        return None
    now_utc = now or datetime.now(timezone.utc)
    if policy == RUN_POLICY_NEXT_SESSION:
        last_check_at = runner_state.get('last_check_at')
        reset_minutes = runner_state.get('last_check_reset_minutes')
        # If the most recent check errored, `last_check_at` is fresh but the
        # pct/reset fields retain stale values from an earlier success. Using
        # them would schedule the task minutes from now instead of ~5h out.
        # Fall back to now+5h whenever we can't trust the cached reset.
        check_errored = bool(runner_state.get('last_check_error'))
        if (last_check_at and reset_minutes is not None and not check_errored):
            target = (datetime.fromtimestamp(last_check_at, tz=timezone.utc)
                      + timedelta(minutes=reset_minutes,
                                  seconds=NEXT_SESSION_BUFFER_SECONDS))
            # If the cached check is so stale the computed reset is already in
            # the past, the user still asked to skip a session — push out a
            # full window from now rather than running immediately.
            if target <= now_utc:
                target = now_utc + timedelta(minutes=SESSION_LENGTH_MINUTES)
        else:
            target = now_utc + timedelta(minutes=SESSION_LENGTH_MINUTES)
        return target.isoformat(timespec='seconds')
    if policy == RUN_POLICY_TONIGHT:
        # "Next 02:00 strictly in the future" in the machine's local timezone.
        # Use .timestamp() on naive datetimes so POSIX DST rules apply — a
        # naive/fixed-offset approach would silently drift 1h around spring/
        # fall transitions.
        now_ts = now_utc.timestamp()
        local_naive = datetime.fromtimestamp(now_ts)
        target_date = local_naive.date()
        target_local = datetime.combine(
            target_date, time(hour=TONIGHT_HOUR_LOCAL))
        # Loop in case tomorrow's 02:00 still isn't past now (won't happen in
        # practice — one iteration suffices — but cheap insurance).
        while target_local.timestamp() <= now_ts:
            target_date = date.fromordinal(target_date.toordinal() + 1)
            target_local = datetime.combine(
                target_date, time(hour=TONIGHT_HOUR_LOCAL))
        target_utc = datetime.fromtimestamp(
            target_local.timestamp(), tz=timezone.utc)
        return target_utc.isoformat(timespec='seconds')
    return None

# Set by _bootstrap() at startup
QUEUE_DIR: Path = None  # type: ignore[assignment]


def get_pending_tasks(log: TaskLogger) -> list[Task]:
    """
    Read all .yaml files from queue/pending/.
    Parse each, skip malformed (log warning).
    Returns all parseable tasks (unsorted — use resolve_run_order to sort).
    """
    pending_dir = QUEUE_DIR / 'pending'
    pending_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for yaml_file in sorted(pending_dir.glob('*.yaml')):
        try:
            tasks.append(parse_task(str(yaml_file)))
        except Exception as e:
            log.warn(f"skipping {yaml_file.name}: {e}")
    return tasks


def get_done_ids() -> set[str]:
    """Scan queue/done/ filenames. Task ID == filename stem (no YAML parsing needed)."""
    done_dir = QUEUE_DIR / 'done'
    if not done_dir.exists():
        return set()
    return {f.stem for f in done_dir.glob('*.yaml')}


def resolve_run_order(tasks: list[Task], log: TaskLogger) -> list[Task]:
    """
    Sort tasks by priority and dependency graph.

    Rules:
    1. A task is "ready" when all its depends_on IDs are in done/ or
       are earlier in the returned list (i.e., will be done by the time we reach it).
    2. Among ready tasks, lower priority number runs first (1=critical > 5=idle).
    3. Ties broken by created timestamp (oldest first).
    4. Tasks with unmet dependencies that can't be resolved this cycle are skipped.
    5. Tasks with eligible_at in the future are gated out (returned to the queue
       silently — they'll be picked up on a later cycle once the gate releases).
    """
    done_ids = get_done_ids()

    # Drop tasks gated by run_policy/eligible_at. ISO8601 UTC strings sort
    # lexicographically; both the stored field and now_iso() use timezone.utc.
    now_str = now_iso()
    eligible_tasks: list[Task] = []
    for t in tasks:
        if t.eligible_at and t.eligible_at > now_str:
            log.info(f'task {t.id}: gated until {t.eligible_at} '
                     f'({t.run_policy or "scheduled"}), skipping')
            continue
        eligible_tasks.append(t)

    ordered: list[Task] = []
    placed: set[str] = set(done_ids)
    # Sort candidates by priority then created BEFORE resolving deps.
    # Each iteration picks the highest-priority ready task, preserving
    # the invariant that dependencies always come before dependents.
    remaining = sorted(eligible_tasks, key=lambda t: (t.priority, t.created))

    changed = True
    while remaining and changed:
        changed = False
        still_remaining = []
        for task in remaining:
            if all(dep in placed for dep in task.depends_on):
                ordered.append(task)
                placed.add(task.id)
                changed = True
            else:
                still_remaining.append(task)
        remaining = still_remaining

    for task in remaining:
        unmet = [d for d in task.depends_on if d not in placed]
        log.info(f'task {task.id}: blocked on dependencies {unmet}, skipping')

    return ordered


def move_task(task: Task, to: str) -> Path:
    """
    Move task yaml from its current location to queue/<to>/<filename>.
    Updates task.yaml_path in-place.
    Writes status field to the yaml.
    Returns new path.
    """
    src = Path(task.yaml_path)
    dest_dir = QUEUE_DIR / to
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src.rename(dest)
    task.yaml_path = str(dest)
    augment_task(str(dest), {'status': to})
    return dest


def augment_stall(task: Task, reason: str, detail: str,
                  checkpoint_content: Optional[dict] = None) -> None:
    """
    Add stall metadata to the task yaml.
    Optionally embed checkpoint content so it's visible in-file.
    """
    fields: dict = {
        'stall_reason': reason,
        'stall_detail': detail,
        'stalled_at': now_iso(),
    }
    if checkpoint_content:
        fields['checkpoint_content'] = checkpoint_content
    augment_task(task.yaml_path, fields)


def append_episodic_entry(project_dir: str, entry: dict) -> None:
    """Append a JSONL event to .agent/log/tasks.jsonl (runtime contract; gitignored).

    Schema: {ts: ISO-8601, task_id: str, status: str, stall_reason: str|None,
             duration_s: float, ...} — see executor._write_episodic.
    """
    tasks_log = Path(project_dir) / '.agent' / 'log' / 'tasks.jsonl'
    tasks_log.parent.mkdir(parents=True, exist_ok=True)
    with open(tasks_log, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def find_task_yaml(task_id: str) -> Optional[Path]:
    """Find a task YAML by ID across all queue directories. Returns None if not found."""
    for sub in QUEUE_STATUSES:
        p = QUEUE_DIR / sub / f'{task_id}.yaml'
        if p.exists():
            return p
    return None


def list_tasks_in(status: str) -> list[dict]:
    """Read all task YAMLs in a queue subdirectory as raw dicts."""
    d = QUEUE_DIR / status
    if not d.exists():
        return []
    rows = []
    for f in sorted(d.glob('*.yaml')):
        try:
            with open(f) as fh:
                data = yaml.safe_load(fh) or {}
            data['_status'] = status
            rows.append(data)
        except Exception:
            rows.append({'id': f.stem, '_status': status, '_error': True})
    return rows


def list_tasks(status: Optional[str] = None) -> list[dict]:
    """Read task rows across all queues, or just one bucket if `status` is set.
    Each row has a `_status` field set to its bucket name."""
    statuses = [status] if status else QUEUE_STATUSES
    rows: list[dict] = []
    for s in statuses:
        rows.extend(list_tasks_in(s))
    return rows


def get_queue_counts() -> dict[str, int]:
    """Return a {status: file_count} map across all queue subdirectories."""
    counts = {}
    for s in QUEUE_STATUSES:
        d = QUEUE_DIR / s
        counts[s] = sum(1 for _ in d.glob('*.yaml')) if d.exists() else 0
    return counts


_SAFE_TASK_ID_RE = __import__('re').compile(r'^[A-Za-z0-9_-]+$')


def _canonical_in_any_queue(real_id: str) -> Optional[str]:
    """Return the queue subdir name where `<real_id>.yaml` exists, else None."""
    for sub in QUEUE_STATUSES:
        if (QUEUE_DIR / sub / f'{real_id}.yaml').exists():
            return sub
    return None


def _unique_quarantine_path(failed_dir: Path, base_stem: str) -> Path:
    """Pick a non-existing failed/<base>[-N].yaml so collision-prone
    `safe_stem` values (different conflicted suffixes that map to the same
    sanitized stem) don't clobber each other."""
    candidate = failed_dir / f'{base_stem}.yaml'
    n = 1
    while candidate.exists():
        candidate = failed_dir / f'{base_stem}-{n}.yaml'
        n += 1
    return candidate


def reconcile_filenames(log: TaskLogger) -> int:
    """Recover or quarantine YAMLs whose filename doesn't match their `id`.

    Sync tools (iCloud, Dropbox) rename conflicting files to e.g.
    `X [conflicted].yaml`, after which `find_task_yaml(X)` returns None and
    every web action 404s. Manual `cp X.yaml Xcopy.yaml` has the same
    effect. We catch both at startup:
      - filename stem == yaml id → ok, skip.
      - id matches the safe regex AND no canonical `<id>.yaml` exists in
        ANY queue subdir → rename in place (recovery).
      - any other case (canonical exists somewhere, id has unsafe chars,
        no id field, parse failure) → quarantine to failed/ with a
        collision-safe filename.
    Returns the number of files acted on.
    """
    import re as _re
    acted = 0
    for sub in QUEUE_STATUSES:
        d = QUEUE_DIR / sub
        if not d.exists():
            continue
        for f in sorted(d.glob('*.yaml')):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception as e:
                log.warn(f'queue integrity: cannot parse {f.name}: {e}')
                continue
            real_id = data.get('id')
            if not isinstance(real_id, str) or not real_id:
                log.warn(f'queue integrity: {f.name} has no id field — skipping')
                continue
            if not _SAFE_TASK_ID_RE.match(real_id):
                # Reject ids with `/`, `..`, NUL, or other path-unsafe chars.
                # We can't trust them as filename or directory components.
                log.warn(f'queue integrity: {f.name} has unsafe id {real_id!r} — '
                         'skipping (no rename or quarantine attempted)')
                continue
            if f.stem == real_id:
                continue
            existing_in = _canonical_in_any_queue(real_id)
            if existing_in is None:
                # Safe to recover in place — no canonical anywhere in the queue.
                canonical = d / f'{real_id}.yaml'
                f.rename(canonical)
                log.warn(f'queue integrity: renamed {sub}/{f.name} → {real_id}.yaml')
                acted += 1
                continue
            # Canonical exists somewhere — even if it's in a different queue
            # subdir (e.g. done/), the conflicted copy must NOT win the
            # find_task_yaml lookup ordering. Quarantine instead.
            failed_dir = QUEUE_DIR / 'failed'
            failed_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = _re.sub(r'[^A-Za-z0-9_-]', '_', f.stem)
            base_stem = f'{real_id}__corrupt-{safe_stem}'
            quarantined = _unique_quarantine_path(failed_dir, base_stem)
            data.update({
                'id': quarantined.stem,
                'status': 'failed',
                'finished_at': now_iso(),
                'stall_reason': 'corrupt_filename',
                'stall_detail': (
                    f'Filename {f.name!r} did not match id {real_id!r}, and a '
                    f'canonical {real_id}.yaml already existed in {existing_in}/. '
                    'Likely a sync-tool conflict or hand-copy. Quarantined here '
                    'so the runner does not pick it up; the canonical file is '
                    'untouched.'
                ),
            })
            with open(quarantined, 'w') as out:
                yaml.dump(data, out, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            f.unlink()
            log.warn(f'queue integrity: quarantined duplicate {sub}/{f.name} → '
                     f'failed/{quarantined.name} (canonical lives in {existing_in}/)')
            acted += 1
    if acted:
        log.info(f'queue integrity: reconciled {acted} mismatched filename(s)')
    return acted


def create_task(dir: str, prompt: str, level: str = 'craftsman',
                priority: int = 3, dry_run: bool = False,
                depends_on: list[str] | None = None,
                tags: list[str] | None = None,
                max_minutes: int | None = None,
                run_policy: str | None = None,
                session_id: str | None = None) -> tuple[str, Path]:
    """Create a task YAML in pending/. Returns (task_id, file_path).

    run_policy controls when the task becomes eligible for selection:
      None / 'this_session' → eligible immediately (default).
      'next_session'        → skip the upcoming burn; eligible after the
                              current 5-hour session ends.
      'tonight'             → eligible at the next 02:00 local time.
    eligible_at is computed once at create time from current runner state and
    persisted, so restarts and clock drift don't lose schedule.
    """
    if run_policy and run_policy not in VALID_RUN_POLICIES:
        raise ValueError(f'invalid run_policy: {run_policy!r} '
                         f'(expected one of {sorted(VALID_RUN_POLICIES)})')
    abs_dir = expand_path(dir)
    task_id = make_task_id(abs_dir)
    data: dict = {
        'id': task_id, 'created': now_iso(), 'dir': abs_dir,
        'prompt': prompt, 'level': level, 'priority': priority,
        'dry_run': dry_run, 'depends_on': depends_on or [],
        'tags': tags or [],
    }
    if session_id:
        data['session_id'] = session_id
    if max_minutes:
        data['budget'] = {'max_minutes': max_minutes}
    if run_policy and run_policy != RUN_POLICY_THIS_SESSION:
        # Lazy import: runner imports queue_ops at module load.
        from .runner import get_runner_state
        eligible_at = compute_eligible_at(run_policy, get_runner_state())
        data['run_policy'] = run_policy
        if eligible_at:
            data['eligible_at'] = eligible_at
    elif run_policy == RUN_POLICY_THIS_SESSION:
        # Persist the explicit choice so the UI can render it back, even
        # though no gate is applied.
        data['run_policy'] = run_policy
    pending_dir = QUEUE_DIR / 'pending'
    pending_dir.mkdir(parents=True, exist_ok=True)
    out_path = pending_dir / f'{task_id}.yaml'
    with open(out_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)
    return task_id, out_path


def begin_task(task_id: str, log: TaskLogger) -> Task:
    """Move pending -> running. Returns the parsed Task. Raises FileNotFoundError.
    Serializes against concurrent web edits via PENDING_MUTATION_LOCK so a
    PATCH that races with task start can't recreate the YAML in pending/."""
    with PENDING_MUTATION_LOCK:
        src = QUEUE_DIR / 'pending' / f'{task_id}.yaml'
        if not src.exists():
            raise FileNotFoundError(f'Task {task_id} not found in pending/')
        task = parse_task(str(src))
        augment_task(task.yaml_path, {'started_at': now_iso()})
        move_task(task, 'running')
    log.task(task_id, f'started | p{task.priority} | level={task.level} | dir={task.dir}')
    return task


def complete_task(task_id: str, log: TaskLogger) -> None:
    """Move running -> done. Writes episodic entry."""
    src = QUEUE_DIR / 'running' / f'{task_id}.yaml'
    if not src.exists():
        raise FileNotFoundError(f'Task {task_id} not found in running/')
    task = parse_task(str(src))
    augment_task(task.yaml_path, {'finished_at': now_iso(), 'status': 'done'})
    move_task(task, 'done')
    append_episodic_entry(task.resolved_dir, {
        'ts': now_iso(), 'task_id': task.id, 'status': 'done', 'level': task.level,
    })
    log.task(task_id, 'done')


def fail_task(task_id: str, detail: str, log: TaskLogger) -> None:
    """Move running -> failed. Writes episodic entry."""
    src = QUEUE_DIR / 'running' / f'{task_id}.yaml'
    if not src.exists():
        raise FileNotFoundError(f'Task {task_id} not found in running/')
    task = parse_task(str(src))
    augment_task(task.yaml_path, {
        'finished_at': now_iso(), 'status': 'failed',
        'stall_detail': detail or 'Failed',
    })
    move_task(task, 'failed')
    append_episodic_entry(task.resolved_dir, {
        'ts': now_iso(), 'task_id': task.id, 'status': 'failed',
        'stall_detail': detail, 'level': task.level,
    })
    log.task(task_id, f'failed: {detail}')


def stall_task(task_id: str, reason: str, detail: str, log: TaskLogger) -> None:
    """Move running -> unfinished. Writes episodic entry."""
    src = QUEUE_DIR / 'running' / f'{task_id}.yaml'
    if not src.exists():
        raise FileNotFoundError(f'Task {task_id} not found in running/')
    task = parse_task(str(src))
    augment_stall(task, reason, detail or reason)
    augment_task(task.yaml_path, {'finished_at': now_iso()})
    move_task(task, 'unfinished')
    append_episodic_entry(task.resolved_dir, {
        'ts': now_iso(), 'task_id': task.id, 'status': 'unfinished',
        'stall_reason': reason, 'level': task.level,
    })
    log.task(task_id, f'stalled: [{reason}] {detail}')


_RETRY_CLEAR_FIELDS = [
    'started_at', 'finished_at', 'status', 'stall_reason', 'stall_detail',
    'stalled_at', 'checkpoint_file', 'checkpoint_content',
    'duration_minutes', 'tokens_used',
]


def retry_task(task_id: str, log: TaskLogger) -> bool:
    """Move unfinished/failed -> pending. Returns True if checkpoint_answer preserved."""
    src = QUEUE_DIR / 'unfinished' / f'{task_id}.yaml'
    if not src.exists():
        src = QUEUE_DIR / 'failed' / f'{task_id}.yaml'
    if not src.exists():
        raise FileNotFoundError(f'Task {task_id} not found in unfinished/ or failed/')
    with open(src) as f:
        data = yaml.safe_load(f) or {}
    for field in _RETRY_CLEAR_FIELDS:
        data.pop(field, None)
    dest = QUEUE_DIR / 'pending' / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'w') as f:
        yaml.dump(data, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)
    src.unlink()
    log.task(task_id, 'retried')
    return bool(data.get('checkpoint_answer'))


def remove_task(task_id: str, log: TaskLogger) -> str:
    """Remove a task from any queue. Returns the queue it was in. Raises FileNotFoundError."""
    path = find_task_yaml(task_id)
    if not path:
        raise FileNotFoundError(f'Task {task_id} not found')
    queue_name = path.parent.name
    path.unlink()
    log.task(task_id, f'removed from {queue_name}/')
    return queue_name
