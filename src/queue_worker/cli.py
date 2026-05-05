import sys
import json
from pathlib import Path

import click

from .config import LOG_DIR, bootstrap as _bootstrap, get_logger as _log


@click.group()
def main():
    """queue-worker — autonomous Claude Code job queue."""
    _bootstrap()


# ── run ───────────────────────────────────────────────────────────────────────

@main.command()
@click.option('--once', is_flag=True, help='Drain the queue once and exit (bypasses usage check).')
def run(once):
    """Start the runner.

    Default mode: chilling with clock-aligned usage checks at every HH:00
    plus reset-anchored T-60 / T-10 / T+5 checks. When usage shows
    >=30% remaining AND <70 minutes to reset, transitions to burning and
    processes tasks until the reset time. During burning, a usage check
    runs after each task finishes.
    Use --once to drain the queue immediately, bypassing the state machine.
    """
    from .runner import start_runner
    start_runner(_log(), run_once=once)


# ── next ──────────────────────────────────────────────────────────────────────

@main.command('next')
@click.option('--json-out', 'as_json', is_flag=True, help='Output as JSON.')
def next_task(as_json):
    """Print the next task to run (highest priority, dependencies met)."""
    from .queue_ops import get_pending_tasks, resolve_run_order
    tasks = get_pending_tasks(_log())
    ordered = resolve_run_order(tasks, _log())
    if not ordered:
        click.echo('No tasks ready.') if not as_json else click.echo('null')
        sys.exit(1)
    task = ordered[0]
    if as_json:
        click.echo(json.dumps({
            'id': task.id, 'dir': task.dir, 'level': task.level,
            'priority': task.priority, 'prompt': task.prompt,
            'depends_on': task.depends_on, 'dry_run': task.dry_run,
            'tags': task.tags, 'budget': {'max_minutes': task.budget.max_minutes},
        }, indent=2))
    else:
        click.echo(f'ID:       {task.id}\nDir:      {task.dir}\nLevel:    {task.level}')
        click.echo(f'Priority: {task.priority}\nDry-run:  {task.dry_run}')
        click.echo(f'Prompt:   {task.prompt.strip()[:200]}')


# ── add ───────────────────────────────────────────────────────────────────────

@main.command()
@click.argument('project_dir')
@click.argument('prompt')
@click.option('-l', '--level', default='craftsman', show_default=True,
              type=click.Choice(['observer', 'craftsman', 'committer', 'deployer']))
@click.option('-p', '--priority', default=3, show_default=True,
              type=click.IntRange(1, 5), help='1=critical, 2=high, 3=normal, 4=low, 5=idle')
@click.option('--dry-run', is_flag=True)
@click.option('--depends-on', default='', help='Comma-separated task IDs.')
@click.option('--tag', default='', help='Comma-separated tags.')
@click.option('--max-minutes', type=click.IntRange(min=1), default=None, help='Timeout in minutes.')
@click.option('--session-id', default=None, help='Claude session UUID to resume when executing.')
def add(project_dir, prompt, level, priority, dry_run, depends_on, tag, max_minutes, session_id):
    """Add a task to the queue."""
    from .queue_ops import create_task
    deps = [x.strip() for x in depends_on.split(',') if x.strip()]
    tags = [x.strip() for x in tag.split(',') if x.strip()]
    task_id, out_path = create_task(project_dir, prompt, level, priority,
                                     dry_run, deps, tags, max_minutes,
                                     session_id=session_id)
    click.echo(f'Added task {task_id}')
    click.echo(f'  file:  {out_path}')
    click.echo(f'  level: {level}  |  priority: {priority}  |  dir: {project_dir}')


# ── begin / done / fail / stall ──────────────────────────────────────────────

