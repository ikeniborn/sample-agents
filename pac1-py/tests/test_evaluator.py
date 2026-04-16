"""Tests for evaluator/critic (FIX-218, Variant 2 — DSPy ChainOfThought).

Tests cover:
  - _build_eval_prompt: prompt structure by efficiency/skepticism level (preserved function)
  - evaluate_completion: LLM approval, rejection, fail-open (None/bad JSON/exception)
  - Parametrized efficiency levels

LLM is mocked via @patch("agent.dspy_lm.call_llm_raw").
DSPy 3.x expects JSON responses with output field names as keys:
  {"reasoning": "...", "approved_str": "yes/no", "issues_str": "...", "correction_hint": "..."}
"""
import json
import types
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Lazy importers
# ---------------------------------------------------------------------------

def _build_prompt():
    from agent.evaluator import _build_eval_prompt
    return _build_eval_prompt


def _evaluate():
    from agent.evaluator import evaluate_completion
    return evaluate_completion


def _make_report(outcome="OUTCOME_OK", message="Done", steps=None, done_ops=None):
    """Minimal report mock using SimpleNamespace."""
    return types.SimpleNamespace(
        outcome=outcome,
        message=message,
        completed_steps_laconic=steps or [],
        done_operations=done_ops or [],
    )


def _approved_json(**extra) -> str:
    """Return a DSPy-parseable JSON string for an approved verdict."""
    return json.dumps({
        "reasoning": "Task is completed correctly.",
        "approved_str": "yes",
        "issues_str": "",
        "correction_hint": "",
        **extra,
    })


def _rejected_json(issues: str, hint: str) -> str:
    """Return a DSPy-parseable JSON string for a rejected verdict."""
    return json.dumps({
        "reasoning": "There is a problem with the outcome.",
        "approved_str": "no",
        "issues_str": issues,
        "correction_hint": hint,
    })


# ---------------------------------------------------------------------------
# _build_eval_prompt — structure tests (no LLM, function preserved)
# ---------------------------------------------------------------------------

def test_build_prompt_contains_task_and_outcome():
    """User message always includes TASK and PROPOSED_OUTCOME."""
    fn = _build_prompt()
    report = _make_report(outcome="OUTCOME_OK", message="Task done")
    _, user_msg = fn(
        task_text="summarize the notes",
        task_type="think",
        report=report,
        done_ops=[],
        digest_str="",
        skepticism="mid",
        efficiency="low",
    )
    assert "TASK: summarize the notes" in user_msg
    assert "PROPOSED_OUTCOME: OUTCOME_OK" in user_msg
    assert "AGENT_MESSAGE: Task done" in user_msg


def test_build_prompt_system_contains_evaluator_role():
    """System prompt identifies the evaluator role."""
    fn = _build_prompt()
    report = _make_report()
    system, _ = fn(
        task_text="test",
        task_type="default",
        report=report,
        done_ops=[],
        digest_str="",
        skepticism="low",
        efficiency="low",
    )
    assert "quality evaluator" in system.lower()


def test_build_prompt_efficiency_low_no_server_ops():
    """efficiency=low → SERVER_DONE_OPS is NOT included."""
    fn = _build_prompt()
    report = _make_report()
    _, user_msg = fn(
        task_text="read something",
        task_type="think",
        report=report,
        done_ops=["WRITTEN: /notes/foo.md"],
        digest_str="step1: read\nstep2: done",
        skepticism="low",
        efficiency="low",
    )
    assert "SERVER_DONE_OPS" not in user_msg
    assert "STEP_DIGEST" not in user_msg


def test_build_prompt_efficiency_mid_includes_server_ops():
    """efficiency=mid → SERVER_DONE_OPS and COMPLETED_STEPS are included."""
    fn = _build_prompt()
    report = _make_report(steps=["read file", "wrote result"])
    _, user_msg = fn(
        task_text="do something",
        task_type="default",
        report=report,
        done_ops=["WRITTEN: /notes/out.md"],
        digest_str="",
        skepticism="mid",
        efficiency="mid",
    )
    assert "SERVER_DONE_OPS" in user_msg
    assert "WRITTEN: /notes/out.md" in user_msg
    assert "COMPLETED_STEPS" in user_msg
    assert "read file" in user_msg
    assert "STEP_DIGEST" not in user_msg


def test_build_prompt_efficiency_high_includes_digest():
    """efficiency=high → also includes STEP_DIGEST when non-empty."""
    fn = _build_prompt()
    report = _make_report()
    _, user_msg = fn(
        task_text="analyse everything",
        task_type="think",
        report=report,
        done_ops=[],
        digest_str="Step 1: Listed /vault\nStep 2: Read /contacts/alice.md",
        skepticism="high",
        efficiency="high",
    )
    assert "STEP_DIGEST" in user_msg
    assert "Listed /vault" in user_msg


