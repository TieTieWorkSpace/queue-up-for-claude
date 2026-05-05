---
name: condense
description: "End-of-session: distill genuine learnings into the project's .agent/ memory without polluting it"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /condense — Distill session learnings into .agent/

The user is wrapping up a session. Take what was *actually learned* — not
what was done — and write it into the four-file `.agent/` core so the next
session inherits it. Most sessions produce little or nothing memory-worthy.
**An empty pass is a valid outcome.**

## Hard invariants

1. **Never commit.** Leave every edit in the working tree. Git diff is the
   user's review queue. Do not run `git add` or `git commit`.
2. **Never blind append.** Every write goes into a *section* — find the right
   one, merge or supersede an existing entry there. Only add a new section if
   no fit exists. Chronological append turns these files into a junk drawer.
3. **Read before writing.** Read every file you propose to touch (the canonical
   memory file or the most recent file of the same kind) before drafting.
4. **Bail loudly if `.agent/` is missing.** Don't scaffold partial structure.
   Tell the user to run `queue-worker init .` and stop.

## Step 1 — orient

```bash
test -d .agent || { echo "no .agent/ — run: queue-worker init ."; exit 1; }
ls .agent/
```

Then read in full:

- `.agent/ABOUT.md` — project + agent + rules. Rarely targeted by `/condense`,
  but you read it to understand context.
- `.agent/HOWTO.md` — commands. Targeted only when the session uncovered a new
  command or a wrong/missing one.
- `.agent/NOTES.md` — non-obvious facts + pitfalls. Primary `/condense` target
  for codebase learnings.
- `.agent/DECISIONS.md` — why-X + future plans + open questions. Primary target
  for decisions made or revisited this session.
- The most recent file in `.agent/log/` if any (style match for any new log entry).

## Step 2 — classify candidates

For each candidate learning from this session:

| Candidate | Goes to |
|---|---|
| Session narrative ("did X, hit Y, left Z unfinished") | `.agent/log/YYYY-MM-DD.md` |
| Non-obvious codebase fact ("looks like dead code but isn't") | `NOTES.md` (relevant section) |
| Pitfall / gotcha | `NOTES.md` `## Pitfalls` section (mandatory section header) |
| New how-to (test/build/deploy/setup) | `HOWTO.md` (relevant section) |
| Wrong how-to that needs correction | `HOWTO.md` (supersede the wrong entry) |
| Decision made or revisited | `DECISIONS.md` with status field |
| Open question / future plan | `DECISIONS.md` with `status: proposed` |
| Open thread / unfinished work | Today's `log/<date>.md` `## Open threads` section, or queue a follow-up via `/queue` |
| User preference learned today | NOWHERE in `.agent/` — wrong layer (user memory, not project memory) |
| Episodic record (id, status, duration, tokens) | NOWHERE — runner writes `log/tasks.jsonl` itself |

### Pitfall promotion test

> Would another agent doing similar work step on this same landmine?

- **Yes** → add to `NOTES.md` `## Pitfalls` section AND mention briefly in the log.
- **No** → log only.

### Decision recording

A decision earns a `DECISIONS.md` entry only if it's *durable* — would survive
into next session and someone might re-litigate it. Use the file's existing
template (date, status, context, decision, why, rejected alternatives). Match
the existing entries' tone and length.

## Step 3 — what NOT to save (the load-bearing filter)

Reject any candidate that fails any of these:

- **Already in the code.** If `grep` would find it, it's re-derivable. Don't save.
- **Already in the commit message** for changes made this session. Commit
  messages are the canonical context for that change; don't duplicate.
- **Process narration.** "I ran tests / read file X / explored Y" is not a
  learning.
- **Already in `NOTES.md` / `HOWTO.md` / `DECISIONS.md`.** You read them in
  Step 1 — if it's there, don't restate.
- **The user's task prompt.** Already in `queue/done/<task>.yaml` if this was
  a queue-worker task.
- **Session-specific trivia.** "User was debugging on a Saturday" has no
  future utility.
- **User preferences as project facts.** "User likes TDD" belongs in user-level
  memory, not in project NOTES.
- **Speculation / unconfirmed.** If you didn't verify it during the session,
  don't enshrine it.

When in doubt, drop it. It is cheaper to miss a lesson than to pollute memory.

## Step 4 — section discipline (the rule that prevents drift)

For every write, follow this order:

1. **Open the target file.** For log entries, today's date file if it exists,
   otherwise create.
2. **Find the right section** by reading existing headers. Examples in the
   current `NOTES.md`: `## Things that look like dead code but aren't`,
   `## Lock semantics`, `## Pitfalls`. Examples in `HOWTO.md`: `## Run tests`,
   `## Lint`. Examples in `DECISIONS.md`: each entry is its own dated `##` block.
3. **Decide: merge, supersede, or new entry.**
   - **Merge:** the existing entry already covers the topic — extend it inline.
   - **Supersede:** the existing entry is now wrong — replace it (in
     `DECISIONS.md`, update the previous entry's status to `superseded` and
     add a new entry that links via `Supersedes:`).
   - **New entry:** only if no existing entry covers the topic.
4. **Only as a last resort, add a new section header.** New sections
   proliferate fast and hurt navigation. If you find yourself wanting a new
   section, first ask whether an existing section can absorb it.

## Step 5 — frontmatter (when creating a new file)

If you create a new file (e.g., today's `log/<date>.md`), it must start with:

```yaml
---
abstract: "One or two sentences. Surfaces in the next session's CLAUDE.md."
---
```

The abstract names *the kind of work* ("Refactored auth into AuthService;
switched JWT lib"), not vague ("worked on auth"). A missing abstract degrades
the next CLAUDE.md silently. Always include it.

When *editing* `NOTES.md`, `HOWTO.md`, or `DECISIONS.md`, keep their existing
abstracts up to date if the edit substantively changes the file's character —
e.g., NOTES gaining a Pitfalls section earned the abstract a `+ pitfalls`
mention.

## Step 6 — write, do not commit, then report

Write all files. Then tell the user, briefly:

- What was written, with paths.
- What was *not* written, and why ("decided X wasn't generalizable;
  user-preference Y belongs in user memory").
- That nothing was committed — `git diff` shows the changes for their review.

If nothing was worth writing, say so plainly: "No durable learnings from this
session — common when the work was reading or routine fixes." Do not write an
empty log entry.

## Naming

- `log/YYYY-MM-DD.md` — ISO date.
- `log/tasks.jsonl` — runner-only; do not touch.

## Self-check before reporting back

- Did I read `NOTES.md`, `HOWTO.md`, and `DECISIONS.md` before writing?
- Every write went into an identified section (no blind append)?
- Every candidate ran through the "what NOT to save" list?
- For pitfalls: did I apply the generalizability test?
- For decisions: did I include status + why + rejected alternatives where they
  apply?
- I did not commit?
- The log entry is concrete enough to save the next agent grep time?