@main.command()
@click.argument('task_id')
def begin(task_id):
    """Move a task from pending to running."""
    from .queue_ops import begin_task
    try:
        begin_task(task_id, _log())
        click.echo(f'Started {task_id}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command('done')
@click.argument('task_id')
def done_cmd(task_id):
    """Move a task from running to done."""
    from .queue_ops import complete_task
    try:
        complete_task(task_id, _log())
        click.echo(f'Completed {task_id}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command()
@click.argument('task_id')
@click.option('--detail', default='', help='Failure detail.')
def fail(task_id, detail):
    """Move a task from running to failed."""
    from .queue_ops import fail_task
    try:
        fail_task(task_id, detail, _log())
        click.echo(f'Failed {task_id}: {detail}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command()
@click.argument('task_id')
@click.option('--reason', required=True,
              type=click.Choice(['timeout', 'checkpoint', 'dry_run_complete', 'uncertain']))
@click.option('--detail', default='', help='Stall detail.')
def stall(task_id, reason, detail):
    """Move a task from running to unfinished."""
    from .queue_ops import stall_task
    try:
        stall_task(task_id, reason, detail, _log())
        click.echo(f'Stalled {task_id}: [{reason}] {detail}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


# ── retry / remove ───────────────────────────────────────────────────────────

@main.command()
@click.argument('task_id')
def retry(task_id):
    """Move a task from unfinished/ or failed/ back to pending/."""
    from .queue_ops import retry_task
    try:
        has_checkpoint = retry_task(task_id, _log())
        click.echo(f'Moved {task_id} -> pending/')
        if has_checkpoint:
            click.echo('  (checkpoint_answer preserved)')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command()
@click.argument('task_id')
@click.option('--force', is_flag=True, help='Skip confirmation prompt.')
def remove(task_id, force):
    """Remove a task from any queue."""
    from .queue_ops import remove_task
    if not force:
        click.confirm(f'Remove task {task_id}?', abort=True)
    try:
        queue_name = remove_task(task_id, _log())
        click.echo(f'Removed {task_id} (was in {queue_name}/)')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


# ── ls / status / context / logs ─────────────────────────────────────────────

@main.command('ls')
@click.option('--status', default=None, type=click.Choice(
    ['pending', 'running', 'done', 'unfinished', 'failed']))
def ls(status):
    """List tasks across all queues."""
    from .queue_ops import list_tasks
    STATUS_COLOR = {'pending': 'blue', 'running': 'yellow', 'done': 'green',
                    'unfinished': 'magenta', 'failed': 'red'}
    rows = list_tasks(status)
    if not rows:
        click.echo('Queue is empty.')
        return
    click.echo(f"{'ID':<36}  {'STATUS':<12}  {'LEVEL':<12}  {'PRI':<5}  {'DIR':<25}  NOTE")
    click.echo('-' * 100)
    for r in rows:
        s = r.get('_status', '?')
        color = STATUS_COLOR.get(s, 'white')
        d = str(r.get('dir', '?'))
        click.echo(f"{r.get('id','?'):<36}  {click.style(f'{s:<12}', fg=color)}  "
                   f"{r.get('level','?'):<12}  {str(r.get('priority','?')):<5}  "
                   f"{d[-23:] if len(d)>23 else d:<25}  {r.get('stall_reason','')}")


@main.command()
def status():
    """Show queue summary."""
    from .queue_ops import get_queue_counts
    for s, count in get_queue_counts().items():
        click.echo(f'  {s:<12} {count}')


@main.command()
@click.argument('task_id')
def context(task_id):
    """Print the full agent context for a task."""
    from .injector import render_task_context
    rendered = render_task_context(task_id)
    if rendered is None:
        raise click.ClickException(f'Task {task_id} not found')
    click.echo(rendered)


@main.command()
@click.option('--task', 'task_id', default=None, help='Filter by task ID.')
@click.option('--date', default=None, help='YYYY-MM-DD (default: today).')
def logs(task_id, date):
    """Show log output for today (or a specified date)."""
    from .logger import read_log_lines
    date_str, lines = read_log_lines(LOG_DIR, date=date, task_id=task_id)
    if not lines:
        click.echo(f'No log for {date_str}')
        return
    for line in lines:
        click.echo(line)


# ── compile / init ───────────────────────────────────────────────────────────

@main.command()
@click.argument('project_dir')
@click.option('-l', '--level', default='craftsman', show_default=True,
              type=click.Choice(['observer', 'craftsman', 'committer', 'deployer']))
def compile(project_dir, level):
    """Generate CLAUDE.md for a project for daytime interactive use."""
    from .task import Task, expand_path, now_iso, CapsOverride, TaskBudget
    from .injector import build_claude_md
    abs_dir = expand_path(project_dir)
    dummy = Task(id='[daytime-session]', created=now_iso(), dir=abs_dir,
                 prompt='[Daytime interactive session]', level=level,
                 yaml_path='', resolved_dir=abs_dir,
                 budget=TaskBudget(), caps_override=CapsOverride())
    out = Path(abs_dir) / 'CLAUDE.md'
    out.write_text(build_claude_md(dummy), encoding='utf-8')
    click.echo(f'CLAUDE.md written to {out}')


@main.command('init')
@click.argument('project_dir')
def init(project_dir):
    """Scaffold a .agent/ directory in a project repo.

    Creates four files at the root: ABOUT.md (identity + project + rules),
    HOWTO.md (commands), NOTES.md (learned facts + pitfalls), DECISIONS.md
    (why-we-chose-X + future plans). Subdirectories (log/, proposed/, inbox/)
    are created lazily by the runner or by /condense when first needed.
    """
    from .task import expand_path
    agent_dir = Path(expand_path(project_dir)) / '.agent'
    agent_dir.mkdir(parents=True, exist_ok=True)
    for name, tmpl in _TEMPLATES.items():
        full = agent_dir / name
        if full.exists():
            click.echo(f'  exists (skipping): {name}')
        else:
            full.write_text(tmpl, encoding='utf-8')
            click.echo(f'  created: {name}')
    gi = agent_dir / '.gitignore'
    if not gi.exists():
        gi.write_text(
            '# Runtime contract — do not commit\n'
            'log/tasks.jsonl\n'
            '# Lazily-created session output\n'
            'proposed/\n'
            'inbox/\n',
            encoding='utf-8',
        )
        click.echo('  created: .gitignore')
    readme = agent_dir / 'README.md'
    if not readme.exists():
        readme.write_text(_AGENT_README, encoding='utf-8')
        click.echo('  created: README.md')

    click.echo(f'\n.agent/ scaffolded in {agent_dir}')
    click.echo('\nNext steps:')
    click.echo('  1. Skim .agent/README.md (permanent reference for the .agent system)')
    click.echo('  2. Fill in ABOUT.md (start here) and HOWTO.md')
    click.echo('  3. Leave NOTES.md and DECISIONS.md mostly empty — /condense fills them over time')
    click.echo('  4. git add .agent/ && git commit -m "chore: add .agent/ context"')
    click.echo(f'  5. (Optional) queue-worker add {agent_dir.parent} "your first task" --level craftsman')


_TEMPLATES = {
    'ABOUT.md': '''\
---
abstract: "REPLACE THIS: one sentence on what this project is + one on the
           agent's role. Surfaces in every generated CLAUDE.md."
---

# About this project and agent

## Project
<!-- What this is, in 2-4 sentences:
     - product / purpose / scale (e.g. "B2B invoice SaaS, ~50k LOC, 3 engineers")
     - tech stack (language, framework, database, key libraries)
     - architecture (monorepo? module boundaries? data flow?) -->

## Agent
<!-- Who the agent is for this project (role + specialty + goals).
     Optional — leave blank if the default "competent engineer in this
     codebase" is fine. -->

- Role: <!-- e.g. "senior backend engineer" -->
- Specialty: <!-- e.g. "REST API design, PostgreSQL optimization" -->
- Goals: <!-- e.g. "ship tested code, follow existing patterns" -->

## Rules

### Always
<!-- Things the agent must do on every task -->
- [e.g. "Run tests before committing"]
- [e.g. "Use conventional commits (feat:, fix:, refactor:)"]

### Never
<!-- Hard prohibitions -->
- [e.g. "Never modify existing migration files"]
- [e.g. "Never push directly to main"]

### Code style
<!-- e.g. "TypeScript strict mode, no any. Single quotes. kebab-case files." -->

## Conventions
<!-- Non-obvious project conventions a newcomer would miss:
     - e.g. "API responses use { data, error } envelope"
     - e.g. "Migrations in prisma/migrations/ — never edit existing"
     - e.g. "Environment vars in .env.local; .env.production for prod" -->
''',

    'HOWTO.md': '''\
---
abstract: "REPLACE THIS: list key commands. e.g. 'Test: npm test. Lint: npm
           run lint. Build: npm run build. Dev: npm run dev (port 3000).'"
---

# How to do things

## Run tests
```
[e.g. npm test]
```
<!-- Notes: prerequisites, flags, single-test invocation -->

## Lint / format
```
[e.g. npm run lint && npm run lint:fix]
```

## Build
```
[e.g. npm run build]
```

## Run dev server
```
[e.g. npm run dev]
```
<!-- Port, env vars, prerequisites -->

## Deploy
```
[e.g. npm run deploy:staging]
```

## Database migrations
```
[e.g. npx prisma migrate dev]
```

## Other useful commands
<!-- Generate types, seed DB, run a single test, etc. -->
''',

    'NOTES.md': '''\
---
abstract: "Non-obvious facts and pitfalls about this project. Grows over
           time as /condense distills lessons from sessions. Hand-curated."
---

# Notes

Facts about this codebase that are not obvious from reading the code.
Each entry should answer: "What would I assume that is wrong, and what is the
truth?"

## Things that look like dead code but aren't
<!-- Decorator-registered handlers, test seams, JSON-shape compat fields, etc.
     Empty until you discover one. -->

## Pitfalls
<!-- Gotchas that bit you or the agent. The "would another agent step on this
     same landmine?" test gates entry here. Empty until something earns its way in. -->

## Architecture quirks
<!-- Non-obvious design choices: lock semantics, recovery flows, ordering
     constraints, security trade-offs. -->
''',

    'DECISIONS.md': '''\
---
abstract: "Why we chose what we chose. Open questions and future plans live
           here too, with status fields. Grows slowly."
---

# Decisions

Each entry: a date, a decision, a status, a short rationale. Statuses:
`proposed` (open question / future plan), `accepted` (decided + in effect),
`superseded` (replaced — link to the replacement), `rejected` (considered + dropped).

When this file passes ~400 lines or you find yourself wanting to link to a
specific decision from a commit, split into `decisions/NNNN-slug.md`.

---

## Template

### YYYY-MM-DD — short title
- **Status**: proposed | accepted | superseded | rejected
- **Context**: 1-2 sentences on what was at stake
- **Decision**: what we chose
- **Why**: 1-3 sentences. Mention rejected alternatives.
- **Supersedes / superseded by**: link if applicable

---

<!-- Real entries below. Delete this template block once you have one. -->
''',
}

_AGENT_README = '''\
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

## First-time setup

- [ ] Fill in `ABOUT.md` (start here)
- [ ] Fill in `HOWTO.md` (just the commands you actually use)
- [ ] Replace every `REPLACE THIS` abstract with a real one
- [ ] `git add .agent/ && git commit -m "chore: add .agent/ context"`
- [ ] (Optional, queueing only) `queue-worker add . "your first task" --level craftsman`

## Full reference

The canonical doc is at `docs/agent-context.md` in the queue-up-for-claude
repo: <https://github.com/TieTieWorkSpace/queue-up-for-claude/blob/main/docs/agent-context.md>
'''
