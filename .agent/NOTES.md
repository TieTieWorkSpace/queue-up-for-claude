---
abstract: "Non-obvious facts and pitfalls. Intentional design choices that
           look like dead code, lock rationales, security choices, recovery
           flows, codex-tooling gotchas."
---

# Notes

Facts about this codebase that aren't obvious from reading the code, organized
by where they bite. Each entry should answer: "What would I assume that's
wrong, and what's the truth?"

## Things that look like dead code but aren't

- **Click commands and FastAPI route handlers** are decorator-registered.
  `grep` for the function name often returns zero callers. They are not dead.
  Look for `@main.command(...)`, `@app.get/post/patch/delete(...)`,
  `@click.command(...)`.
- **`Task.status` field** (in `task.py`) is written by `complete_task` /
  `fail_task` / `stall_task` but never read by any code. It's kept on disk
  for `grep "status: failed" queue/failed/` value when a human is debugging.
  Codex flagged it as drop-able; we kept it. Don't re-flag.
- **`tick_seconds` parameter in `start_runner`** has no production override.
  Kept as a test seam — `test_run_policy.py` and others rely on overriding it.
- **`RunnerState` diagnostic fields** (`last_anchor_at`, `last_anchor_kind`,
  `pre_reset_done_for_cycle`, `t_minus_60_done_for_cycle`) are exposed in the
  `/api/status` JSON but not consumed by the bundled dashboard. Useful for
  ad-hoc debugging via `curl /api/status` when reset-anchor behavior looks
  off — keep them. The persistence allowlist `_PERSISTED_FIELDS` deliberately
  excludes the ones that should re-derive on restart.

## Lock semantics

- `_state_lock` (runner.py): protects `_runner_state` mutations. Held briefly.
- `_usage_check_lock` (runner.py): serializes usage checks. NOT for thread-safety
  of urllib (urllib is fine concurrent). It's there to prevent two concurrent
  `claude -p "hi"` kicks (would burn a kick on an already-active session) AND
  to prevent CSV row interleave. Keep it.
- `_execute_lock` (runner.py): serializes `claude -p` task execution across
  the background runner, "Run All (once)", and "Run This Task". One task at
  a time, ever. The runner acquires this BEFORE selecting the next task to
  prevent web-triggered runs from sniping it.

## Burn decision flow (subtle)

