# .agent/

Per-project agent memory and identity. Survives across sessions; gets
injected into the generated `CLAUDE.md` whenever an AI agent works on
this codebase.

This scaffold ships with [queue-up-for-claude](https://github.com/TieTieWorkSpace/queue-up-for-claude),
a usage-aware Claude Code job queue. It works **standalone** without the
queueing piece — drop it into any project to give an agent stable identity
and accumulating learnings:

- `queue-worker init <dir>` — scaffold this structure in any project.
- `queue-worker compile <dir>` — render a `CLAUDE.md` from the contents
  for use with interactive Claude Code.
- `/condense` skill — distill end-of-session learnings into `NOTES.md` and
  `DECISIONS.md` with section discipline.

## Files

| File | What | Who fills |
|---|---|---|
| `ABOUT.md` | Project + agent identity + rules | Human, once |
| `HOWTO.md` | Commands (test, lint, build, deploy, dev) | Human, edits over time |
| `NOTES.md` | Non-obvious facts and pitfalls | `/condense` mostly, hand-curated |
| `DECISIONS.md` | Why-X + future plans + open questions | `/condense` mostly, hand-curated |
| `log/<date>.md` | Daily session narrative | `/condense`, lazy |
| `log/tasks.jsonl` | Runner event log (gitignored) | Runner, lazy |
| `proposed/` | Agent edits awaiting human review | Lazy, opt-in |
| `inbox/` | Checkpoints + dry-run output from autonomous runs | Lazy |

## Conventions

- Every `.md` starts with a `--- abstract: "..." ---` frontmatter block.
  The injector surfaces this in every generated CLAUDE.md so the agent can
  decide which files to read in full. If missing, it silently falls back to
  the first paragraph.
- `/condense` follows **section discipline**: find the right section, merge
  or supersede an existing entry — never blind append. This keeps
  NOTES/DECISIONS from becoming chronological junk drawers.
- `DECISIONS.md` graduates to `decisions/NNNN-slug.md` once it passes ~400
  lines or you start wanting to link to a specific decision from a commit.
- `log/tasks.jsonl` is a runtime contract written by the queue-worker
  runner — do not hand-edit.

## Full reference

The canonical doc is at [`docs/agent-context.md`](../docs/agent-context.md)
(queue-up-for-claude repo).
