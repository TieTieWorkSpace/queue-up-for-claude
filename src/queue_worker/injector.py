import os
import json
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import yaml

from .task import Task
from .profiles import resolve_capabilities, build_caps_section


# Optional global base directory for shared agent identity
BASE_AGENT_DIR = Path.home() / '.agent-base'

# queue-worker project root (derived same way as cli.py)
QW_ROOT = Path(__file__).resolve().parents[2]


# ── Abstract extraction ────────────────────────────────────────────────────────

def extract_abstract(file_path: Path) -> str:
    """
    Extract the abstract for a file. Priority order:
    1. YAML frontmatter `abstract:` field
    2. First non-empty, non-heading paragraph (trimmed to 220 chars)
    3. '(no abstract — add frontmatter to this file)'
    """
    try:
        content = file_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return '(file not found)'
    except OSError:
        return '(could not read file)'

    # 1. YAML frontmatter
    if content.startswith('---'):
        end = content.find('---', 3)
        if end > 0:
            try:
                fm = yaml.safe_load(content[3:end])
                if isinstance(fm, dict) and fm.get('abstract'):
                    return str(fm['abstract']).strip()
            except yaml.YAMLError:
                pass

    # 2. First non-empty, non-heading text line(s)
    lines = content.splitlines()
    text_lines = [l.strip() for l in lines
                  if l.strip() and not l.startswith('#') and l.strip() != '---']
    if text_lines:
        snippet = ' '.join(text_lines[:2])
        return snippet[:220] + ('...' if len(snippet) > 220 else '')

    return '(no abstract — add `abstract:` to frontmatter)'


def extract_episodic_abstract(episodic_path: Path) -> str:
    """
    Read last 5 lines of log/tasks.jsonl and format a short summary.
    """
    try:
        from collections import deque
        with open(episodic_path, encoding='utf-8') as f:
            recent = deque(f, maxlen=5)
        if not recent:
            return '(no sessions recorded yet)'
        entries = []
        for line in recent:
            try:
                e = json.loads(line)
                ts = e.get('ts', '')[:10]
                tid = e.get('task_id', '?')
                status = e.get('status', '?')
                entries.append(f"{ts}: {tid} → {status}")
            except json.JSONDecodeError:
                pass
        summary = "Recent: " + ' | '.join(reversed(entries))
        return summary[:300]
    except FileNotFoundError:
        return '(no sessions recorded yet)'
    except OSError:
        return '(could not read tasks log)'


# ── Reference file definitions ─────────────────────────────────────────────────

AGENT_REF_FILES = [
    # (relative_path_in_agent_dir, section_label)
    ('ABOUT.md',      'Project + agent identity + rules'),
    ('HOWTO.md',      'How to do things (commands)'),
    ('NOTES.md',      'Non-obvious facts + pitfalls'),
    ('DECISIONS.md',  'Why-we-chose-X + open questions'),
]


def build_reference_section(agent_dir: Path) -> str:
    """
    Build the "## Context files" block.
    Each entry:
      ### <label>
      `<absolute path>`
      > <abstract>
    """
    lines = ['## Context files', '',
             'Read each file you need. Start with those relevant to your task.',
             'Use your file-reading tools — do not guess at file contents.', '']

    for rel, label in AGENT_REF_FILES:
        file_path = agent_dir / rel

        # Fall back to base agent dir if project file missing
        if not file_path.exists() and BASE_AGENT_DIR.exists():
            base_path = BASE_AGENT_DIR / rel
            if base_path.exists():
                file_path = base_path

        abstract = extract_abstract(file_path)
        lines += [
            f'### {label}',
            f'`{file_path}`',
            f'> {abstract}',
            '',
        ]

    # Recent session narratives — newest log/<date>.md, optional
    log_dir = agent_dir / 'log'
    if log_dir.is_dir():
        recent_logs = sorted(log_dir.glob('*.md'), reverse=True)[:1]
        if recent_logs:
            log_file = recent_logs[0]
            abstract = extract_abstract(log_file)
            lines += [
                '### Most recent session log',
                f'`{log_file}`',
                f'> {abstract}',
                '',
            ]

    # Task event log (runner-only, runtime contract)
    tasks_log = agent_dir / 'log' / 'tasks.jsonl'
    if tasks_log.exists():
        abstract = extract_episodic_abstract(tasks_log)
        lines += [
            '### Recent task history',
            f'`{tasks_log}`',
            f'> {abstract}',
            '',
        ]

    return '\n'.join(lines)


