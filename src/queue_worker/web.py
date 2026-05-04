"""queue-worker web dashboard — FastAPI server on localhost:51002."""

import csv
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth
from .config import (QUEUE_DIR, LOG_DIR, STATIC_DIR,
                     bootstrap as _bootstrap, get_logger as _log)
from .usage_check import USAGE_CSV


_runner_thread: Optional[threading.Thread] = None
_runner_stop = threading.Event()


def _background_runner():
    """Run the queue state machine in a background thread, auto-restarting on crash."""
    from .runner import start_runner
    while not _runner_stop.is_set():
        try:
            start_runner(_log(), stop_event=_runner_stop, execute_lock=_execute_lock)
        except Exception as e:
            _log().error(f'background runner crashed: {e} — restarting in 15s')
            _runner_stop.wait(15)  # wait 15s before restart, or exit if stop signalled


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runner_thread
    _bootstrap()
    _runner_stop.clear()
    _runner_thread = threading.Thread(target=_background_runner, daemon=True)
    _runner_thread.start()
    _log().info('background runner started with web server')
    yield
    _runner_stop.set()
    from .runner import wake_runner
    wake_runner()  # interrupt any in-progress sleep so shutdown is fast

app = FastAPI(title="queue-worker", lifespan=lifespan)


# ── Auth middleware ──────────────────────────────────────────────────────────

_AUTH_EXEMPT_PATHS = {'/login', '/logout'}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if not auth.is_enabled():
        return await call_next(request)
    path = request.url.path
    if path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE_NAME)
    if auth.is_session_valid(token):
        return await call_next(request)
    # Unauthenticated. API → 401 JSON. HTML → redirect to /login.
    if path.startswith('/api/'):
        return JSONResponse({'detail': 'not authenticated'}, status_code=401)
    return RedirectResponse('/login', status_code=302)


# ── Routes: Auth ─────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    password: str


@app.get("/login")
def serve_login():
    return FileResponse(STATIC_DIR / 'login.html')


@app.post("/login")
def api_login(body: LoginBody, request: Request):
    ip = auth.extract_client_ip(request)
    result = auth.check_password_attempt(ip, body.password)
    if result.success:
        token, max_age = auth.create_session()
        resp = JSONResponse({'ok': True})
        resp.set_cookie(auth.COOKIE_NAME, token, max_age=max_age,
                        httponly=True, secure=auth.COOKIE_SECURE,
                        samesite='lax', path='/')
        return resp
    if result.reason in (auth.AuthReason.IP_LOCKED, auth.AuthReason.GLOBAL_LOCKED):
        _log().warn(f'auth: {result.reason.value} for {ip} (retry_after={result.retry_after}s)')
        return JSONResponse(
            {'detail': result.reason.value, 'retry_after': result.retry_after},
            status_code=429,
        )
    _log().warn(f'auth: wrong password from {ip}')
    return JSONResponse(
        {'detail': 'wrong_password', 'remaining_attempts': result.remaining_attempts or 0},
        status_code=401,
    )


@app.post("/logout")
def api_logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME)
    auth.invalidate_session(token)
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(auth.COOKIE_NAME, path='/')
    return resp


# ── Thread management ────────────────────────────────────────────────────────

_run_lock = threading.Lock()           # guards _task_threads / _run_thread bookkeeping
_execute_lock = threading.Lock()       # serializes claude -p subprocesses across runner + web
_run_thread: Optional[threading.Thread] = None
_run_error: Optional[str] = None
_task_threads: dict[str, threading.Thread] = {}
_task_errors: dict[str, Optional[str]] = {}
_MAX_TASK_HISTORY = 100


def _run_once_worker():
    global _run_error
    if not _execute_lock.acquire(blocking=False):
        _run_error = 'Another task is already running'
        return
    try:
        from .runner import start_runner
        start_runner(_log(), run_once=True)
        _run_error = None
    except Exception as e:
        _run_error = str(e)
    finally:
        _execute_lock.release()
        from .runner import wake_runner
        wake_runner()  # let the burning loop pick up the next task immediately