def test_build_prompt_account_evidence_mid():
    """account_evidence is included at efficiency=mid and above."""
    fn = _build_prompt()
    report = _make_report()
    _, user_msg = fn(
        task_text="process inbox",
        task_type="inbox",
        report=report,
        done_ops=[],
        digest_str="",
        skepticism="mid",
        efficiency="mid",
        account_evidence='{"company": "GreenGrid Energy"}',
    )
    assert "ACCOUNT_DATA" in user_msg
    assert "GreenGrid Energy" in user_msg


def test_build_prompt_inbox_evidence_mid():
    """inbox_evidence is included at efficiency=mid."""
    fn = _build_prompt()
    report = _make_report()
    _, user_msg = fn(
        task_text="process inbox",
        task_type="inbox",
        report=report,
        done_ops=[],
        digest_str="",
        skepticism="mid",
        efficiency="mid",
        inbox_evidence="From: supplier@co.com\nPlease update my account.",
    )
    assert "INBOX_MESSAGE" in user_msg
    assert "supplier@co.com" in user_msg


# ---------------------------------------------------------------------------
# evaluate_completion — mock agent.dspy_lm.call_llm_raw
# DSPy 3.x expects JSON with output field names
# ---------------------------------------------------------------------------

@patch("agent.dspy_lm.call_llm_raw")
def test_evaluate_completion_approval(mock_llm):
    """LLM returns approved_str=yes → EvalVerdict.approved is True."""
    mock_llm.return_value = _approved_json()
    fn = _evaluate()
    verdict = fn(
        task_text="summarize the file",
        task_type="think",
        report=_make_report(),
        done_ops=[],
        digest_str="",
        model="test-model",
        cfg={},
    )
    assert verdict.approved is True


@patch("agent.dspy_lm.call_llm_raw")
def test_evaluate_completion_rejection_with_issues(mock_llm):
    """LLM returns approved_str=no + issues → verdict carries them."""
    mock_llm.return_value = _rejected_json(
        issues="only 1 operation completed",
        hint="OUTCOME_NONE_CLARIFICATION",
    )
    fn = _evaluate()
    verdict = fn(
        task_text="delete all threads",
        task_type="default",
        report=_make_report(outcome="OUTCOME_OK", steps=["deleted 1 thread"]),
        done_ops=["DELETED: /threads/t1.md"],
        digest_str="",
        model="test-model",
        cfg={},
    )
    assert verdict.approved is False
    assert "only 1 operation" in verdict.issues[0]
    assert verdict.correction_hint == "OUTCOME_NONE_CLARIFICATION"


@patch("agent.dspy_lm.call_llm_raw")
def test_evaluate_completion_none_response_fail_open(mock_llm):
    """LLM returns None → DSPy parse fails → fail-open, approved=True."""
    mock_llm.return_value = None
    fn = _evaluate()
    verdict = fn(
        task_text="test",
        task_type="default",
        report=_make_report(),
        done_ops=[],
        digest_str="",
        model="test-model",
        cfg={},
    )
    assert verdict.approved is True


@patch("agent.dspy_lm.call_llm_raw")
def test_evaluate_completion_empty_string_fail_open(mock_llm):
    """LLM returns empty string → DSPy parse fails → fail-open, approved=True."""
    mock_llm.return_value = ""
    fn = _evaluate()
    verdict = fn(
        task_text="test",
        task_type="default",
        report=_make_report(),
        done_ops=[],
        digest_str="",
        model="test-model",
        cfg={},
    )
    assert verdict.approved is True


@patch("agent.dspy_lm.call_llm_raw")
def test_evaluate_completion_bad_json_fail_open(mock_llm):
    """LLM returns garbage → DSPy parse fails → fail-open, approved=True."""
    mock_llm.return_value = "Sorry, I cannot evaluate this task right now."
    fn = _evaluate()
    verdict = fn(
        task_text="test",
        task_type="default",
        report=_make_report(),
        done_ops=[],
        digest_str="",
        model="test-model",
        cfg={},
    )
    assert verdict.approved is True


@patch("agent.dspy_lm.call_llm_raw")
def test_evaluate_completion_exception_fail_open(mock_llm):
    """LLM raises exception → fail-open, approved=True."""
    mock_llm.side_effect = ConnectionError("network failure")
    fn = _evaluate()
    verdict = fn(
        task_text="test",
        task_type="default",
        report=_make_report(),
        done_ops=[],
        digest_str="",
        model="test-model",
        cfg={},
    )
    assert verdict.approved is True


# ---------------------------------------------------------------------------
# Parametrized: skepticism levels don't break prompt building
# ---------------------------------------------------------------------------

def test_build_prompt_skepticism_levels():
    """All three skepticism levels produce a non-empty system prompt."""
    fn = _build_prompt()
    for level in ("low", "mid", "high"):
        system, _ = fn(
            task_text="test",
            task_type="default",
            report=_make_report(),
            done_ops=[],
            digest_str="",
            skepticism=level,
            efficiency="low",
        )
        assert len(system) > 100, f"System prompt too short for skepticism={level}"