The chain is:
1. `usage_check.py:check_usage_once()` calls `fetch_usage_http`, returns a
   `UsageCheckResult`. Sets a `status` string ("Chilling" / "NEED TO BURN
   TOKEN !" / "ERROR:..."). **The `status` is just a label.**
2. `runner.py:do_usage_check()` receives the result and computes the *real*
   burn decision via `_effective_burn_minutes(scrape, anchor) → min` and the
   `usage_left >= BURN_USAGE_THRESHOLD_PCT and eff_min < BURN_RESET_WINDOW_MIN`
   check. Anchor-aware so a stale anchor can't fire a burn fresh data
   contradicts.

If you ever feel tempted to consume `result.status == "NEED TO BURN TOKEN !"`
in code, **don't**. Recompute from `pct` + `reset_minutes` + anchor.

A previous session removed `UsageCheckResult.should_burn` for exactly this
reason — it was a write-only field that lied about the actual burn decision.

## Single source of truth

- `BURN_USAGE_THRESHOLD_PCT = 30` and `BURN_RESET_WINDOW_MIN = 70` live in
  `usage_check.py` and are imported by `runner.py`. Don't re-hardcode 30 or 70
  anywhere. (A previous version had them re-hardcoded in the runner — codex
  caught it.)

## Recovery flows

- **`between_sessions`**: `usage_check_http.py` raises `BetweenSessions` (with
  `code = 'between_sessions'`). The dispatcher in `usage_check.py:check_usage_once`
  catches at the `error_code` level, runs `_kick()` which spawns
  `claude -p "hi"` (KICK_CLI_BIN, KICK_CLI_TIMEOUT_S = 60s), settles
  `POST_KICK_SETTLE_S = 3.0` seconds, then re-fetches. Exactly one CSV row
  is written regardless. The runner sees no error if recovery succeeded.
- **`session_key_invalid`**: NO recovery. User must rotate the cookie.
  Surfaced as an error in the dashboard.
- **`cloudflare_blocked`**: NO recovery (the Playwright fallback that previously
  handled this is gone). Runner stays chilling until the next scheduled check.

## Subprocess env hygiene

`config.subprocess_env()` filters out queue-worker-private env keys
(`CLAUDE_SESSION_KEY`, `QUEUE_WORKER_PASSWORD`) so child `claude -p` processes
don't inherit them. The `.env` loader in `config.py` is private — values from
`.env` are NOT exported into `os.environ` for the same reason. **Always use
`get_env()` instead of `os.environ.get()` to read queue-worker config.**

## File browser quirks

- **SVG is intentionally classified as `text`** in `file_browser.py:classify_file`,
  not `image`. Stored-XSS prevention — same-origin SVG with `<script>` is a
  vector if any task writes one into its workspace. Tests assert this; don't
  "fix" it.
- **Unknown extensions classify as `text`**, then the binary detector in
  `read_text_file` (null-byte probe in first 8 KB) catches actual binaries
  with `reason: 'binary'`. There's no `'unsupported'` classification anymore.
- **`/api/files/*` is not sandboxed** — `..` works, arbitrary absolute paths
  work. Documented in `docs/security.md`. Fine on loopback / Tailscale, NEVER
  expose to the public internet without scoping.

## Forward-compat YAML

`task.parse_task` reads every optional field via `raw.get(...)`. Adding new
optional fields is safe: old YAMLs deserialize as `None` for the new field.
**Removing** a field is also safe: old YAMLs that have the field just have
the value silently ignored. **Renaming** a field requires a migration path.

## Reset anchor model

- Persisted to `state/runner_state.json` as `next_reset_at` (epoch seconds).
- Survives restart — runner re-arms the pre/post_reset slot queue from the
  persisted anchor.
- Re-anchor decisions are sanity-banded ±15 min (`ANCHOR_DRIFT_TOLERANCE_S`).
  Wildly different proposals are rejected, so a single noisy fetch can't
  repaint truth.
- Past-due anchor → "clamp" action: clear the anchor + re-anchor as cold_start.
  The `_decide_anchor_action` returns `'skip'` when proposed is None, so the
  clamp branch always carries a non-None proposed (don't add `if proposed is
  not None` guards inside the clamp block — codex flagged that as dead).

## Test-suite quirks

- `pytest_mock` is in dev deps but not all tests use it; some do raw
  `monkeypatch`. Both fine.
- `tests/test_dotenv_isolation.py` uses `sk-ant-test-fake-...` string fixtures.
  Gitleaks correctly skips them by length/entropy.
- `tests/test_run_policy.py` has fixtures that use `'cloudflare_blocked'` as
  the canonical "transient error" example — `'Chrome unavailable'` was a
  Playwright-era code that no longer exists.

## Git remote / push

- Remote: `git@github.com:TieTieWorkSpace/queue-up-for-claude.git` (SSH).
- HTTPS doesn't work — the gh-cli token expired and hasn't been re-auth'd.
- The harness blocks direct pushes to `main` without explicit user
  confirmation. Asking the user is the right move; don't try to bypass.

## Pitfalls

Gotchas that have bitten past sessions. Generalizability test gates entry
here ("would another agent step on this same landmine?").

- **`codex exec` OOM-kills on large codebases with `model_reasoning_effort=high`
  + `--enable web_search_cached`.** Exit code 137 = SIGKILL = OOM. Reproduced
  twice on the round-3 cleanup attempt. Mitigation: use `medium` effort and
  skip web_search for surveys; reserve `high` for tightly-scoped diff reviews
  where the input is a few hundred lines.
- **Function signature changes that span CLI + web + runner** ship `TypeError`
  to the unchecked surface if you only `grep` two of three. A previous cleanup
  removed `check_interval_seconds` from `start_runner()` but `web.py:_background_runner`
  still passed it. Lesson: when changing a signature used by multiple entry
  points, grep all of them — codex's second pass caught this one.
- **Stale comments referencing removed features** (Playwright, HH:55 schedule,
  `--fork-session`, `os.environ.copy()`) have shipped to main multiple times
  because the cleanup focused on code, not adjacent prose. Always grep
  comment-text for removed feature names after a rip-out.