def _run_single_task_worker(task_id: str):
    if not _execute_lock.acquire(blocking=False):
        _task_errors[task_id] = 'Another task is already running'
        return
    try:
        from .queue_ops import begin_task
        from .runner import run_and_finalize

        task = begin_task(task_id, _log())
        run_and_finalize(task, _log())
        _task_errors[task_id] = None
    except Exception as e:
        _task_errors[task_id] = str(e)
    finally:
        _execute_lock.release()
        from .runner import wake_runner
        wake_runner()  # let the burning loop pick up the next task immediately


def _cleanup_task_history():
    if len(_task_threads) > _MAX_TASK_HISTORY:
        dead = [k for k, t in _task_threads.items() if not t.is_alive()]
        for k in dead[:-_MAX_TASK_HISTORY // 2]:
            _task_threads.pop(k, None)
            _task_errors.pop(k, None)


# ── Pydantic models ──────────────────────────────────────────────────────────

class LevelEnum(str, Enum):
    observer = 'observer'
    craftsman = 'craftsman'
    committer = 'committer'
    deployer = 'deployer'

class StallReasonEnum(str, Enum):
    timeout = 'timeout'
    checkpoint = 'checkpoint'
    dry_run_complete = 'dry_run_complete'
    uncertain = 'uncertain'

RunPolicy = Literal['this_session', 'next_session', 'tonight']


class AddTaskBody(BaseModel):
    dir: str
    prompt: str
    level: LevelEnum = LevelEnum.craftsman
    priority: int = Field(default=3, ge=1, le=5)
    dry_run: bool = False
    depends_on: list[str] = []
    tags: list[str] = []
    max_minutes: Optional[int] = Field(default=None, ge=1)
    run_policy: RunPolicy = 'this_session'
    session_id: Optional[str] = None


class EditTaskBody(BaseModel):
    prompt: Optional[str] = None
    dir: Optional[str] = None
    level: Optional[LevelEnum] = None
    priority: Optional[int] = Field(default=None, ge=1, le=5)
    dry_run: Optional[bool] = None
    tags: Optional[list[str]] = None
    depends_on: Optional[list[str]] = None
    max_minutes: Optional[int] = Field(default=None, ge=1)
    run_policy: Optional[RunPolicy] = None


class FailBody(BaseModel):
    detail: str = ''


class StallBody(BaseModel):
    reason: StallReasonEnum
    detail: str = ''


# ── Helpers ───────────────────────────────────────────────────────────────────

def _not_found(task_id: str):
    raise HTTPException(404, f'Task {task_id} not found')


def _task_to_dict(task) -> dict:
    return asdict(task)


# ── Routes: Static ───────────────────────────────────────────────────────────

@app.get("/")
def serve_index():
    # Disable browser caching of the dashboard HTML — the file is small and
    # ships UI fixes that we want to land the moment the user reloads. Without
    # this header Chrome happily serves a stale copy after a server restart.
    return FileResponse(
        STATIC_DIR / 'index.html',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


# ── Routes: Browse filesystem ────────────────────────────────────────────────

@app.get("/api/browse")
def api_browse(path: str = Query(default="~")):
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, f'Invalid path: {path}')
    if not resolved.is_dir():
        raise HTTPException(400, f'Not a directory: {resolved}')
    entries = []
    try:
        for entry in sorted(resolved.iterdir()):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir():
                entries.append({'name': entry.name, 'path': str(entry)})
    except PermissionError:
        pass
    return {
        'current': str(resolved),
        'parent': str(resolved.parent) if resolved != resolved.parent else None,
        'dirs': entries,
    }


# ── Routes: Status ───────────────────────────────────────────────────────────

def _redact_runner_state(state: dict) -> dict:
    """Defense-in-depth: scrub any sk-ant- key or email from runner state
    strings before sending to the dashboard. Iterates every string field
    so future additions to RunnerState are covered automatically.
    """
    from .usage_check_http import redact
    out = dict(state)
    for k, v in out.items():
        if isinstance(v, str) and v:
            out[k] = redact(v)
    return out


@app.get("/api/status")
def api_status():
    from .runner import get_runner_state
    from .queue_ops import get_queue_counts
    return {
        'runner': _redact_runner_state(get_runner_state()),
        'counts': get_queue_counts(),
    }


@app.post("/api/check-usage")
def api_check_usage():
    """Trigger a usage check immediately. Returns the new runner state."""
    from .runner import do_usage_check, get_runner_state
    do_usage_check(_log(), kind='manual')
    return _redact_runner_state(get_runner_state())


# ── Routes: Tasks CRUD ───────────────────────────────────────────────────────

@app.get("/api/tasks")
def api_list_tasks(status: Optional[str] = Query(None)):
    from .queue_ops import list_tasks
    return list_tasks(status)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: str):
    from .task import parse_task
    from .queue_ops import find_task_yaml
    path = find_task_yaml(task_id)
    if not path:
        _not_found(task_id)
    task = parse_task(str(path))
    d = _task_to_dict(task)
    d['_status'] = path.parent.name
    return d


