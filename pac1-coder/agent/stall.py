"""Stall detection and recovery for the agent loop (FIX-74).

Extracted from loop.py to reduce God Object size.
Public API used by loop.py:
  _check_stall()        — detect stall signals, return hint or None
  _handle_stall_retry() — inject hint + call LLM once on stall (dependency-injected call_llm_fn)

Three task-agnostic stall signals:
  1. Same tool+args fingerprint 3× in a row → action loop
  2. Same path error ≥ 2× → path doesn't exist
  3. ≥ 6 steps without write/delete/move/mkdir → exploration stall (escalates at 12+)
"""
from collections import Counter, deque
from pathlib import Path as _Path

from .dispatch import CLI_YELLOW, CLI_CLR
from .log_compaction import _StepFact


def _check_stall(
    fingerprints: deque,
    steps_since_write: int,
    error_counts: Counter,
    step_facts: "list[_StepFact] | None" = None,
) -> str | None:
    """Detect stall patterns and return an adaptive, task-agnostic hint.

    Signals checked (in priority order):
    1. Last 3 action fingerprints are identical → stuck in action loop.
    2. Repeated error (same tool:path:code ≥ 2 times) → path doesn't exist.
    3. ≥ 6 steps without any write/delete/move/mkdir → stuck in exploration.
    Returns None if no stall detected."""
    # Signal 1: repeated identical action
    if len(fingerprints) >= 3 and fingerprints[-1] == fingerprints[-2] == fingerprints[-3]:
        tool_name = fingerprints[-1].split(":")[0]
        # Include recent exploration context in hint
        _recent = [f"{f.kind}({f.path})" for f in step_facts[-4:]] if step_facts else []
        _ctx = f" Recent actions: {_recent}." if _recent else ""
        return (
            f"You have called {tool_name} with the same arguments 3 times in a row without progress.{_ctx} "
            "Try a different tool, a different path, or use search/find with different terms. "
            "If the task is complete or cannot be completed, call report_completion."
        )

    # Signal 2: repeated error on same path
    for (tool_name, path, code), count in error_counts.items():
        if count >= 2:
            # Name the parent dir explicitly in hint
            _parent = str(_Path(path).parent)
            return (
                f"Error {code!r} on path '{path}' has occurred {count} times — path does not exist. "
                f"List the parent directory '{_parent}' to see what files are actually there, "
                "then use the exact filename from that listing."
            )

    # Signal 3: long exploration without writing
    if steps_since_write >= 6:
        # Include explored dirs/files from step_facts in hint
        _listed = [f.path for f in step_facts if f.kind == "list"][-5:] if step_facts else []
        _read_f = [f.path for f in step_facts if f.kind == "read"][-3:] if step_facts else []
        _explored = ""
        if _listed:
            _explored += f" Listed: {_listed}."
        if _read_f:
            _explored += f" Read: {_read_f}."
        # FIX-276: escalation after 12+ steps — force code_eval or report
        if steps_since_write >= 12:
            return (
                f"[STALL ESCALATION] You have been exploring for {steps_since_write} steps without action.{_explored} "
                "Either: (1) Use code_eval to analyze data and determine the answer/fix, or "
                "(2) Report OUTCOME_NONE_CLARIFICATION if you cannot determine what to do. "
                "Do NOT continue reading the same files."
            )
        return (
            f"You have taken {steps_since_write} steps without writing, deleting, moving, or creating anything.{_explored} "
            "Either take a concrete action now (write/delete/move/mkdir) "
            "or call report_completion if the task is complete or cannot be completed."
        )

    return None


def _handle_stall_retry(
    job,
    log: list,
    model: str,
    max_tokens: int,
    cfg: dict,
    fingerprints: deque,
    steps_since_write: int,
    error_counts: Counter,
    step_facts: "list[_StepFact]",
    stall_active: bool,
    call_llm_fn,  # injected: _call_llm from loop.py — avoids circular import
) -> tuple:
    """Check for stall and issue a one-shot retry LLM call if needed.
    Returns (job, stall_active, retry_fired, in_tok, out_tok, elapsed_ms, ev_c, ev_ms).
    retry_fired is True when a stall LLM call was made (even if it returned None).
    Token/timing deltas reflect the retry call when it fired."""
    _stall_hint = _check_stall(fingerprints, steps_since_write, error_counts, step_facts)
    if _stall_hint and not stall_active:
        print(f"{CLI_YELLOW}[stall] Detected: {_stall_hint[:120]}{CLI_CLR}")
        # FIX-200: record stall event as step fact for compaction survival
        step_facts.append(_StepFact(kind="stall", path="", summary=_stall_hint[:100]))
        log.append({"role": "user", "content": f"[STALL HINT] {_stall_hint}"})
        stall_active = True
        _job2, _e2, _i2, _o2, _, _ev_c2, _ev_ms2 = call_llm_fn(log, model, max_tokens, cfg)
        log.pop()
        if _job2 is not None:
            return _job2, stall_active, True, _i2, _o2, _e2, _ev_c2, _ev_ms2
        # LLM retry fired but returned None — still count the call, keep original job
        return job, stall_active, True, _i2, _o2, _e2, _ev_c2, _ev_ms2
    return job, stall_active, False, 0, 0, 0, 0, 0
