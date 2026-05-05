import yaml
from pathlib import Path
from .task import Task

# Set by config.bootstrap() at CLI / web startup
PROFILES_PATH: Path = None  # type: ignore[assignment]

# Human-readable descriptions of each flag
CAP_ALLOWED_TEXT: dict[str, str] = {
    'read_files':          'Read any file in the project directory',
    'write_files':         'Create and modify files',
    'delete_files':        'Delete files',
    'run_shell_readonly':  'Run read-only shell commands (ls, cat, grep, find, diff)',
    'run_shell':           'Run shell commands (tests, build, lint, package install)',
    'run_deploy_scripts':  'Run scripts tagged as deployment operations',
    'git_read':            'git log, status, diff, show',
    'git_stage_commit':    'git add, commit, branch, stash, merge (local only)',
    'git_push':            'git push, create pull requests',
    'net_packages':        'Install packages via pip, npm, cargo, brew',
    'net_full':            'Any outbound network request',
    'write_agent_memory':  'Append events to .agent/log/tasks.jsonl',
    'write_agent_proposed':'Propose NOTES.md / DECISIONS.md edits to .agent/proposed/',
    'write_agent_direct':  'Directly edit .agent/NOTES.md or .agent/DECISIONS.md',
    'write_briefing':      'Write .agent/log/YYYY-MM-DD.md session log (mandatory)',
    'write_checkpoint':    'Write .agent/inbox/checkpoints/TIMESTAMP.yaml and halt',
    'write_dryrun':        'Write proposed changes to .agent/inbox/dryrun/YYYYMMDD/',
}

# Boundary rules: what to do instead of a disallowed action
CAP_BOUNDARY_TEXT: dict[str, str] = {
    'git_push':         'Do not push. Write a checkpoint describing what you want to push.',
    'net_full':         'Do not make arbitrary network calls. Write a checkpoint if you need external data.',
    'write_agent_direct':'Do not edit NOTES.md / DECISIONS.md directly. Write proposals to .agent/proposed/ instead.',
    'run_deploy_scripts':'Do not run deploy scripts. Write a checkpoint describing what you want to deploy.',
    'delete_files':     'Do not delete files. Write a checkpoint asking whether to delete.',
}


def load_profiles() -> dict[str, list[str]]:
    """Load profiles.yaml. Returns {profile_name: [cap_flags]}."""
    with open(PROFILES_PATH) as f:
        data = yaml.safe_load(f)
    return {name: cfg['caps'] for name, cfg in data['profiles'].items()}


def resolve_capabilities(task: Task) -> set[str]:
    """
    1. Load base caps for task.level from profiles.yaml
    2. Apply caps_override.add
    3. Apply caps_override.remove
    Returns final set of capability flag strings.
    """
    profiles = load_profiles()
    if task.level not in profiles:
        raise ValueError(f"Unknown profile: {task.level!r}. "
                         f"Valid: {list(profiles)}")
    caps = set(profiles[task.level])
    caps |= set(task.caps_override.add or [])
    caps -= set(task.caps_override.remove or [])
    return caps


def build_caps_section(caps: set[str]) -> str:
    """
    Build the ALLOWED / NOT ALLOWED section for CLAUDE.md.
    """
    all_flags = set(CAP_ALLOWED_TEXT)
    allowed = [CAP_ALLOWED_TEXT[f] for f in sorted(all_flags) if f in caps]
    not_allowed_flags = [f for f in sorted(CAP_BOUNDARY_TEXT) if f not in caps]

    lines = ['### Allowed', '']
    for a in allowed:
        lines.append(f'- {a}')

    if not_allowed_flags:
        lines += ['', '### Not allowed — do this instead', '']
        for f in not_allowed_flags:
            lines.append(f'- {CAP_BOUNDARY_TEXT[f]}')

    lines += [
        '',
        'If you are about to take any action not in the Allowed list, **stop immediately**.',
        'Write a checkpoint file to `.agent/inbox/checkpoints/` and halt. Do not improvise.',
    ]
    return '\n'.join(lines)