@app.post("/api/tasks", status_code=201)
def api_add_task(body: AddTaskBody):
    from .queue_ops import create_task
    from .runner import wake_runner
    task_id, out_path = create_task(
        body.dir, body.prompt, body.level.value, body.priority,
        body.dry_run, body.depends_on, body.tags, body.max_minutes,
        run_policy=body.run_policy, session_id=body.session_id)
    wake_runner()  # so a burning runner picks up the new task immediately
    return {'id': task_id, 'file': str(out_path)}


@app.patch("/api/tasks/{task_id}")
def api_edit_task(task_id: str, body: EditTaskBody):
    """Update a pending task's editable fields. Reports the diff so the UI
    can confirm what actually changed (and stay quiet when nothing did).
    Serializes against begin_task via PENDING_MUTATION_LOCK so a runner
    that starts the task mid-edit can't leave duplicate YAML behind.
    """
    from pathlib import Path as PathLib
    from .task import expand_path, update_task_yaml
    from .queue_ops import (find_task_yaml, compute_eligible_at,
                            PENDING_MUTATION_LOCK)
    from .runner import get_runner_state, wake_runner

    if body.prompt is not None and not body.prompt.strip():
        # Empty/whitespace prompt would corrupt the YAML — parse_task rejects
        # tasks where required fields are falsy, and a 500 on every subsequent
        # view of the task is a worse outcome than rejecting the edit here.
        raise HTTPException(400, 'prompt cannot be empty or whitespace')

    set_fields: dict = {
        'prompt': body.prompt,
        'level': body.level.value if body.level else None,
        'priority': body.priority,
        'dry_run': body.dry_run,
        'tags': body.tags,
        'depends_on': body.depends_on,
        'run_policy': body.run_policy,
    }
    delete_fields: tuple[str, ...] = ()

    if body.dir is not None and body.dir.strip():
        abs_dir = expand_path(body.dir)
        # Reject early — prevents silently committing a task whose runner will
        # later fail to chdir.
        if not PathLib(abs_dir).is_dir():
            raise HTTPException(400, f'dir does not exist: {abs_dir}')
        set_fields['dir'] = abs_dir

    if body.max_minutes is not None:
        set_fields['budget'] = {'max_minutes': body.max_minutes}

    if body.run_policy is not None:
        # Recompute eligible_at fresh so switching `tonight` → `this_session`
        # doesn't leave stale gating around. Pydantic already validated
        # run_policy against the RunPolicy Literal.
        new_elig = compute_eligible_at(body.run_policy, get_runner_state())
        if new_elig is not None:
            set_fields['eligible_at'] = new_elig
        else:
            delete_fields = ('eligible_at',)

    with PENDING_MUTATION_LOCK:
        path = find_task_yaml(task_id)
        if not path:
            _not_found(task_id)
        if path.parent.name != 'pending':
            raise HTTPException(400, 'Only pending tasks can be edited')
        changed = update_task_yaml(str(path), set_fields, delete_fields)
    if changed:
        wake_runner()  # eligibility may have just changed
    return {'updated': task_id, 'fields': changed}


