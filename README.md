# queue-up-for-claude

A usage-aware job queue for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

You queue tasks against your projects. The runner sits idle most of the day and only spawns `claude -p` subprocesses during the last hour of your 5-hour Claude.ai usage window — so unused budget gets used instead of wiped at reset. Each task gets a fresh Claude session with a project-specific identity, capability boundaries, and rolling memory injected via `CLAUDE.md`.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Anthropic. Reads your Claude.ai plan usage by calling the same web endpoints the claude.ai UI uses with your `sessionKey` cookie. **This may violate Anthropic's Terms of Service.** See [Disclaimer](#disclaimer). If the cookie API ever stops working, this tool stops working — there is no browser-scraping fallback.

> ⚠️ **Full-disk file access.** This tool can read any file on your computer that your user account can read. Two reasons:
> 1. The dashboard's file-browser API (`/api/files/list|read|raw`) is **not sandboxed** — it accepts any absolute path and resolves it via the OS. Anyone who can reach the dashboard can browse your whole home directory. Keep it on `127.0.0.1` / Tailscale and put a password on it before exposing it.
> 2. Each task runs `claude -p --dangerously-skip-permissions`, so the Claude Code subprocess has no per-file permission prompts — it can read/write anywhere your user can.
>
> **Nothing is uploaded to the author and there is no telemetry.** The session key, usage history, task prompts, and logs all stay on your machine. The two outbound destinations are the ones you'd expect: `claude.ai` (for usage checks, with your cookie) and Anthropic's API (whatever `claude -p` sends as part of running your tasks — prompts and any files it reads, same as using Claude Code directly). See [Data storage](#data-storage).

---

## Why

If you're on a Claude Max plan, your token allotment refills every five hours regardless of whether you used the previous bucket. Pure on-demand usage leaves a lot on the table. queue-up-for-claude lets you stage work asynchronously and only burns it when you have unused budget about to expire — turning idle plan capacity into shipped code.

The runner is also a structured way to give a Claude Code subprocess a stable identity per project (who it is, what the project is, what it's allowed to do, what it remembers from prior sessions) without you re-typing context each time.

---

## How it works

Two states: **chilling** (default) and **burning** (active).

```
              ┌─────────────┐  hourly + reset-anchored checks
              │  CHILLING   │  (HH:00 + T-60 / T-10 / T+5)
              └──────┬──────┘
                     │  pct_remaining ≥ 30% AND reset < 70min
              ┌──────▼──────┐
              │   BURNING   │  one task at a time until reset
              └──────┬──────┘  (recheck usage between tasks)
                     │
                     ▼
       claude -p subprocess per task,
       fresh CLAUDE.md injected per task,
       outcome recorded, queue advances.
```

For each task the runner:

1. Resolves dependencies + priority to pick the next ready task.
2. Builds a `CLAUDE.md` from the project's `.agent/` files (identity, context, behavior rules, capability boundaries, rolling memory) and injects it into the project dir.
3. Spawns `claude -p --dangerously-skip-permissions` in a fresh process group.
4. Watches for checkpoint files, dry-run output, timeouts, and exit codes.
5. Restores the original `CLAUDE.md`, moves the task YAML to `done/` / `unfinished/` / `failed/`, and updates the project's episodic memory.

Full state-machine details: [docs/runner-state-machine.md](docs/runner-state-machine.md).

---

## Quick start

Requires **Python 3.11+** and **Claude Code** installed and authenticated. Tested on macOS, should work on Linux. No browser dependency.

```bash
# Install
git clone <your-fork-url> queue-up-for-claude
cd queue-up-for-claude
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env && chmod 0600 .env
# paste your claude.ai sessionKey into .env (see docs/usage-checking.md)

# Scaffold a project
./queue-worker init ~/projects/my-app
# fill in .agent/AGENT.md, CONTEXT.md, BEHAVIOR.md (templates included)

# Queue a task
./queue-worker add ~/projects/my-app "Add input validation to /signup" --level committer

# Start the dashboard + runner
queue-worker-web
# open http://localhost:51002
```

Usage is read by calling the same `/api/organizations/{uuid}/usage` endpoint the claude.ai UI uses, authenticated with your sessionKey cookie. See [docs/usage-checking.md](docs/usage-checking.md) for how to grab the cookie and how recovery works when the API reports the account is between 5-hour windows.

---

## Features

| Area | What | Read more |
|---|---|---|
| **Usage-aware runner** | Chilling/burning state machine, reset-anchored scheduling, HTTP usage check via claude.ai cookie API | [runner-state-machine.md](docs/runner-state-machine.md), [usage-checking.md](docs/usage-checking.md) |
| **Per-project agent identity** | `.agent/` directory with AGENT/CONTEXT/BEHAVIOR + rolling memory (procedural / semantic / episodic), checkpoints, briefings, proposed memory edits | [agent-context.md](docs/agent-context.md) |
| **Capability boundaries** | Four levels (observer / craftsman / committer / deployer) compiled into ALLOWED / NOT ALLOWED sections of the injected `CLAUDE.md`; per-task overrides | [agent-context.md](docs/agent-context.md#capability-levels) |
| **Web dashboard** | FastAPI + Alpine.js SPA at `localhost:51002` — task CRUD, live structured trace of each `claude -p` run (tool calls, file reads/writes, cost), link to the on-disk session transcript, file browser, usage chart, logs viewer. Mobile-friendly (hamburger nav at ≤768px) | [web-dashboard.md](docs/web-dashboard.md) |
| **Auth + remote access** | Optional password gate with brute-force protection; instructions for Tailscale and Cloudflare Tunnel | [auth-and-remote-access.md](docs/auth-and-remote-access.md) |
| **CLI** | `queue-worker {init,add,ls,status,next,context,logs,retry,remove,compile,run,...}` | [cli.md](docs/cli.md) |
| **Crash recovery** | Per-task lock files, dead-PID detection on startup, CLAUDE.md backup restoration, durable reset anchor in `state/runner_state.json` | [runner-state-machine.md](docs/runner-state-machine.md#crash-recovery) |

---

## Repo layout

```
queue-up-for-claude/
├── .agent/                 ← this repo's own queue-worker context (worked example —
│                            run `queue-worker compile .` to see the CLAUDE.md it produces)
├── queue-worker            ← bash wrapper for the CLI
├── pyproject.toml          ← package metadata + entry points
├── config/profiles.yaml    ← capability level definitions
├── docs/                   ← per-feature documentation
├── src/queue_worker/       ← all Python code
│   ├── cli.py              ← Click CLI commands + .agent/ templates
│   ├── web.py              ← FastAPI dashboard + embedded runner thread
│   ├── runner.py           ← state machine + scheduling
│   ├── usage_check.py      ← dispatcher: kick recovery + CSV write
│   ├── usage_check_http.py ← HTTP backend (claude.ai cookie API)
│   ├── executor.py         ← claude -p subprocess lifecycle
│   ├── injector.py         ← CLAUDE.md builder + inject/cleanup
│   ├── queue_ops.py        ← task lifecycle, dependency resolution
│   ├── profiles.py         ← capability resolution
│   ├── task.py             ← Task dataclass + YAML I/O
│   ├── auth.py             ← password + session auth
│   ├── file_browser.py     ← file listing / preview helpers
│   ├── sessions.py         ← Claude Code transcript locator
│   ├── lock.py             ← per-task lock files
│   ├── logger.py           ← daily rolling logger
│   ├── config.py           ← paths + .env loader (private to process)
│   └── static/             ← dashboard SPA (single index.html + login.html)
└── tests/                  ← pytest unit tests
```

---

## Disclaimer

**This is unofficial software.** It is not affiliated with, endorsed by, or supported by Anthropic PBC.

queue-up-for-claude reads your Claude.ai plan usage by calling the same web API endpoints the claude.ai UI uses, authenticated with your `sessionKey` browser cookie.

**This may violate Anthropic's Terms of Service.** By using this software you accept that:

- Anthropic may block, restrict, or terminate your access at any time.
- Your Claude account could be affected by using unofficial usage-checking methods.
- Token usage from automated `claude -p` runs counts against your normal plan limits.
- **You use this at your own risk.** The author assumes no liability.

### Data storage

- The session key lives in `.env` (gitignored, `0600` recommended), in `~/.config/queue-worker/session_key` (mode `0600`), or in process env. **Never transmitted anywhere except claude.ai.**
- Usage history is appended to `usage_history.csv` in the project directory — timestamps + percentages only, no prompts or chat content.
- Task prompts and per-task `claude -p` output live in `queue/` and `logs/`.
- Strings sent to the dashboard pass through a redactor that strips `sk-ant-...` keys and email addresses (defense in depth).
- **No data is sent to the author and there is no telemetry.** Outbound traffic only goes to `claude.ai` (usage check, authenticated with your cookie) and Anthropic's API (whatever `claude -p` sends while running your tasks — prompts and the contents of any files it reads, same as using Claude Code directly).
- The dashboard's file browser API is **not path-scoped**: it can list and read any file the process user can read. Treat it as you would a remote shell — bind to loopback / Tailscale, set a password, do not expose publicly.

See [docs/security.md](docs/security.md) for the full security model and known limitations (notably: the file-browser API is not sandboxed — fine for loopback / Tailscale, not safe to expose publicly without scoping).

---

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

The HTTP usage backend mirrors the approach in [ClaudeMeter](https://github.com/eddmann/ClaudeMeter) by Edd Mann (a polished macOS menu-bar app for Claude.ai usage). The header set, `/api/organizations/{uuid}/usage` endpoint shape, and sessionKey cookie auth model were worked out there first; this project ports the read path to Python and wires it into a usage-aware job runner.
