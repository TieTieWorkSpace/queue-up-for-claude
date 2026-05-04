"""Unit tests for executor's stream-json parser and claude argv builder.

The runner spawns `claude -p --output-format stream-json --verbose <prompt>`
and pipes stdout line-by-line through _StreamParser. These tests pin both
sides of that contract:
  - parser renders each event type to a stable, grep-friendly trace line
  - parser tracks usage / cost / session id even when killed before `result`
  - argv construction obeys session-id rules (resume reuses, fresh pins)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import pytest

from queue_worker.executor import _StreamParser, _build_cmd
from queue_worker.task import Task, TaskBudget, CapsOverride


# ── Helpers ─────────────────────────────────────────────────────────────────

def _line(event: dict) -> str:
    return json.dumps(event)


def _make_task(session_id: Optional[str] = None) -> Task:
    return Task(
        id='demo-20260503-aaaa', created='2026-05-03T00:00:00+00:00',
        dir='/tmp/proj', prompt='hello', level='craftsman',
        yaml_path='/tmp/queue/pending/demo.yaml',
        resolved_dir='/tmp/proj',
        session_id=session_id,
        budget=TaskBudget(), caps_override=CapsOverride(),
    )


# ── _build_cmd ─────────────────────────────────────────────────────────────

def test_build_cmd_fresh_pins_session_id_and_streams_json():
    cmd = _build_cmd(_make_task(session_id=None), 'do thing',
                     '11111111-1111-1111-1111-111111111111')
    assert cmd[0] == 'claude'
    assert '--resume' not in cmd
    assert '--session-id' in cmd
    sid_idx = cmd.index('--session-id')
    assert cmd[sid_idx + 1] == '11111111-1111-1111-1111-111111111111'
    assert '--output-format' in cmd and 'stream-json' in cmd
    assert '--verbose' in cmd
    assert '--dangerously-skip-permissions' in cmd
    # prompt is positional → must be the last element
    assert cmd[-1] == 'do thing'


def test_build_cmd_resume_does_not_add_session_id():
    """When task.session_id is set, --resume <id> alone reuses the session.
    Adding --session-id alongside --resume is redundant at best and may
    conflict in some CLI versions — keep them mutually exclusive."""
    cmd = _build_cmd(_make_task(session_id='abc'), 'do thing',
                     'should-not-appear')
    assert '--resume' in cmd and 'abc' in cmd
    assert '--session-id' not in cmd
    assert 'should-not-appear' not in cmd
    assert cmd[-1] == 'do thing'


def test_build_cmd_resume_keeps_stream_flags():
    cmd = _build_cmd(_make_task(session_id='abc'), 'go', 'abc')
    assert '--output-format' in cmd and 'stream-json' in cmd
    assert '--verbose' in cmd


# ── _StreamParser: per-event rendering ──────────────────────────────────────

def test_init_event_renders_session_model_and_tools():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'system', 'subtype': 'init',
        'session_id': 'abcd1234-0000-0000-0000-000000000000',
        'data': {'model': 'claude-opus-4-7', 'tools': ['Bash', 'Read']},
    }))
    assert out == ['· session=abcd1234 model=claude-opus-4-7 tools=2']


def test_init_event_top_level_fields_fallback():
    """Tolerate older CLI schemas that put model/tools top-level instead of
    under data — defensive parsing per the plan."""
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'system', 'subtype': 'init',
        'session_id': 'abc', 'model': 'claude-x', 'tools': ['A'],
    }))
    assert out == ['· session=abc model=claude-x tools=1']


def test_init_event_session_id_mismatch_warns():
    p = _StreamParser(expected_session_id='aaaaaaaa-0000-0000-0000-000000000000')
    out = p.feed(_line({
        'type': 'system', 'subtype': 'init',
        'session_id': 'bbbbbbbb-0000-0000-0000-000000000000',
        'data': {'model': 'm', 'tools': []},
    }))
    assert len(out) == 2
    assert 'session_id mismatch' in out[1]


def test_assistant_text_block_emits_one_line_per_source_line():
    """Multi-line assistant text must split — the dashboard greps each log
    line by [task:<id>] and would lose continuation lines otherwise."""
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'assistant',
        'message': {'content': [
            {'type': 'text', 'text': 'first line\n\nthird line'},
        ]},
    }))
    assert out == ['[assistant] first line', '[assistant] third line']


def test_assistant_tool_use_bash_uses_colon_separator():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'assistant',
        'message': {'content': [
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash',
             'input': {'command': 'ls -la /tmp'}},
        ]},
    }))
    assert out == ['→ Bash: ls -la /tmp']
    # tool_use_id is mapped for later tool_result labeling — assert through
    # behavior rather than reaching into _tool_names directly.
    follow = p.feed(_line({
        'type': 'user',
        'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok'},
        ]},
    }))
    assert follow == ['← Bash [1L, 2C] ok']


def test_assistant_tool_use_read_renders_path_and_extras():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'assistant',
        'message': {'content': [
            {'type': 'tool_use', 'id': 't', 'name': 'Read',
             'input': {'file_path': '/x.py', 'offset': 100, 'limit': 50}},
        ]},
    }))
    assert out == ['→ Read /x.py [offset=100, limit=50]']


def test_assistant_thinking_block_truncates():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'assistant',
        'message': {'content': [
            {'type': 'thinking', 'thinking': 'x' * 500},
        ]},
    }))
    assert out[0].startswith('[thinking] ')
    assert out[0].endswith('…')
    assert len(out[0]) <= len('[thinking] ') + 200


def test_assistant_multiple_blocks_in_one_event_render_separately():
    """One assistant event can carry text + tool_use + parallel tool_uses;
    _format_event must emit one line per block rather than dropping any."""
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'assistant',
        'message': {'content': [
            {'type': 'text', 'text': 'thinking out loud'},
            {'type': 'tool_use', 'id': 'a', 'name': 'Read',
             'input': {'file_path': '/a'}},
            {'type': 'tool_use', 'id': 'b', 'name': 'Read',
             'input': {'file_path': '/b'}},
        ]},
    }))
    assert out == [
        '[assistant] thinking out loud',
        '→ Read /a',
        '→ Read /b',
    ]


def test_user_tool_result_labels_with_tool_name_and_size():
    p = _StreamParser()
    p.feed(_line({
        'type': 'assistant',
        'message': {'content': [
            {'type': 'tool_use', 'id': 'toolu_x', 'name': 'Bash',
             'input': {'command': 'echo hi'}},
        ]},
    }))
    out = p.feed(_line({
        'type': 'user',
        'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_x',
             'content': 'hi\nbye'},
        ]},
    }))
    assert out == ['← Bash [2L, 6C] hi']


def test_user_tool_result_marks_errors():
    p = _StreamParser()
    # Seed the tool name via a normal tool_use event rather than poking
    # internals — keeps the test honest about the public contract.
    p.feed(_line({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 't', 'name': 'Bash', 'input': {'command': 'x'}},
    ]}}))
    out = p.feed(_line({
        'type': 'user',
        'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 't',
             'content': 'oh no', 'is_error': True},
        ]},
    }))
    assert out == ['← ERROR Bash [1L, 5C] oh no']


def test_user_string_content_is_dropped_as_kick_prompt():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'user',
        'message': {'content': 'You have been started by queue-worker...'},
    }))
    assert out == []


def test_result_event_emits_summary_and_latches_tokens_and_cost():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'result', 'subtype': 'success',
        'usage': {'input_tokens': 1000, 'output_tokens': 500,
                  'cache_read_input_tokens': 2500,
                  'cache_creation_input_tokens': 100},
        'total_cost_usd': 0.04231, 'num_turns': 3, 'duration_ms': 8210,
    }))
    assert out == [
        '✓ success · turns=3 in=1,000 out=500 cache=2,500r/100w '
        '· $0.0423 · 8210ms'
    ]
    assert p.tokens_used == 1500
    assert p.cost_usd == pytest.approx(0.04231)


def test_result_failure_renders_x_mark():
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'result', 'subtype': 'error_max_turns',
        'usage': {}, 'num_turns': 50, 'duration_ms': 10,
    }))
    assert out[0].startswith('✗ error_max_turns')


def test_tokens_used_fallback_from_assistant_usage_when_no_result():
    """Timeout / SIGKILL kills the subprocess before the result event; the
    last assistant event's running usage totals are the authoritative
    fallback. Without this, every timed-out task records tokens_used=None."""
    p = _StreamParser()
    p.feed(_line({
        'type': 'assistant',
        'message': {
            'content': [{'type': 'text', 'text': 'partial'}],
            'usage': {'input_tokens': 800, 'output_tokens': 200},
        },
    }))
    # No result event emitted — process killed.
    assert p.tokens_used == 1000
    assert p.cost_usd is None


def test_subagent_lines_get_indent_prefix():
    """parent_tool_use_id != null means we're inside an Agent/Task subagent;
    indent the line so the trace doesn't interleave unreadably."""
    p = _StreamParser()
    out = p.feed(_line({
        'type': 'assistant',
        'parent_tool_use_id': 'toolu_outer',
        'message': {'content': [
            {'type': 'tool_use', 'id': 'inner', 'name': 'Bash',
             'input': {'command': 'pwd'}},
        ]},
    }))
    assert out == ['└ → Bash: pwd']


