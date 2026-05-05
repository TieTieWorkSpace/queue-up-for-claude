---
abstract: "Why we chose what we chose. Playwright removal, kept dead-looking
           fields, held-line refactors. Open questions live here too."
---

# Decisions

Each entry has a date, status, context, decision, rationale (with rejected
alternatives where relevant). Statuses: `proposed` (open question / future
plan), `accepted` (decided + in effect), `superseded` (replaced — link to
replacement), `rejected` (considered + dropped).

**Graduation rule**: when this file passes ~400 lines or you want to link to
a specific decision from a commit message, split into `decisions/NNNN-slug.md`.

---

## 2026-05-01 — Drop Playwright fallback; HTTP-only usage backend

- **Status**: accepted (commit `8b935ea`)
- **Context**: Earlier the project had two backends — HTTP cookie API plus a
  Playwright/CDP fallback that drove a real Chrome instance to scrape the
  rendered usage page. The fallback covered Cloudflare-block edge cases the
  HTTP path couldn't handle.
- **Decision**: Removed the Playwright fallback entirely. `usage_check.py`
  shrank from 651 → 173 LOC. README install dropped from 5 steps to 3.
- **Why**: For a public repo, install friction matters more than edge-case
  recoverability. Dual-backend story complicated docs, install, and the
  dispatcher. The trade-off — no Cloudflare-block recovery — is documented
  in `docs/usage-checking.md` under "What this tool does NOT do."
- **Rejected alternative**: keep Playwright but move it behind an opt-in
  install extra (`pip install -e .[browser]`). Decided no: split-install
  introduces support burden that the project doesn't want.

## 2026-05-01 — Keep `Task.status` field even though no code reads it

- **Status**: accepted
- **Context**: Codex flagged `Task.status` as a write-only dataclass field —
  written by `complete_task` / `fail_task` / `stall_task` but never read.
- **Decision**: Keep it.
- **Why**: It carries `grep` value on disk. `grep "status: failed" queue/failed/`
  is a real operator workflow when something has gone wrong. The disk YAMLs
  are forward-compat anyway (`raw.get(...)` reads). Removing the field would
  be an invisible-to-users API break.

## 2026-05-01 — Hold the line on more shared-helper extractions

- **Status**: accepted
- **Context**: After extracting `queue_ops.list_tasks`, `injector.render_task_context`,
  and `logger.read_log_lines` to dedupe CLI/web, codex suggested several more
  candidates (small-scope dedups bordering on refactors).
- **Decision**: Stopped. Did not extract further.
- **Why**: The cleanup mandate was "remove dead code." Extracting more shared
  helpers would have changed module shape under the guise of cleanup, which
  the BEHAVIOR refactor-discipline rule explicitly prohibits ("refactors do
  not change behavior; if you find behavior worth changing during a refactor,
  split it into a separate commit"). When the extra extractions earn their
  weight (e.g., a third caller materializes), do them as their own commit.

## 2026-05-01 — Drop `UsageCheckResult.backend` and `RunnerState.last_check_backend`

- **Status**: accepted (commit `68025a1`)
- **Context**: Both fields had been kept post-Playwright-removal "for JSON-shape
  compat with anyone scripting against `/api/status`." Round-3 cleanup
  re-evaluated.
- **Decision**: Dropped both. Always-`'http'` values aren't carrying signal.
- **Why**: No consumer reads them — frontend doesn't render, no docs reference,
  no internal control flow branches on them. The "JSON-shape compat" argument
  was theoretical (no known external scripter). Verified pytest 132/132 + the
  persisted-state JSON load tolerates absent keys.
- **Supersedes**: the earlier "kept on purpose" note that previously lived in
  `semantic.md` (now `NOTES.md`).

---

## Open / future

### `RunnerState.tick_seconds` rename

- **Status**: proposed
- **Context**: `tick_seconds` parameter has only test callers using non-default
  values. Currently named generically; semantic.md notes it's a test seam.
- **Decision (proposed)**: Rename to `_tick_seconds_for_testing` — explicit
  about its sole purpose.
- **Why proposed**: Discoverability for future reviewers. Renaming makes the
  test-seam status visible at the call site, eliminating the "is this a knob
  I should expose?" question. Cost: zero behavior change, two-line patch.

### Static dashboard SPA audit

- **Status**: proposed
- **Context**: `static/index.html` is 62 KB of un-linted Alpine.js. Likely
  contains request-cancellation bugs and stale event listeners.
- **Decision (proposed)**: Defer until a user reports a concrete dashboard
  bug. Speculative refactor doesn't earn its weight.

### `.agent/` shape redesign → 0.2.0

- **Status**: in progress (this very session, 2026-05-04)
- **Context**: Round-1 design used cognitive-science taxonomy (procedural /
  semantic / episodic) + propose-and-review. Real workflow is interactive:
  discuss → work → condense. Names didn't earn their weight; `proposed/` and
  `dry-run/` were rarely used.
- **Decision**: Migrate to four-file core (ABOUT/HOWTO/NOTES/DECISIONS) with
  lazy `log/`, `proposed/`, `inbox/`. Plain-English filenames. Section-discipline
  rule baked into `/condense`.
- **Why**: Names a human grepping the folder cold can navigate. Gives design
  decisions an explicit home (this file). Drops `proposed/` from default
  scaffold since git diff is the review queue for interactive work.
