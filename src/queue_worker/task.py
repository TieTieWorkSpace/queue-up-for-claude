from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml
from datetime import datetime, timezone

@dataclass
class TaskBudget:
    max_minutes: int = 120

@dataclass
class CapsOverride:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)

@dataclass
class Task:
    # ── required ──────────────────────────────────────────
    id: str
    created: str
    dir: str
    prompt: str
    level: str

    # ── set by parse_task(), not in yaml ──────────────────
    yaml_path: str        # absolute path to the .yaml file on disk
    resolved_dir: str     # dir with ~ expanded + realpath

    # ── optional task config ──────────────────────────────
    priority: int = 3                  # 1=critical, 2=high, 3=normal, 4=low, 5=idle
    depends_on: list[str] = field(default_factory=list)
    dry_run: bool = False
    tags: list[str] = field(default_factory=list)
    budget: TaskBudget = field(default_factory=TaskBudget)
    caps_override: CapsOverride = field(default_factory=CapsOverride)
    session_id: Optional[str] = None       # Claude session UUID to resume (claude --resume <id> -p ...)

    # ── scheduling (set at create time, immutable thereafter) ──
    run_policy: Optional[str] = None       # 'this_session' | 'next_session' | 'tonight'
    eligible_at: Optional[str] = None      # ISO8601 UTC; task skipped while now < eligible_at

    # ── written at task start ───────────────────────────
    started_at: Optional[str] = None

    # ── written at task finish ────────────────────────────
    finished_at: Optional[str] = None
    status: Optional[str] = None
    stall_reason: Optional[str] = None
    stall_detail: Optional[str] = None
    checkpoint_file: Optional[str] = None
    duration_minutes: Optional[float] = None
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None              # USD billed for this run (from result.total_cost_usd)
    actual_session_id: Optional[str] = None       # session UUID Claude used; locates ~/.claude/projects/<slug>/<uuid>.jsonl

    # ── written by human to resume from checkpoint ────────
    checkpoint_answer: Optional[str] = None
    resume_context: Optional[str] = None


def expand_path(p: str) -> str:
    """Expand ~ and resolve to absolute path."""
    return str(Path(p).expanduser().resolve())


def _normalize_ts(value) -> Optional[str]:
    """Coerce a YAML-loaded timestamp to an ISO8601 UTC string, or None.

    PyYAML parses unquoted ISO timestamps to `datetime` automatically. A
    hand-edited `eligible_at: 2026-04-20T10:00:00+00:00` therefore arrives as
    datetime, not str. Compare-as-string in resolve_run_order would crash.
    Our own writes are quoted and round-trip as strings, so in practice this
    only triggers for hand-edited YAML — still worth defending against.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat(timespec='seconds')
    return str(value)


def parse_task(yaml_path: str) -> Task:
    """
    Read a task YAML file and return a Task dataclass.
    Raises ValueError with a clear message if required fields are missing.
    yaml_path must be an absolute path.
    """
    with open(yaml_path, 'r') as f:
        raw: dict = yaml.safe_load(f) or {}

    required = ['id', 'dir', 'prompt', 'level']
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ValueError(f"{yaml_path}: missing required fields: {missing}")

    budget_raw = raw.get('budget', {}) or {}
    caps_raw   = raw.get('caps_override', {}) or {}

    return Task(
        id=raw['id'],
        created=raw.get('created', datetime.now(timezone.utc).isoformat()),
        dir=raw['dir'],
        prompt=raw['prompt'],
        level=raw['level'],
        yaml_path=yaml_path,
        resolved_dir=expand_path(raw['dir']),
        priority=int(raw.get('priority', 3)),
        depends_on=raw.get('depends_on') or [],
        dry_run=raw.get('dry_run', False) is True,
        tags=raw.get('tags') or [],
        budget=TaskBudget(
            max_minutes=budget_raw.get('max_minutes', 120),
        ),
        caps_override=CapsOverride(
            add=caps_raw.get('add') or [],
            remove=caps_raw.get('remove') or [],
        ),
        started_at=raw.get('started_at'),
        finished_at=raw.get('finished_at'),
        status=raw.get('status'),
        stall_reason=raw.get('stall_reason'),
        stall_detail=raw.get('stall_detail'),
        checkpoint_file=raw.get('checkpoint_file'),
        duration_minutes=raw.get('duration_minutes'),
        tokens_used=raw.get('tokens_used'),
        cost_usd=raw.get('cost_usd'),
        actual_session_id=raw.get('actual_session_id'),
        checkpoint_answer=raw.get('checkpoint_answer'),
        resume_context=raw.get('resume_context'),
        session_id=raw.get('session_id'),
        run_policy=raw.get('run_policy'),
        eligible_at=_normalize_ts(raw.get('eligible_at')),
    )


def augment_task(yaml_path: str, fields: dict) -> None:
    """
    Merge `fields` into an existing task YAML file in-place.
    Preserves all existing keys. Uses safe_dump to write.
    Filters out None — use `update_task_yaml` if you need to delete keys.

    Writes via tmp+rename so a crash mid-write (or a concurrent reader
    like GET /api/tasks/{id}) never sees a half-written YAML.
    """
    with open(yaml_path, 'r') as f:
        data: dict = yaml.safe_load(f) or {}
    data.update({k: v for k, v in fields.items() if v is not None})
    target = Path(yaml_path)
    tmp = target.with_suffix(target.suffix + '.tmp')
    with open(tmp, 'w') as f:
        yaml.dump(data, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)
    tmp.replace(target)


def update_task_yaml(yaml_path: str, set_fields: dict,
                     delete_fields: tuple[str, ...] = ()) -> list[str]:
    """Set and/or delete top-level keys in a task YAML. Returns a list of
    field names that actually changed (skipping no-op writes).

    Unlike augment_task, this respects None as "do not change" (use
    delete_fields to remove keys) and reports the diff for callers that
    need an audit trail (e.g. PATCH responses). Writes via temp+rename so
    a crash mid-write doesn't truncate the user's task. Caller is responsible
    for serializing against concurrent moves (queue_ops.PENDING_MUTATION_LOCK).
    """
    with open(yaml_path, 'r') as f:
        data: dict = yaml.safe_load(f) or {}
    changed: list[str] = []
    for key, value in set_fields.items():
        if value is None:
            continue
        if data.get(key) != value:
            data[key] = value
            changed.append(key)
    for key in delete_fields:
        if key in data:
            data.pop(key)
            changed.append(key)
    if not changed:
        return []
    target = Path(yaml_path)
    tmp = target.with_suffix(target.suffix + '.tmp')
    with open(tmp, 'w') as f:
        yaml.dump(data, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)
    tmp.replace(target)
    return changed


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def make_task_id(project_dir: str) -> str:
    """
    Generate a unique task ID: <project-slug>-<YYYYMMDD>-<4hex>
    e.g. saas-app-20260409-a3f1
    """
    import secrets
    slug = Path(project_dir).expanduser().name.lower()
    slug = ''.join(c if c.isalnum() else '-' for c in slug).strip('-')
    date_str = datetime.now().strftime('%Y%m%d')
    rand = secrets.token_hex(2)
    return f"{slug}-{date_str}-{rand}"