def test_decode_error_falls_through_as_raw_line():
    """`--verbose` may emit non-JSON debug noise on stderr (merged into
    stdout). Don't swallow it — surface raw."""
    p = _StreamParser()
    out = p.feed('not json at all')
    assert out == ['not json at all']


def test_blank_line_yields_no_output():
    p = _StreamParser()
    assert p.feed('') == []
    assert p.feed('   \n') == []


def test_unknown_event_type_renders_placeholder():
    p = _StreamParser()
    out = p.feed(_line({'type': 'mystery', 'subtype': 'foo'}))
    assert out == ['?? mystery:foo']


def test_error_event_is_surfaced():
    p = _StreamParser()
    out = p.feed(_line({'type': 'error', 'message': 'rate limited'}))
    assert out == ['! error rate limited']


def test_compact_boundary_renders_marker():
    p = _StreamParser()
    out = p.feed(_line({'type': 'system', 'subtype': 'compact_boundary'}))
    assert out == ['· compaction']


def test_hook_lifecycle_events_are_suppressed():
    """The user's local ~/.claude/settings.json hooks fire constantly and
    aren't useful per-task — opt-in via --include-hook-events (which we
    don't pass). Drop them silently."""
    p = _StreamParser()
    assert p.feed(_line({'type': 'system', 'subtype': 'hook_started'})) == []
    assert p.feed(_line({'type': 'system', 'subtype': 'hook_response'})) == []
    assert p.feed(_line({'type': 'system', 'subtype': 'hook_finished'})) == []