# ── Output conventions section ─────────────────────────────────────────────────

def build_output_conventions(task: Task, agent_dir: Path, caps) -> str:
    today_iso = datetime.now().strftime('%Y-%m-%d')
    today_compact = datetime.now().strftime('%Y%m%d')
    log_path = agent_dir / 'log' / f'{today_iso}.md'
    checkpoint_path = agent_dir / 'inbox' / 'checkpoints' / f'{today_compact}-HHMMSS.yaml'
    proposed_path = agent_dir / 'proposed' / f'notes-{today_compact}-HHMMSS.md'
    dryrun_path = agent_dir / 'inbox' / 'dryrun' / today_compact

    lines = [
        '## Output conventions — always follow these',
        '',
        f'**1. Session log (mandatory)**: Your final action MUST be writing `{log_path}`.',
        '   Do not exit without it. Append to today\'s log file if it exists; do not',
        '   overwrite. Format:',
        '   ```',
        '   ---',
        '   abstract: "One-sentence summary of this session."',
        '   ---',
        '   # Session — YYYY-MM-DD',
        '   task: <task_id>',
        '   status: done | partial | stalled',
        '',
        '   ## What I did',
        '   ## What I learned',
        '   ## Needs your attention',
        '   ```',
        '',
    ]

    if 'write_checkpoint' in caps:
        lines += [
            '**Checkpoint (when you hit a decision boundary)**:',
            f'   Write `{checkpoint_path}` (use actual timestamp) then halt.',
            '   Format:',
            '   ```yaml',
            '   question: "What decision do you need?"',
            '   options: [option_a, option_b, option_c]',
            '   agent_recommendation: option_a',
            '   context_summary: "What you completed and what is pending."',
            '   ```',
            '',
        ]

    if 'write_agent_proposed' in caps:
        lines += [
            '**Learning (when you discover a durable fact)**:',
            f'   Write `{proposed_path}` (use actual timestamp).',
            '   The human reviews and merges into NOTES.md or DECISIONS.md.',
            '   Never edit NOTES.md / DECISIONS.md directly unless `write_agent_direct`',
            '   is in your allowed capabilities.',
            '',
        ]
    elif 'write_agent_direct' in caps:
        lines += [
            '**Learning (when you discover a durable fact)**:',
            '   Edit NOTES.md (facts/pitfalls) or DECISIONS.md (why/future) directly.',
            '   Use section discipline: find the right section, merge or supersede an',
            '   existing entry — never blind append. Do not commit.',
            '',
        ]

    if task.dry_run and 'write_dryrun' in caps:
        lines += [
            '**DRY RUN MODE**: Do NOT apply any changes.',
            f'   Write all proposed changes as unified diffs to `{dryrun_path}/`.',
            '   Include a summary in the session log. Then write the log and halt.',
            '',
        ]

    return '\n'.join(lines)


# ── Main build function ────────────────────────────────────────────────────────