@app.post("/api/tasks/{task_id}/unfinish")
def api_unfinish(task_id: str):
    """Move a pending task into queue/unfinished/. Uses the same machinery
    as a runner-driven stall so the task can be resumed later via /retry.
    Serializes against begin_task via PENDING_MUTATION_LOCK.
    """
    from .task import parse_task
    from .queue_ops import move_task, augment_stall, PENDING_MUTATION_LOCK
    src = QUEUE_DIR / 'pending' / f'{task_id}.yaml'
    with PENDING_MUTATION_LOCK:
        if not src.exists():
            raise HTTPException(404, f'Task {task_id} not found in pending/')
        task = parse_task(str(src))
        augment_stall(task, 'manual', 'moved to unfinished from web')
        move_task(task, 'unfinished')
    _log().task(task_id, 'moved to unfinished (manual)')
    return {'unfinished': task_id}


@app.delete("/api/tasks/{task_id}")
def api_remove_task(task_id: str):
    from .queue_ops import remove_task
    try:
        queue_name = remove_task(task_id, _log())
        return {'removed': task_id, 'was_in': queue_name}
    except FileNotFoundError:
        _not_found(task_id)


# ── Routes: Lifecycle actions ────────────────────────────────────────────────

@app.post("/api/tasks/{task_id}/retry")
def api_retry(task_id: str):
    from .queue_ops import retry_task
    try:
        has_checkpoint = retry_task(task_id, _log())
        return {'retried': task_id, 'checkpoint_answer_preserved': has_checkpoint}
    except FileNotFoundError:
        _not_found(task_id)


@app.post("/api/tasks/{task_id}/begin")
def api_begin(task_id: str):
    from .queue_ops import begin_task
    try:
        begin_task(task_id, _log())
        return {'started': task_id}
    except FileNotFoundError:
        _not_found(task_id)


@app.post("/api/tasks/{task_id}/done")
def api_done(task_id: str):
    from .queue_ops import complete_task
    try:
        complete_task(task_id, _log())
        return {'completed': task_id}
    except FileNotFoundError:
        _not_found(task_id)


@app.post("/api/tasks/{task_id}/fail")
def api_fail(task_id: str, body: FailBody):
    from .queue_ops import fail_task
    try:
        fail_task(task_id, body.detail, _log())
        return {'failed': task_id}
    except FileNotFoundError:
        _not_found(task_id)


