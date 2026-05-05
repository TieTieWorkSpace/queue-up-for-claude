# Agent context (`.agent/`)

`.agent/` is a per-project directory that gives an AI agent stable identity,
a project map, accumulated learnings, and decision history that survive across
sessions. The queue-worker runner injects its content into a generated
`CLAUDE.md` before each task — but **`.agent/` works standalone**: you can
use `queue-worker init` and `queue-worker compile` to scaffold and render
context for *any* project, without ever queueing tasks. Many users do exactly
this: drop `.agent/` into every project they work on and let the agent build
up memory across sessions, with the queue piece as an optional add-on for
unattended autonomous runs.

> **Worked example:** this repo eats its own dog food. The populated [`.agent/`](../.agent/)
> at the repo root is a real, non-template version of everything described
> below. Run `queue-worker compile .` from the repo root to see the `CLAUDE.md`
> it produces.

## Scaffold

```bash
queue-worker init ~/projects/my-app
```

Creates four content files plus a directory README and gitignore — that's it:

```
my-app/
└── .agent/
    ├── README.md             ← permanent inside-folder reference (GitHub renders this)
    ├── ABOUT.md              ← project + agent identity + rules (combined)
    ├── HOWTO.md              ← commands (test, lint, build, deploy, dev)
    ├── NOTES.md              ← non-obvious facts + pitfalls
    ├── DECISIONS.md          ← why-X + future plans + open questions
    └── .gitignore
```

`.agent/README.md` exists so a developer (or the agent itself) browsing the
folder cold knows what each file is for, without needing to clone the
queue-up-for-claude repo. It does **not** get injected into the generated
CLAUDE.md (that's `ABOUT.md`'s job); it's purely a directory README.

Subdirectories are **lazily created** — never by `init`:

- `log/<date>.md` — daily session narratives, written by `/condense` or the runner.
- `log/tasks.jsonl` — runner event log (id/status/duration/tokens). Runtime
  contract; gitignored. The runner writes this on first task completion.
- `proposed/` — agent edits awaiting human review. Created only if you opt
  into the propose-and-review flow (e.g., for unattended autonomous tasks).
- `inbox/` — checkpoints + dry-run output from autonomous runs.
  - `inbox/checkpoints/<ts>.yaml` — mid-task escalations.
  - `inbox/dryrun/<date>/` — proposed changes when a task has `dry_run: true`.

## YAML frontmatter

Every `.agent/*.md` file should have YAML frontmatter with an `abstract:`
field. The abstract is a 1-2 sentence summary that the injector surfaces in
every generated `CLAUDE.md` so the agent can decide which files to read in
full.

```markdown
---
abstract: "Senior backend engineer working on Acme SaaS. TypeScript/Node,
           PostgreSQL, Stripe integrations."
---

# About this project and agent
...
```

If the abstract is missing, `extract_abstract()` falls back to the file's
first non-heading paragraph (truncated to 220 chars). If even that is empty,
you get `(no abstract — add 'abstract:' to frontmatter)` in the generated
CLAUDE.md. Always include an abstract — degrading silently is the failure mode.

## The four core files

`init` writes templates with inline guidance — fill in the bracketed
placeholders.

### `ABOUT.md` — project + agent identity + rules

Combines what used to be three files (AGENT/CONTEXT/BEHAVIOR). Sections:

- `## Project` — what this is, tech stack, architecture, conventions. The
  "objective" layer.
- `## Agent` — role, specialty, goals. The "subjective" layer. Optional —
  leave blank if "competent engineer in this codebase" is the right default.
- `## Rules` — `### Always`, `### Never`, `### Code style`. The "prescriptive"
  layer.

Decision rule when something straddles boundaries: *Could a different person
reasonably do this work?* If yes, it's not Agent — it's Project or Rules.
*Is it a fact about reality?* Project. *Is it a constraint on action?* Rules.

If `ABOUT.md` grows past ~600 lines, split into `PROJECT.md`, `AGENT.md`,
`RULES.md` — but only when growth forces it, not preemptively.

### `HOWTO.md` — how to do things

Every command the agent might need, with its prerequisites and gotchas. Test,
lint, build, dev server, deploy, migrations, anything project-specific.

### `NOTES.md` — non-obvious facts + pitfalls

The "what would I assume that's wrong?" file. Sections:

- `## Things that look like dead code but aren't` — decorator-registered
  handlers, write-only fields kept on purpose, test seams, etc.
- `## Pitfalls` — gotchas that bit you or the agent. Mandatory section.
  Generalizability test gates entry: "would another agent step on this same
  landmine?" Yes → in. No → keep in `log/<date>.md` instead.
- Other sections as the project demands (lock semantics, recovery flows,
  security boundaries, etc.).

### `DECISIONS.md` — why we chose what we chose

One entry per durable decision. Fields: date, status, context, decision, why,
rejected alternatives. Statuses: `proposed` (open question / future plan),
`accepted`, `superseded`, `rejected`.

**Graduation rule**: when this file passes ~400 lines or you find yourself
wanting to link to a specific decision from a commit message, split into
`decisions/NNNN-slug.md`. Don't pre-split.

## Lazy directories

### `log/`

`log/<YYYY-MM-DD>.md` — daily session narrative. Written by the agent via
`/condense` at session end, or by the runner after each task. Pulled into
the next CLAUDE.md as "Most recent session log."

