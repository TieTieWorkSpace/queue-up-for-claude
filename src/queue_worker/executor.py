import json
import os
import signal
import time
import threading
import subprocess
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Optional

from .task import Task, augment_task, now_iso
from .injector import build_claude_md, inject_claude_md, cleanup_claude_md, BackupInfo
from .lock import acquire_task_lock, update_task_lock, release_task_lock
from .queue_ops import append_episodic_entry, augment_stall
from .logger import TaskLogger


@dataclass
class ExecuteResult:
    status: str                        # 'done' | 'unfinished' | 'failed'
    stall_reason: Optional[str] = None
    stall_detail: Optional[str] = None
    duration_minutes: float = 0.0
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None


# ── stream-json pretty-printer ──────────────────────────────────────────────
#
# `claude -p --output-format stream-json --verbose` emits one JSON event per
# stdout line. The parser below renders each event as one or more human-readable
# log lines and tracks token + cost totals so a SIGKILL/timeout that prevents
# the final `result` event still leaves us with a usage estimate.

_TRUNC = '…'


def _trunc(s: str, max_chars: int) -> str:
    """Single-line, max-len truncation. Newlines are squashed so the result
    fits in one [task:<id>] log entry (the dashboard greps by that tag)."""
    s = s.replace('\n', ' ').replace('\r', ' ')
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + _TRUNC


def _short_json(obj: Any, max_chars: int = 120) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = repr(obj)
    return _trunc(s, max_chars)


def _first_nonblank_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ''


def _format_tool_input(name: str, inp: Any) -> str:
    """One-line human summary of a tool's input dict, per tool."""
    if not isinstance(inp, dict):
        return _short_json(inp)
    if name == 'Bash':
        return _trunc(str(inp.get('command', '')), 200)
    if name in ('Read', 'Write'):
        path = str(inp.get('file_path', ''))
        extras = []
        if 'offset' in inp:
            extras.append(f"offset={inp['offset']}")
        if 'limit' in inp:
            extras.append(f"limit={inp['limit']}")
        return path + (f' [{", ".join(extras)}]' if extras else '')
    if name == 'NotebookEdit':
        return str(inp.get('notebook_path', ''))
    if name == 'Edit':
        path = str(inp.get('file_path', ''))
        return path + (' (all)' if inp.get('replace_all') else '')
    if name in ('Grep', 'Glob'):
        pat = str(inp.get('pattern', ''))
        path = inp.get('path') or inp.get('glob') or ''
        return pat + (f' in {path}' if path else '')
    if name in ('Agent', 'Task'):
        sub = inp.get('subagent_type', '')
        desc = inp.get('description', '') or inp.get('prompt', '')
        return (f'[{sub}] {_trunc(str(desc), 160)}'
                if sub else _trunc(str(desc), 160))
    if name == 'WebFetch':
        return str(inp.get('url', ''))
    if name == 'WebSearch':
        return str(inp.get('query', ''))
    if name == 'TodoWrite':
        todos = inp.get('todos') or []
        return f'{len(todos)} todos'
    return _short_json(inp)