@app.post("/api/tasks/{task_id}/stall")
def api_stall(task_id: str, body: StallBody):
    from .queue_ops import stall_task
    try:
        stall_task(task_id, body.reason, body.detail, _log())
        return {'stalled': task_id}
    except FileNotFoundError:
        _not_found(task_id)


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel(task_id: str):
    """Kill a running task's subprocess and let the executor move it to failed."""
    import os, signal, json
    from .lock import RUNNING_DIR

    lock_file = RUNNING_DIR / f'{task_id}.lock'
    try:
        data = json.loads(lock_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raise HTTPException(404, f'No running lock for {task_id}')

    pgid = data.get('subprocess_pgid')
    sub_pid = data.get('subprocess_pid')
    if not pgid and not sub_pid:
        raise HTTPException(409, 'Task has no subprocess yet (still starting up?)')

    target = pgid or sub_pid
    try:
        os.killpg(target, signal.SIGTERM)
        _log().info(f'cancel: sent SIGTERM to pgid {target} for task {task_id}')
    except ProcessLookupError:
        _log().info(f'cancel: pgid {target} already dead for task {task_id}')
    except OSError as e:
        raise HTTPException(500, f'Failed to kill subprocess: {e}')

    return {'cancelled': task_id, 'killed_pgid': target}


# ── Routes: Run task / Next / Context ────────────────────────────────────────

@app.post("/api/tasks/{task_id}/run")
def api_run_task(task_id: str):
    src = QUEUE_DIR / 'pending' / f'{task_id}.yaml'
    if not src.exists():
        _not_found(task_id)
    with _run_lock:
        if task_id in _task_threads and _task_threads[task_id].is_alive():
            raise HTTPException(409, f'Task {task_id} is already running')
        t = threading.Thread(target=_run_single_task_worker, args=(task_id,), daemon=True)
        _task_threads[task_id] = t
        t.start()
    _cleanup_task_history()
    return {'running': task_id}


@app.get("/api/tasks/{task_id}/run-status")
def api_run_task_status(task_id: str):
    if task_id in _task_threads:
        return {'running': _task_threads[task_id].is_alive(), 'error': _task_errors.get(task_id)}
    return {'running': False, 'error': None}


@app.get("/api/next")
def api_next():
    from .queue_ops import get_pending_tasks, resolve_run_order
    ordered = resolve_run_order(get_pending_tasks(_log()), _log())
    if not ordered:
        raise HTTPException(404, 'No tasks ready')
    return _task_to_dict(ordered[0])


@app.get("/api/context/{task_id}")
def api_context(task_id: str):
    from .injector import render_task_context
    rendered = render_task_context(task_id)
    if rendered is None:
        _not_found(task_id)
    return PlainTextResponse(rendered)


# ── Routes: Resume info ──────────────────────────────────────────────────────

@app.get("/api/tasks/{task_id}/resume-info")
def api_resume_info(task_id: str):
    """Return a summary of the conversation a queued task will resume.

    Distinct error shapes per failure mode so a missing transcript doesn't
    masquerade as a slug bug. Read-only — does not touch any task YAML.
    """
    from .task import parse_task
    from .queue_ops import find_task_yaml
    from . import sessions as sess

    path = find_task_yaml(task_id)
    if not path:
        return JSONResponse({'error': 'task_not_found'}, status_code=404)
    task = parse_task(str(path))
    if not task.session_id:
        return JSONResponse({'error': 'no_session_id'}, status_code=400)
    if not sess.is_valid_session_id(task.session_id):
        return JSONResponse({
            'error': 'invalid_session_id',
            'detail': 'session_id is not a UUID — possibly a web (cse_*) session or hand-edited YAML',
        }, status_code=400)

    project_dir = sess.project_dir_for(task.resolved_dir)
    transcript = sess.find_transcript(task.resolved_dir, task.session_id)
    if transcript is None:
        if not project_dir.exists():
            return JSONResponse({
                'error': 'project_dir_not_found',
                'expected_slug': sess.slugify_cwd(task.resolved_dir),
                'expected_path': str(project_dir),
            }, status_code=404)
        return JSONResponse({
            'error': 'transcript_not_found',
            'transcript_path': str(project_dir / f'{task.session_id}.jsonl'),
        }, status_code=404)
    try:
        summary = sess.summarize_session(transcript)
    except OSError as e:
        return JSONResponse({
            'error': 'transcript_unreadable', 'detail': str(e),
        }, status_code=422)
    summary['session_id'] = task.session_id
    return summary


@app.get("/api/tasks/{task_id}/transcript")
def api_transcript(task_id: str):
    """Locate the on-disk Claude Code transcript for THIS run of a task.

    Distinct from /resume-info: that one summarizes the conversation the task
    will resume FROM (task.session_id, set at queue time). This one reports
    where the transcript for the run we just performed lives (actual_session_id,
    written by the executor when claude is spawned). Useful for: clicking
    through to the full tool-by-tool trace from the dashboard.
    """
    from .task import parse_task
    from .queue_ops import find_task_yaml
    from . import sessions as sess

    path = find_task_yaml(task_id)
    if not path:
        return JSONResponse({'error': 'task_not_found'}, status_code=404)
    task = parse_task(str(path))
    sid = task.actual_session_id
    if not sid:
        return JSONResponse({'error': 'no_actual_session_id'}, status_code=404)
    if not sess.is_valid_session_id(sid):
        return JSONResponse({
            'error': 'invalid_session_id',
            'detail': 'actual_session_id is not a UUID — possibly hand-edited YAML',
        }, status_code=400)

    project_dir = sess.project_dir_for(task.resolved_dir)
    transcript = sess.find_transcript(task.resolved_dir, sid)
    if transcript is None:
        return JSONResponse({
            'error': 'transcript_not_found',
            'session_id': sid,
            'expected_path': str(project_dir / f'{sid}.jsonl'),
            'project_dir_exists': project_dir.exists(),
        }, status_code=404)

    try:
        st = transcript.stat()
    except OSError as e:
        return JSONResponse({
            'error': 'transcript_unreadable', 'detail': str(e),
        }, status_code=422)

    return {
        'session_id': sid,
        'transcript_path': str(transcript),
        'project_dir': str(project_dir),
        'file_size': st.st_size,
    }


# ── Routes: Logs ─────────────────────────────────────────────────────────────

@app.get("/api/logs")
def api_logs(date: Optional[str] = Query(None), task_id: Optional[str] = Query(None)):
    from .logger import read_log_lines
    date_str, lines = read_log_lines(LOG_DIR, date=date, task_id=task_id)
    return {'date': date_str, 'lines': lines}


@app.get("/api/tasks/{task_id}/output")
def api_task_output(task_id: str, offset: int = Query(default=0, ge=0)):
    """Return the terminal output for a task, collected from relevant log files."""
    tag = f'[task:{task_id}]'
    all_lines: list[str] = []
    if not LOG_DIR.exists():
        return {'task_id': task_id, 'lines': [], 'total': 0}
    # Extract date from task ID (format: name-YYYYMMDD-xxxx) to skip older logs
    parts = task_id.rsplit('-', 2)
    min_date = parts[-2] if len(parts) >= 3 and len(parts[-2]) == 8 else None
    for log_file in sorted(LOG_DIR.glob('*.log')):
        if min_date and log_file.stem.replace('-', '') < min_date:
            continue
        with open(log_file) as f:
            for line in f:
                if tag not in line:
                    continue
                all_lines.append(line.rstrip())
    total = len(all_lines)
    return {'task_id': task_id, 'lines': all_lines[offset:], 'total': total}


# ── Routes: Run once ─────────────────────────────────────────────────────────

@app.post("/api/run-once")
def api_run_once():
    global _run_thread
    with _run_lock:
        if _run_thread and _run_thread.is_alive():
            raise HTTPException(409, 'Runner is already active')
        _run_thread = threading.Thread(target=_run_once_worker, daemon=True)
        _run_thread.start()
    return {'started': True}


@app.get("/api/run-once/status")
def api_run_once_status():
    if _run_thread is None:
        return {'running': False, 'ever_ran': False}
    return {'running': _run_thread.is_alive(), 'ever_ran': True, 'error': _run_error}


# ── Routes: Usage history ────────────────────────────────────────────────────

@app.get("/api/usage-history")
def api_usage_history():
    if not USAGE_CSV.exists():
        return []
    rows = []
    with open(USAGE_CSV, newline='') as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for row in reader:
            if len(row) < 3:
                continue
            try:
                pct = int(row[1].strip().rstrip('%'))
            except ValueError:
                pct = None
            rows.append({
                'datetime': row[0].strip(), 'usage_pct': pct,
                'resets_in': row[2].strip(),
                'status': row[3].strip() if len(row) > 3 else '',
            })
    return rows


# ── Routes: File browser ────────────────────────────────────────────────────

from . import file_browser as fb


@app.get("/api/files/list")
def api_files_list(path: str = Query(...)):
    p = fb.normalize_path(path)
    if not p.exists():
        raise HTTPException(404, f'Not found: {path}')
    if not p.is_dir():
        raise HTTPException(400, f'Not a directory: {p}')
    return fb.list_directory(p)


@app.get("/api/files/read")
def api_files_read(path: str = Query(...)):
    p = fb.normalize_path(path)
    if not p.exists():
        raise HTTPException(404, f'Not found: {path}')
    return fb.read_text_file(p)


@app.get("/api/files/raw")
def api_files_raw(path: str = Query(...)):
    p = fb.normalize_path(path)
    if not p.exists():
        raise HTTPException(404, f'Not found: {path}')
    mime = fb.get_raw_mime(p)
    if mime is None:
        raise HTTPException(415, f'Cannot serve as raw: {p.name}')
    # No filename= — Starlette would set Content-Disposition: attachment and
    # break inline preview in <img>/<embed>.
    return FileResponse(p, media_type=mime)


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=51002, log_level='info')

if __name__ == '__main__':
    main()