def build_claude_md(task: Task) -> str:
    """
    Assemble the full agent context.
    Uses reference links + abstracts — does NOT copy file content inline.
    Used by `context` (prints to conversation) and `compile` (writes CLAUDE.md).
    """
    agent_dir = Path(task.resolved_dir) / '.agent'
    caps = resolve_capabilities(task)

    sections: list[str] = []

    # Header
    sections.append(
        f'<!-- queue-worker context | task: {task.id} | '
        f'level: {task.level} | generated: {datetime.now().isoformat(timespec="seconds")} -->'
    )
    sections.append('')

    # Identity preamble
    sections.append('# Agent session')
    sections.append('')
    sections.append(
        'You are running autonomously via queue-worker. '
        'Your project context is in the files listed below. '
        'Read each file you need using your file-reading tools. '
        'Never guess at file contents — read them.'
    )
    sections.append('')

    # queue-worker CLI instructions
    qw_path = QW_ROOT / 'queue-worker'
    venv_activate = QW_ROOT / '.venv' / 'bin' / 'activate'
    sections.append('## queue-worker CLI')
    sections.append('')
    sections.append(f'The queue-worker CLI is available at `{qw_path}`.')
    sections.append('To use it in shell commands:')
    sections.append('')
    sections.append('```bash')
    sections.append(f'source {venv_activate} && queue-worker <command>')
    sections.append('```')
    sections.append('')
    sections.append('Useful commands: `queue-worker ls`, `queue-worker status`, '
                    '`queue-worker next`, `queue-worker logs`, '
                    '`queue-worker add <dir> "<prompt>"`, '
                    '`queue-worker init <dir>` (scaffold .agent/ in a new project).')
    sections.append(f'Full documentation: `{QW_ROOT / "README.md"}`')
    sections.append('')
    sections.append('---')
    sections.append('')

    # Reference files section
    sections.append(build_reference_section(agent_dir))

    sections.append('---')
    sections.append('')

    # Capabilities
    sections.append(f'## Automation level: {task.level}')
    sections.append('')
    sections.append(build_caps_section(caps))
    sections.append('')
    sections.append('---')
    sections.append('')

    # Output conventions
    sections.append(build_output_conventions(task, agent_dir, caps))

    sections.append('---')
    sections.append('')

    # Task (skip for daytime interactive sessions)
    if not task.id.startswith('[daytime-'):
        sections.append('## Your task')
        sections.append('')
        sections.append(task.prompt.strip())
        sections.append('')

    # Checkpoint resume
    if task.checkpoint_answer:
        sections.append('---')
        sections.append('')
        sections.append('## Resuming from checkpoint')
        sections.append('')
        sections.append(
            'A previous session paused and asked for human input. The answer is:'
        )
        sections.append('')
        sections.append(f'**{task.checkpoint_answer}**')
        if task.resume_context:
            sections.append('')
            sections.append(task.resume_context.strip())
        sections.append('')
        sections.append(
            'Read the most recent entry in `.agent/log/tasks.jsonl` and the checkpoint '
            'file in `.agent/inbox/checkpoints/` to understand exactly where the '
            'previous session stopped. Continue from there.'
        )
        sections.append('')

    return '\n'.join(sections)


# ── Inject / cleanup ───────────────────────────────────────────────────────────

@dataclass
class BackupInfo:
    had_original: bool
    backup_path: Optional[Path] = None


def inject_claude_md(project_dir: str, content: str) -> BackupInfo:
    """Write CLAUDE.md to project_dir, backing up any existing one (PID-stamped)."""
    claude_path = Path(project_dir) / 'CLAUDE.md'
    backup_path = Path(project_dir) / f'CLAUDE.md.queue-worker-bak-{os.getpid()}'
    had_original = claude_path.exists()

    if had_original:
        claude_path.rename(backup_path)

    claude_path.write_text(content, encoding='utf-8')
    return BackupInfo(had_original=had_original,
                      backup_path=backup_path if had_original else None)


def cleanup_claude_md(project_dir: str, backup: BackupInfo) -> None:
    """Delete injected CLAUDE.md and restore backup. Must run in a finally block."""
    claude_path = Path(project_dir) / 'CLAUDE.md'
    claude_path.unlink(missing_ok=True)
    if backup.had_original and backup.backup_path and backup.backup_path.exists():
        backup.backup_path.rename(claude_path)


def render_task_context(task_id: str) -> Optional[str]:
    """Render the CLAUDE.md a task would receive at execution time. Returns
    None if the task isn't found in any queue bucket."""
    from .queue_ops import find_task_yaml
    from .task import parse_task
    path = find_task_yaml(task_id)
    if not path:
        return None
    return build_claude_md(parse_task(str(path)))