def _extract_result_text(content: Any) -> str:
    """tool_result.content can be a string or a list of content blocks
    (text/image). Return a flat text representation for trace rendering."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get('text'), str):
                parts.append(block['text'])
        return '\n'.join(parts) if parts else _short_json(content)
    return _short_json(content)


class _StreamParser:
    """Consume `claude -p --output-format stream-json` lines and emit
    human-readable trace lines. One instance per task run.

    Public surface: feed(line), tokens_used, cost_usd. The latched state
    (_tool_names cross-event mapping, _last_usage running totals so a
    SIGKILL before the result event still yields a token estimate) is
    internal — assert through rendered lines, not these attrs."""

    def __init__(self, expected_session_id: Optional[str] = None):
        self._tool_names: dict[str, str] = {}
        self._last_usage: dict = {}
        self._session_id: Optional[str] = None
        self._expected_session_id = expected_session_id
        self.cost_usd: Optional[float] = None

    def feed(self, line: str) -> list[str]:
        """Parse one stdout line; return rendered output lines (possibly empty
        for skipped events; the raw line if JSON parsing fails)."""
        line = line.rstrip()
        if not line:
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return [line]
        if not isinstance(event, dict):
            return [line]
        return self._render(event)

    @property
    def tokens_used(self) -> Optional[int]:
        if not self._last_usage:
            return None
        i = self._last_usage.get('input_tokens') or 0
        o = self._last_usage.get('output_tokens') or 0
        total = i + o
        return total if total > 0 else None

    def _render(self, event: dict) -> list[str]:
        prefix = '└ ' if event.get('parent_tool_use_id') else ''
        etype = event.get('type')
        if etype == 'system':
            return self._render_system(event, prefix)
        if etype == 'assistant':
            return self._render_assistant(event, prefix)
        if etype == 'user':
            return self._render_user(event, prefix)
        if etype == 'result':
            return self._render_result(event, prefix)
        if etype == 'error':
            msg = event.get('message') or event.get('error') or _short_json(event)
            return [f'{prefix}! error {_trunc(str(msg), 200)}']
        if etype == 'rate_limit_event':
            sub = event.get('subtype') or ''
            msg = (event.get('message') or event.get('reason')
                   or event.get('detail') or '')
            tail = f': {_trunc(str(msg), 140)}' if msg else ''
            return [f'{prefix}· rate_limit{f" {sub}" if sub else ""}{tail}']
        sub = event.get('subtype') or ''
        return [f'{prefix}?? {etype}{":" + sub if sub else ""}']

    def _render_system(self, event: dict, prefix: str) -> list[str]:
        sub = event.get('subtype', '')
        if sub == 'init':
            sid = event.get('session_id') or ''
            self._session_id = sid
            # Newer CLIs nest the init payload under `data`; older ones may
            # surface fields top-level. Tolerate both.
            data = event.get('data') if isinstance(event.get('data'), dict) else event
            model = data.get('model') or '?'
            tools = data.get('tools') or []
            tool_count = len(tools) if isinstance(tools, list) else 0
            out = [f'{prefix}· session={sid[:8]} model={model} tools={tool_count}']
            if (self._expected_session_id and sid
                    and sid != self._expected_session_id):
                out.append(
                    f'! session_id mismatch: claude={sid} '
                    f'expected={self._expected_session_id}'
                )
            return out
        if sub == 'compact_boundary':
            return [f'{prefix}· compaction']
        # Hook lifecycle events come from the user's local hook configuration
        # (~/.claude/settings.json). They fire constantly and aren't useful in
        # a per-task trace — drop them. The user opts in to richer hook output
        # via --include-hook-events, which we don't pass.
        if sub in ('hook_started', 'hook_response', 'hook_finished'):
            return []
        return [f'{prefix}?? system:{sub}'] if sub else [f'{prefix}?? system']

    def _render_assistant(self, event: dict, prefix: str) -> list[str]:
        msg = event.get('message') or {}
        # Latch running usage from every assistant turn so a kill before
        # the result event still leaves a token estimate.
        usage = msg.get('usage')
        if isinstance(usage, dict) and usage:
            self._last_usage = usage

        content = msg.get('content') or []
        out: list[str] = []
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'text':
                text = block.get('text') or ''
                # Emit one log entry per non-blank source line so the
                # [task:<id>] grep in api_task_output picks them all up.
                for ln in text.splitlines():
                    if ln.strip():
                        out.append(f'{prefix}[assistant] {ln}')
            elif btype == 'tool_use':
                tid = block.get('id') or ''
                name = block.get('name') or '?'
                if tid:
                    self._tool_names[tid] = name
                summary = _format_tool_input(name, block.get('input') or {})
                sep = ': ' if name in ('Bash', 'Grep', 'Glob') else ' '
                out.append(f'{prefix}→ {name}{sep}{summary}'
                           if summary else f'{prefix}→ {name}')
            elif btype == 'thinking':
                t = (block.get('thinking') or block.get('text') or '').strip()
                if t:
                    out.append(f'{prefix}[thinking] {_trunc(t, 200)}')
        return out

    def _render_user(self, event: dict, prefix: str) -> list[str]:
        msg = event.get('message') or {}
        content = msg.get('content') or []
        # User messages with string content are the kick prompt we sent —
        # no signal value, drop.
        if isinstance(content, str):
            return []
        out: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'tool_result':
                continue
            tid = block.get('tool_use_id') or ''
            name = self._tool_names.get(tid, '?')
            text = _extract_result_text(block.get('content'))
            line_count = text.count('\n') + (1 if text else 0)
            # len() over chars, not bytes — bytes would force a full encode of
            # potentially-huge tool_results (e.g. 100KB Read) on every event.
            char_count = len(text)
            first = _trunc(_first_nonblank_line(text), 140)
            err = ' ERROR' if block.get('is_error') else ''
            head = f'{prefix}←{err} {name} [{line_count}L, {char_count}C]'
            out.append(f'{head} {first}' if first else head)
        return out

    def _render_result(self, event: dict, prefix: str) -> list[str]:
        sub = event.get('subtype') or '?'
        usage = event.get('usage') or {}
        if isinstance(usage, dict) and usage:
            self._last_usage = usage
        cost = event.get('total_cost_usd')
        if isinstance(cost, (int, float)):
            self.cost_usd = float(cost)
        i = (usage.get('input_tokens') or 0) if isinstance(usage, dict) else 0
        o = (usage.get('output_tokens') or 0) if isinstance(usage, dict) else 0
        cr = (usage.get('cache_read_input_tokens') or 0) if isinstance(usage, dict) else 0
        cc = (usage.get('cache_creation_input_tokens') or 0) if isinstance(usage, dict) else 0
        turns = event.get('num_turns', '?')
        ms = event.get('duration_ms', '?')
        mark = '✓' if sub == 'success' else '✗'
        cost_str = f'${self.cost_usd:.4f}' if self.cost_usd is not None else '$?'
        return [f'{prefix}{mark} {sub} · turns={turns} '
                f'in={i:,} out={o:,} cache={cr:,}r/{cc:,}w · '
                f'{cost_str} · {ms}ms']


# ── subprocess driver ──────────────────────────────────────────────────────

def _run_claude(cmd: list[str], cwd: str,
                timeout_seconds: int, log_fn,
                lock_path: Optional[Path] = None,
                expected_session_id: Optional[str] = None
                ) -> tuple[int, bool, Optional[int], Optional[float]]:
    """
    Spawn `claude -p` as a subprocess in its own process group.
    Stream merged stdout+stderr through _StreamParser to log_fn.
    Returns (exit_code, timed_out, tokens_used, cost_usd).
    """
    # Scrub queue-worker secrets from the subprocess env: the spawned
    # `claude -p` runs untrusted task prompts and has no business reading our
    # session key, dashboard password, or org pin. Defense-in-depth on top of
    # config._DOTENV staying out of os.environ.
    from .config import subprocess_env
    proc = subprocess.Popen(
        cmd, cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env=subprocess_env(),
        preexec_fn=os.setsid,
    )

    # Store subprocess PID and PGID so the cancel API can kill the process group
    # directly without resolving PGID later (avoids PID-reuse race).
    if lock_path:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        update_task_lock(lock_path, {'subprocess_pid': proc.pid, 'subprocess_pgid': pgid})

    timed_out = False
    parser = _StreamParser(expected_session_id=expected_session_id)

    def stream():
        for line in proc.stdout:
            for rendered in parser.feed(line):
                log_fn(rendered)

    t = threading.Thread(target=stream, daemon=True)
    t.start()

    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()

    t.join(timeout=2)
    return proc.returncode, timed_out, parser.tokens_used, parser.cost_usd


def _find_new_checkpoint(agent_dir: Path, since: float) -> Optional[Path]:
    checkpoints = agent_dir / 'inbox' / 'checkpoints'
    if not checkpoints.exists():
        return None
    for f in sorted(checkpoints.glob('*.yaml')):
        if f.stat().st_mtime > since:
            return f
    return None


def _read_checkpoint(path: Path) -> dict:
    import yaml
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _build_cmd(task: Task, prompt: str, generated_id: str) -> list[str]:
    """Construct the claude argv. Resume path uses --resume <id> (which reuses
    the existing session by default — --fork-session is opt-in). Fresh path
    pins --session-id <uuid> so the on-disk transcript is at a known UUID.
    Both paths request stream-json; --verbose is required for stream-json to
    emit anything beyond the final result event in -p mode."""
    base = ['claude']
    if task.session_id:
        base += ['--resume', task.session_id]
    base += ['-p', '--dangerously-skip-permissions',
             '--output-format', 'stream-json', '--verbose']
    if not task.session_id:
        base += ['--session-id', generated_id]
    base.append(prompt)  # positional, must be last
    return base


def execute_task(task: Task, log: TaskLogger) -> ExecuteResult:
    """
    Full single-task lifecycle:
    1. Inject CLAUDE.md into project dir
    2. Spawn `claude -p --dangerously-skip-permissions`
    3. Determine outcome (checkpoint > dry-run > timeout > exit code > success)
    4. Cleanup CLAUDE.md in finally block (always runs)
    """
    start_time = time.monotonic()
    start_epoch = time.time()
    agent_dir = Path(task.resolved_dir) / '.agent'
    lock_path = acquire_task_lock(task.id, task.resolved_dir)
    backup: Optional[BackupInfo] = None

    try:
        # 1. Build and inject CLAUDE.md
        log.task(task.id, 'building CLAUDE.md')
        content = build_claude_md(task)
        backup = inject_claude_md(task.resolved_dir, content)
        update_task_lock(lock_path, {
            'claude_md_written': True,
            'backed_up_original': backup.had_original,
        })

        # 2. Spawn claude
        if task.session_id:
            prompt = (
                'You have been resumed by queue-worker to execute a queued task. '
                'Read CLAUDE.md in this directory for your task details and '
                'output conventions, then complete the task.'
            )
        else:
            prompt = (
                'You have been started by queue-worker. '
                'Your full context and task are in CLAUDE.md in this directory. '
                'Read CLAUDE.md first, then complete your task.'
            )
        # Resume reuses the prior session UUID; fresh runs get a new one.
        # Persist either way so the dashboard can link to the on-disk transcript
        # at ~/.claude/projects/<slug>/<actual_session_id>.jsonl.
        generated_id = task.session_id or str(uuid.uuid4())
        augment_task(task.yaml_path, {'actual_session_id': generated_id})

        cmd = _build_cmd(task, prompt, generated_id)
        timeout_s = task.budget.max_minutes * 60

        mode = f'--resume {task.session_id[:12]}...' if task.session_id else '-p'
        log.task(task.id, f'spawning claude {mode} (timeout: {task.budget.max_minutes}min)')
        exit_code, timed_out, tokens_used, cost_usd = _run_claude(
            cmd=cmd, cwd=task.resolved_dir,
            timeout_seconds=timeout_s,
            log_fn=lambda line: log.task(task.id, f'  {line}'),
            lock_path=lock_path,
            expected_session_id=generated_id,
        )

        duration = (time.monotonic() - start_time) / 60

        if tokens_used:
            cost_str = f' (~${cost_usd:.4f})' if cost_usd is not None else ''
            log.task(task.id, f'tokens used: {tokens_used:,}{cost_str}')

        # 3. Determine outcome (check in this order)

        # a) Checkpoint?
        checkpoint_path = _find_new_checkpoint(agent_dir, start_epoch)
        if checkpoint_path:
            log.task(task.id, f'checkpoint detected: {checkpoint_path.name}')
            cp = _read_checkpoint(checkpoint_path)
            augment_stall(task, 'checkpoint',
                          cp.get('question', 'Agent wrote a checkpoint.'),
                          checkpoint_content=cp)
            augment_task(task.yaml_path, {'checkpoint_file': str(checkpoint_path)})
            _write_episodic(task, 'unfinished', 'checkpoint',
                            duration, tokens_used, cost_usd)
            return ExecuteResult('unfinished', 'checkpoint',
                                cp.get('question'), duration, tokens_used, cost_usd)

        # b) Dry-run?
        if task.dry_run:
            today = datetime.now().strftime('%Y%m%d')
            dryrun_dir = agent_dir / 'inbox' / 'dryrun' / today
            if dryrun_dir.exists():
                log.task(task.id, 'dry-run output detected')
                augment_stall(task, 'dry_run_complete',
                              f'Proposed changes in .agent/inbox/dryrun/{today}/')
                _write_episodic(task, 'unfinished', 'dry_run_complete',
                                duration, tokens_used, cost_usd)
                return ExecuteResult('unfinished', 'dry_run_complete',
                                    f'Review .agent/inbox/dryrun/{today}/',
                                    duration, tokens_used, cost_usd)

        # c) Timeout?
        if timed_out:
            log.task(task.id, 'timed out')
            augment_stall(task, 'timeout',
                          f'Exceeded {task.budget.max_minutes} minute budget.')
            _write_episodic(task, 'unfinished', 'timeout',
                            duration, tokens_used, cost_usd)
            return ExecuteResult('unfinished', 'timeout',
                                f'Exceeded {task.budget.max_minutes}min budget',
                                duration, tokens_used, cost_usd)

        # d) Non-zero exit?
        if exit_code != 0:
            detail = f'claude exited with code {exit_code}'
            log.task(task.id, detail)
            _write_episodic(task, 'failed', None, duration, tokens_used, cost_usd)
            return ExecuteResult('failed', None, detail, duration, tokens_used, cost_usd)

        # e) Success
        log_path = agent_dir / 'log' / f'{datetime.now().strftime("%Y-%m-%d")}.md'
        if not log_path.exists():
            log.task(task.id, 'warning: agent did not write a session log')
        log.task(task.id, f'done ({duration:.1f}min)')
        _write_episodic(task, 'done', None, duration, tokens_used, cost_usd)
        return ExecuteResult('done', duration_minutes=duration,
                             tokens_used=tokens_used, cost_usd=cost_usd)

    finally:
        if backup is not None:
            cleanup_claude_md(task.resolved_dir, backup)
        release_task_lock(lock_path)


def _write_episodic(task: Task, status: str, stall_reason: Optional[str],
                    duration_minutes: float,
                    tokens_used: Optional[int] = None,
                    cost_usd: Optional[float] = None) -> None:
    entry = {
        'ts': now_iso(),
        'task_id': task.id,
        'status': status,
        'stall_reason': stall_reason,
        'duration_minutes': round(duration_minutes, 1),
        'level': task.level,
    }
    if tokens_used:
        entry['tokens_used'] = tokens_used
    if cost_usd is not None:
        entry['cost_usd'] = round(cost_usd, 6)
    append_episodic_entry(task.resolved_dir, entry)
