# Web dashboard

A FastAPI + Alpine.js SPA at `localhost:51002` for managing the queue visually. Launch with:

```bash
queue-worker-web
# open http://localhost:51002
```

The web server runs the runner state machine in a background thread, so you only need one process. The runner thread auto-restarts on crash (15-second cooldown), and the main loop catches individual task errors without taking down the thread.

> Don't run `queue-worker run` alongside `queue-worker-web` against the same queue — both would try to process tasks. Pick one.

## Views

| View | What |
|---|---|
| **Dashboard** | Runner state (`CHILLING` / `BURNING`), burn deadline countdown, last usage check (pct + reset minutes + status), queue counts, `Run All (once)` button, `Check usage now` button |
| **Tasks** | Sortable by ID / priority / created / finished. Filter by status. Tasks with a `session_id` show a purple `↻ RESUME` pill |
| **Task detail** | Read-only by default. For pending tasks, `Edit` opens a full form (dir / prompt / level / priority / depends_on / max_minutes / run_policy / tags / dry_run). Switching `run_policy` to/from `this_session` recomputes or clears `eligible_at`. `Move to Unfinished` parks a pending task. For tasks with `session_id`, a banner shows the resumed conversation's last user message + click-to-copy session id (loaded lazily, with cwd-verification against the transcript so a slug collision can't surface a different project's chat). `Run This Task` / `Cancel Task` / `Terminal Output` controls. After a run lands, the detail also shows tokens, USD cost, and a `Transcript` link that opens the on-disk Claude Code session JSONL via the file viewer |
| **Add task** | Form with folder picker, prompt, when-to-run, level, priority, dry-run, tags, deps |
| **File browser** | Click `Files` on any task detail to browse that task's working directory. Text files render inline (256 KB cap, null-byte probe rejects binaries). Images and PDFs preview via `<img>` / `<embed>`. SVG is intentionally excluded from inline rendering (stored-XSS vector) and falls back to text source. `..` navigates freely past the task dir — no sandbox, read-only |
| **Logs** | Date picker + task ID filter |
| **Usage** | Chart of session usage from `usage_history.csv` with Today / Week / Month / All ranges |

## Headless setup

If you don't want the web UI:

```bash
nohup queue-worker run > /dev/null 2>&1 &
```

The CLI runner has the same state machine. You can still poke at the queue with `queue-worker ls`, `add`, `remove`, `retry`, `logs`, etc.

## REST API

All endpoints require a valid session cookie if `QUEUE_WORKER_PASSWORD` is set.

### Runner & queue state

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/status` | Runner state + queue counts |
| `POST` | `/api/check-usage` | Trigger a usage check now |
| `GET`  | `/api/next` | Peek the next ready task (highest priority, deps met) |
| `POST` | `/api/run-once` | Drain the queue once in the background |
| `GET`  | `/api/run-once/status` | Poll whether the background run-once is still active |

### Tasks — CRUD

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/tasks` | List all tasks (optional `?status=pending`) |
| `GET`  | `/api/tasks/{id}` | Get one task |
| `POST` | `/api/tasks` | Add a task |
| `PATCH`| `/api/tasks/{id}` | Edit a pending task. Accepts `dir`, `prompt`, `level`, `priority`, `dry_run`, `tags`, `depends_on`, `max_minutes`, `run_policy`. Recomputes `eligible_at` on `run_policy` change. Returns `{updated, fields}`. 400 on empty/whitespace prompt or non-existent dir |
| `DELETE`| `/api/tasks/{id}` | Remove a task from any queue |

### Tasks — execution & lifecycle

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/tasks/{id}/run` | Run a single task immediately (background thread) |
| `GET`  | `/api/tasks/{id}/run-status` | Poll a single-task run thread |
| `POST` | `/api/tasks/{id}/retry` | Move unfinished/failed → pending |
| `POST` | `/api/tasks/{id}/begin` | Manual: pending → running |
| `POST` | `/api/tasks/{id}/done` | Manual: running → done |
| `POST` | `/api/tasks/{id}/fail` | Manual: running → failed |
| `POST` | `/api/tasks/{id}/cancel` | Kill running subprocess → failed |
| `POST` | `/api/tasks/{id}/stall` | Manual: running → unfinished |
| `POST` | `/api/tasks/{id}/unfinish` | Manual: pending → unfinished. Stamps `stall_reason: manual` |

### Diagnostics

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/tasks/{id}/output` | Tail per-task structured trace (one rendered line per stream-json event — tool calls, tool results, assistant turns, init / cost / token totals). Supports `?offset=N` for incremental polling |
| `GET`  | `/api/tasks/{id}/resume-info` | Summary of the Claude conversation a queued task will resume. Verifies the transcript's recorded `cwd` matches the task before returning. Distinct error shapes per failure: `no_session_id` / `invalid_session_id` (400), `project_dir_not_found` / `transcript_not_found` (404), `transcript_unreadable` (422) |
| `GET`  | `/api/tasks/{id}/transcript` | Locate the on-disk Claude Code transcript for THIS run (uses `actual_session_id` written by the executor). Returns `{session_id, transcript_path, project_dir, file_size}`. Distinct from `/resume-info` which reports the resume *source* — this one reports the *output* of the run. 404 on missing actual_session_id or transcript |
| `GET`  | `/api/context/{id}` | Render the full `CLAUDE.md` that will be injected for the task |
| `GET`  | `/api/logs` | Daily logs, optional `?date=` and `?task_id=` filters |
| `GET`  | `/api/usage-history` | Usage chart data parsed from `usage_history.csv` |

### Filesystem (read-only)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/browse?path=...` | Folder-picker listing (dirs only, hides dotfiles) — used by Add Task |
| `GET`  | `/api/files/list?path=...` | Directory listing for the file browser (dirs + files with kind / size / denied, 1000-entry cap) |
| `GET`  | `/api/files/read?path=...` | UTF-8 text content (256 KB cap, returns a `reason` enum: `ok` / `too_large` / `binary` / `denied` / `not_regular`) |
| `GET`  | `/api/files/raw?path=...` | Streams raw image/PDF bytes via `FileResponse` for inline preview (50 MB cap, 415 for unsupported extensions) |

⚠️ **Filesystem endpoints are not sandboxed.** `..` navigation and arbitrary absolute paths are allowed. Fine for personal use on a trusted network (Tailscale), but don't expose the server publicly without a password + scoping. See [security.md](security.md).

## `/api/status` response shape

```json
{
  "runner": {
    "state": "burning",
    "burn_until": 1775984373.9,
    "last_check_at": 1775971653.9,
    "last_check_pct": 30,
    "last_check_reset_minutes": 212,
    "last_check_status": "NEED TO BURN TOKEN !",
    "last_check_error": null,
    "next_reset_at": 1775984673.9,
    "post_reset_done_for_cycle": true,
    "pre_reset_done_for_cycle": false,
    "last_anchor_kind": "post_reset",
    "last_anchor_at": 1775985000.0,
    "scheduled_checks": [
      {"due_at": 1775984073.9, "kind": "pre_reset"},
      {"due_at": 1775984973.9, "kind": "post_reset"}
    ]
  },
  "counts": {"pending": 3, "running": 0, "done": 12, "unfinished": 0, "failed": 0}
}
```
