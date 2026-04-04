"""FIX-218: Evaluator/critic — reviews agent completion before submission.

Intercepts ReportTaskCompletion before dispatch() sends vm.answer().
Uses a dedicated MODEL_EVALUATOR LLM to review outcome vs evidence.
Fail-open: any LLM/parse error → auto-approve (never blocks a working agent).
"""
import json
import re

from pydantic import BaseModel, Field

from .dispatch import call_llm_raw, CLI_YELLOW, CLI_CLR

# Bracket-extraction fallback for LLM responses wrapped in text/markdown
_JSON_BRACKET_RE = re.compile(r"\{[^{}]*\}")


class EvalVerdict(BaseModel):
    """Evaluator output schema."""
    approved: bool
    issues: list[str] = Field(default_factory=list)
    correction_hint: str = ""


_SKEPTICISM_DESC = {
    "low": (
        "Approve unless there is an obvious contradiction between the proposed "
        "outcome and the evidence. Err on the side of approval."
    ),
    "mid": (
        "Verify that the proposed outcome matches the evidence. Check for: "
        "truncated/vague task text that should be CLARIFICATION, incomplete "
        "operations (task says 'all' but done_ops shows fewer), wrong date "
        "calculations, mismatched outcome codes."
    ),
    "high": (
        "Assume the agent made a mistake. Actively search for errors in the "
        "evidence. Only approve if every detail checks out. Look for: missing "
        "operations, wrong calculations, outcome type mismatches, incomplete "
        "file mutations."
    ),
}

_EFFICIENCY_MAX_TOKENS = {"low": 256, "mid": 512, "high": 1024}


def _build_eval_prompt(
    task_text: str,
    task_type: str,
    report,          # ReportTaskCompletion (not imported to avoid circular)
    done_ops: list[str],
    digest_str: str,
    skepticism: str,
    efficiency: str,
) -> tuple[str, str]:
    """Build (system_prompt, user_message) for the evaluator LLM call.

    skepticism/efficiency: "low"|"mid"|"high" strings.
    digest_str: pre-built by caller via build_digest() — avoids circular import.
    """
    system = (
        "You are a quality evaluator for a file-system agent. "
        "Review the agent's proposed task completion BEFORE it is submitted.\n\n"
        f"Skepticism level: {_SKEPTICISM_DESC[skepticism]}\n\n"
        "Output ONLY valid JSON:\n"
        '{"approved": true/false, "issues": ["..."], "correction_hint": "..."}\n\n'
        "REJECTION TRIGGERS:\n"
        "- OUTCOME_OK but done_ops empty for a task that required file mutations\n"
        "- OUTCOME_OK but task text is truncated/vague/ambiguous (should be CLARIFICATION)\n"
        "- OUTCOME_NONE_CLARIFICATION but task has a clear action verb + identifiable target\n"
        "- Incomplete deletions: task says 'all' but done_ops shows fewer deletions\n"
        "- Date/math results that appear incorrect (e.g. wrong number of days)\n"
        "- done_operations in agent report vs server ledger mismatch\n\n"
        "IMPORTANT: Short email body ('Subj', 'Hi', single word) is VALID content "
        "when explicitly provided in the task — do NOT reject OUTCOME_OK for these.\n\n"
        "If approving, correction_hint MUST be empty string."
    )

    parts = [
        f"TASK: {task_text}",
        f"TYPE: {task_type}",
        f"PROPOSED_OUTCOME: {report.outcome}",
        f"AGENT_MESSAGE: {report.message}",
    ]

    if efficiency in ("mid", "high"):
        ops_str = "\n".join(f"  - {op}" for op in done_ops) if done_ops else "  (none)"
        parts.append(f"SERVER_DONE_OPS:\n{ops_str}")
        report_ops = getattr(report, "done_operations", []) or []
        if report_ops:
            r_ops_str = "\n".join(f"  - {op}" for op in report_ops)
            parts.append(f"AGENT_REPORTED_OPS:\n{r_ops_str}")
        steps_str = "\n".join(f"  - {s}" for s in report.completed_steps_laconic)
        parts.append(f"COMPLETED_STEPS:\n{steps_str}")

    if efficiency == "high" and digest_str:
        parts.append(f"STEP_DIGEST:\n{digest_str}")

    return system, "\n\n".join(parts)


def evaluate_completion(
    task_text: str,
    task_type: str,
    report,           # ReportTaskCompletion
    done_ops: list[str],
    digest_str: str,
    model: str,
    cfg: dict,
    skepticism: str = "mid",
    efficiency: str = "mid",
) -> EvalVerdict:
    """Call evaluator LLM and return verdict.

    Fail-open: returns EvalVerdict(approved=True) on any LLM or parse error.
    Uses call_llm_raw from dispatch.py (3-tier: Anthropic → OpenRouter → Ollama).

    Args:
        digest_str: pre-built by caller via build_digest() — avoids circular import.
        skepticism: "low"|"mid"|"high" — controls review strictness.
        efficiency: "low"|"mid"|"high" — controls context depth and token budget.
    """
    system, user_msg = _build_eval_prompt(
        task_text, task_type, report, done_ops, digest_str,
        skepticism, efficiency,
    )
    max_tok = _EFFICIENCY_MAX_TOKENS.get(efficiency, 512)

    try:
        raw = call_llm_raw(
            system, user_msg, model, cfg,
            max_tokens=max_tok, think=False, max_retries=1,
        )
        if not raw:
            print(f"{CLI_YELLOW}[evaluator] Empty LLM response — auto-approve{CLI_CLR}")
            return EvalVerdict(approved=True)
        # Try strict JSON first, then bracket-extraction fallback
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = _JSON_BRACKET_RE.search(raw)
            if not m:
                print(f"{CLI_YELLOW}[evaluator] No JSON found in response — auto-approve{CLI_CLR}")
                return EvalVerdict(approved=True)
            parsed = json.loads(m.group())
        return EvalVerdict.model_validate(parsed)
    except Exception as e:
        print(f"{CLI_YELLOW}[evaluator] Error ({e}) — auto-approve{CLI_CLR}")
        return EvalVerdict(approved=True)
