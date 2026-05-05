---
name: queue
description: "Queue a prompt for later autonomous execution, resuming the current conversation"
argument-hint: "<prompt>"
allowed-tools:
  - Bash
  - Read
---

# /queue — Save a prompt to queue-worker for later execution

The user wants to queue a follow-up task that will run autonomously during the next
burn window (or when manually triggered). The task will **resume this conversation**
so the agent has full context of what was discussed.

## What you receive

Everything after `/queue` is the prompt to queue. For example:
- `/queue Fix the auth bug we discussed` → prompt is "Fix the auth bug we discussed"
- `/queue` (no args) → ask the user what they want to queue

## Steps

### 1. Get the session ID

First try the env var:

```bash
echo "$CLAUDE_CODE_SESSION_ID"
```

If empty or unset, fall back to scanning `~/.claude/sessions/` for the most
recently-modified session matching the current cwd:

```bash
python3 -c "
import json, os
cwd = os.getcwd()
sessions_dir = os.path.expanduser('~/.claude/sessions')
best = None
for f in os.listdir(sessions_dir):
    if not f.endswith('.json'): continue
    path = os.path.join(sessions_dir, f)
    try:
        data = json.load(open(path))
    except: continue
    if data.get('cwd') == cwd:
        mtime = os.path.getmtime(path)
        if best is None or mtime > best[1]:
            best = (data.get('sessionId'), mtime)
print(best[0] if best else '')
"
```

If the ID starts with `cse_` (web session), warn: "This is a web session — resume
won't work. The task will run as a fresh session instead." and set session_id to empty.

If still empty, warn and proceed without a session_id (task runs as fresh session).

### 2. Determine the project directory

Use the current working directory (`pwd`).

### 3. Queue the task

POST to the queue-worker API:

```bash
curl -s -X POST http://localhost:51002/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "dir": "<cwd>",
    "prompt": "<the user prompt>",
    "level": "craftsman",
    "priority": 3,
    "session_id": "<session-id-or-null>",
    "run_policy": "this_session"
  }'
```

If the API is unreachable (queue-worker-web not running), fall back to the CLI:

```bash
queue-worker add "<cwd>" "<prompt>" --session-id "<session-id>"
```

### 4. Report back

Tell the user:
- The task ID that was created
- That it will resume this conversation when executed
- When it will run (next burn window, or "trigger manually from the dashboard")

Keep the response short — 2-3 lines max.

## Important

- Default level is `craftsman` unless the user specifies otherwise
- Default priority is 3 (normal) unless the user specifies otherwise
- If the user says something like `/queue --priority 1 Fix the critical bug`, parse
  the flags and pass them through
- Always include the session_id so the task resumes this conversation
- The task prompt should be exactly what the user typed (minus any flags you parsed)