def test_rate_limit_event_renders_concisely():
    p = _StreamParser()
    out = p.feed(_line({'type': 'rate_limit_event', 'subtype': 'warn',
                        'message': 'approaching session limit'}))
    assert out == ['· rate_limit warn: approaching session limit']


def test_rate_limit_event_minimal():
    p = _StreamParser()
    out = p.feed(_line({'type': 'rate_limit_event'}))
    assert out == ['· rate_limit']


# ── Round-trip realism: a small but believable session ─────────────────────

def test_realistic_session_renders_in_order_and_captures_totals():
    p = _StreamParser()
    events = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'sid',
         'data': {'model': 'claude-opus-4-7', 'tools': ['Bash', 'Read']}},
        {'type': 'assistant', 'message': {
            'content': [
                {'type': 'text', 'text': 'Reading the file first.'},
                {'type': 'tool_use', 'id': 'r1', 'name': 'Read',
                 'input': {'file_path': '/etc/hosts'}},
            ],
            'usage': {'input_tokens': 50, 'output_tokens': 20},
        }},
        {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'r1',
             'content': '127.0.0.1 localhost\n::1 localhost'},
        ]}},
        {'type': 'assistant', 'message': {
            'content': [{'type': 'text', 'text': 'Done.'}],
            'usage': {'input_tokens': 120, 'output_tokens': 30},
        }},
        {'type': 'result', 'subtype': 'success',
         'usage': {'input_tokens': 120, 'output_tokens': 30},
         'total_cost_usd': 0.0012, 'num_turns': 2, 'duration_ms': 950},
    ]
    rendered: list[str] = []
    for ev in events:
        rendered.extend(p.feed(_line(ev)))
    assert rendered == [
        '· session=sid model=claude-opus-4-7 tools=2',
        '[assistant] Reading the file first.',
        '→ Read /etc/hosts',
        '← Read [2L, 33C] 127.0.0.1 localhost',
        '[assistant] Done.',
        '✓ success · turns=2 in=120 out=30 cache=0r/0w · $0.0012 · 950ms',
    ]
    assert p.tokens_used == 150
    assert p.cost_usd == pytest.approx(0.0012)