`log/tasks.jsonl` — structured runner event log. **Runtime contract**: every
line is `{ts, task_id, status, stall_reason, duration_min, tokens, cost}`.
Gitignored — like a build cache. Runner writes; injector reads recent lines
into the CLAUDE.md as "Recent task history."

### `proposed/`

Capability `write_agent_proposed` lets the agent write to `.agent/proposed/`.
Files here represent suggested edits to `NOTES.md` or `DECISIONS.md` — never
directly merged. You review and copy.

This is **opt-in**. The default interactive workflow is "discuss → work →
condense" with the human in the loop the entire time, so `git diff` already
serves as the review queue. `proposed/` earns its weight when the agent runs
unattended (autonomous burn-window tasks, Stop-hook auto-condense).

### `inbox/`

Mid-task output from autonomous runs:

- `inbox/checkpoints/<ts>.yaml` — agent stalls a task to ask a question:

  ```yaml
  question: "Stripe API key has two candidates in .env. Which should I use?"
  options:
    - STRIPE_SECRET_KEY (live)
    - STRIPE_SECRET_KEY_TEST
  context: "I need it for the refund flow in src/billing/refund.ts"
  ```

  If a checkpoint appears during execution, the executor stalls the task with
  `stall_reason: checkpoint` and moves it to `unfinished/`. You answer by
  editing the task YAML's `checkpoint_answer` field and `queue-worker retry`.

- `inbox/dryrun/<YYYYMMDD>/` — when a task has `dry_run: true`, the agent
  writes proposed changes here as unified diffs and halts.

## Section discipline (the rule that prevents drift)

When `/condense` (or you) writes to NOTES.md or DECISIONS.md, the load-bearing
rule is: **find the right section, merge or supersede an existing entry, only
add a new section if no fit exists**.

Without this, `/condense` degenerates into chronological append and these
files turn into junk drawers. The rule is enforced in the bundled `/condense`
SKILL.md.

## Capability levels

Levels live in `config/profiles.yaml` and are resolved at task spawn time by
`profiles.resolve_capabilities()`. The result is compiled into ALLOWED /
NOT ALLOWED markdown sections of the injected CLAUDE.md.

| Level | Read | Write | Shell | Git stage/commit | Git push | Deploy |
|-------|------|-------|-------|------------------|----------|--------|
| `observer` | ✓ | — | read-only | — | — | — |
| `craftsman` | ✓ | ✓ | ✓ | — | — | — |
| `committer` | ✓ | ✓ | ✓ | ✓ | — | — |
| `deployer` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

All levels can write to `log/`, `proposed/`, `inbox/checkpoints/`. `craftsman`
and above can write to `inbox/dryrun/`.

### Per-task overrides

```yaml
# in a task YAML
caps_override:
  add: [git_push]        # grant git push to a craftsman for this one task
  remove: [delete_files] # revoke file deletion from a committer
```

The override is applied after the base level's caps are resolved.

### Capability flags

Defined in `config/profiles.yaml`. Full set: `read_files`, `write_files`,
`delete_files`, `run_shell`, `run_shell_readonly`, `run_deploy_scripts`,
`git_read`, `git_stage_commit`, `git_push`, `net_packages`, `net_full`,
`write_agent_memory`, `write_agent_proposed`, `write_agent_direct`,
`write_briefing`, `write_checkpoint`, `write_dryrun`.

The output-conventions block of the injected CLAUDE.md is gated by these
caps — `proposed/` and `inbox/checkpoints/` paths only appear when the
relevant capability is granted.

## How CLAUDE.md is built and injected

For each task, `injector.build_claude_md(task)`:

1. Loads `ABOUT.md`, `HOWTO.md`, `NOTES.md`, `DECISIONS.md` and extracts each
   `abstract`.
2. Resolves capabilities and builds the ALLOWED / NOT ALLOWED section.
3. Pulls the most recent `log/<date>.md` abstract (if any).
4. Pulls a digest of recent `log/tasks.jsonl` entries (if any).
5. Renders the task prompt and any pending checkpoint answer.

Then `inject_claude_md(project_dir, content)`:

1. If the project already has a `CLAUDE.md`, copies it to a backup path.
2. Writes the generated content to `CLAUDE.md`.
3. Spawns `claude -p` against the project dir.
4. After the subprocess exits (or on crash recovery), `cleanup_claude_md`
   restores the backup so your interactive `CLAUDE.md` is never lost.

## Daytime use (no queued task)

```bash
queue-worker compile ~/projects/my-app --level craftsman
```

Generates a `CLAUDE.md` with the same identity / context / behavior /
capabilities, minus a task prompt — useful for interactive Claude Code
sessions where you want the same project conventions applied.

## Git hygiene

The scaffolded `.agent/.gitignore` excludes `log/tasks.jsonl` (runtime
contract; like a build cache), `proposed/`, and `inbox/`. **Commit** the four
core .md files plus `log/<date>.md` daily logs (your call — committing
gives history a paper trail; ignoring keeps the diff cleaner).

## Bundled skills

Two Claude Code skills ship in the repo at `skills/` — copy them into
`~/.claude/skills/` to use:

- **`/queue <prompt>`** — enqueue a follow-up task that will resume the
  current conversation when it runs.
- **`/condense`** — distill end-of-session learnings into `.agent/` with
  section discipline.
