"""Tests for _extract_json_from_text() and _richness_key() — FIX-212 deterministic tie-breaking."""
import json
import pytest


def _extract():
    """Lazy import to let conftest stub heavy deps first."""
    from agent.loop import _extract_json_from_text
    return _extract_json_from_text


def _key():
    from agent.loop import _richness_key
    return _richness_key


# --- Tier 1: fenced JSON block ---

def test_fenced_json_block():
    text = '```json\n{"tool":"read","path":"/x.md"}\n```'
    result = _extract()(text)
    assert result == {"tool": "read", "path": "/x.md"}


def test_fenced_json_invalid_fallthrough():
    text = '```json\n{invalid}\n```\n{"tool":"list","path":"/"}'
    result = _extract()(text)
    assert result is not None
    assert result["tool"] == "list"


# --- Tier 2: mutation tools ---

def test_mutation_preferred_over_report():
    text = (
        '{"tool":"write","path":"/x.md","content":"hello"} '
        '{"current_state":"done","plan_remaining_steps_brief":[],"task_completed":true,'
        '"function":{"tool":"report_completion","message":"ok"}}'
    )
    result = _extract()(text)
    assert result["tool"] == "write"


def test_mutation_deterministic_two_writes():
    """FIX-212: two mutations in same tier → richest (most keys) wins, not text order."""
    obj_a = {"tool": "write", "path": "/a.md", "content": "a"}
    obj_b = {"tool": "write", "path": "/b.md", "content": "b", "extra": "field"}
    text = json.dumps(obj_a) + " " + json.dumps(obj_b)
    result = _extract()(text)
    # obj_b has more keys → preferred by _richness_key
    assert result["path"] == "/b.md"


# --- Tier 3: bare tool (non-mutation) ---

def test_bare_tool_preferred_over_full_nextstep():
    text = (
        '{"tool":"search","pattern":"test","root":"/"} '
        '{"current_state":"searching","plan_remaining_steps_brief":["search"],'
        '"task_completed":false,"function":{"tool":"read","path":"/x"}}'
    )
    result = _extract()(text)
    assert result["tool"] == "search"
    assert "current_state" not in result


# --- Tier 4: full NextStep non-report ---

def test_full_nextstep_non_report():
    text = (
        '{"current_state":"reading","plan_remaining_steps_brief":["read"],'
        '"task_completed":false,"function":{"tool":"read","path":"/x"}}'
    )
    result = _extract()(text)
    assert result["current_state"] == "reading"
    assert result["function"]["tool"] == "read"


# --- Tier 5: full NextStep with report_completion ---

def test_full_nextstep_report():
    text = (
        '{"current_state":"done","plan_remaining_steps_brief":[],'
        '"task_completed":true,"function":{"tool":"report_completion","message":"ok"}}'
    )
    result = _extract()(text)
    assert result["function"]["tool"] == "report_completion"


# --- Tier 7: fallback to first/richest ---

def test_fallback_richest():
    text = '{"a":1} {"a":1,"b":2,"c":3}'
    result = _extract()(text)
    assert result == {"a": 1, "b": 2, "c": 3}


# --- Empty / garbage ---

def test_empty_returns_none():
    assert _extract()("") is None


def test_garbage_returns_none():
    assert _extract()("no json here at all") is None


# --- Req_XXX prefix injection ---

def test_req_prefix_injects_tool():
    text = 'Req_Read({"path":"/x.md"})'
    result = _extract()(text)
    assert result is not None
    assert result.get("tool") == "read"


# --- _richness_key tests ---

def test_richness_more_keys_preferred():
    key = _key()
    small = {"tool": "read", "path": "/x"}
    large = {"tool": "read", "path": "/x", "extra": "field", "another": "key"}
    assert key(large) < key(small)


def test_richness_full_nextstep_preferred():
    key = _key()
    full = {"current_state": "x", "function": {"tool": "read"}, "task_completed": False}
    bare = {"tool": "read", "path": "/x"}
    assert key(full) < key(bare)


def test_richness_non_report_preferred():
    key = _key()
    read_step = {"current_state": "x", "function": {"tool": "read"}, "task_completed": False}
    report_step = {"current_state": "x", "function": {"tool": "report_completion"}, "task_completed": True}
    assert key(read_step) < key(report_step)
