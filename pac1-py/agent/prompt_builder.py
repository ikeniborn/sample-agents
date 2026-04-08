"""Dynamic system prompt addendum builder (FIX-NNN).

Called after task classification, before run_loop.
Generates a short task-specific guidance section using a lightweight LLM call.

Design:
  - Fail-open: returns "" on any error so the agent still runs with the base prompt.
  - Activated only for task types in _NEEDS_BUILDER (ambiguous/complex types where
    vault-specific context meaningfully changes strategy).
  - Plain-text output only — no JSON, short bullet list (3-6 items).
  - Token budget: 300 tokens by default (enough for 6 bullet points).
  - Temperature: uses 'classifier' ollama profile (deterministic, seed=1).
"""

from __future__ import annotations

import os

from .dispatch import call_llm_raw

# All task types benefit from vault-specific guidance.
_NEEDS_BUILDER: frozenset[str] = frozenset({
    "default", "think", "longContext", "lookup", "email", "inbox", "distill", "coder",
})

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

_BUILDER_SYSTEM = (
    "You are a prompt strategist for a file-system AI agent that manages a personal vault.\n"
    "Given the task and vault context, output 3–6 concise bullet points of ADDITIONAL\n"
    "instructions specific to THIS task. Focus on:\n"
    "  - Which folder(s) to start with\n"
    "  - Key risks or edge cases specific to this task\n"
    "  - Required output format or field names (if the task specifies them)\n"
    "Output plain text ONLY. No JSON. No preamble. Each line starts with a dash (-).\n"
    "\n"
    "## CRITICAL PRE-CHECK — evaluate BEFORE generating any guidance:\n"
    "If the task involves ANY of the following, output EXACTLY one bullet and stop:\n"
    "  - Calendar invites, meetings, events, scheduling → OUTCOME_NONE_UNSUPPORTED\n"
    "  - External CRM, external URL, or external API → OUTCOME_NONE_UNSUPPORTED\n"
    "  - Ambiguous, vague, truncated, or garbled request → OUTCOME_NONE_CLARIFICATION\n"
    "For these cases output: '- [SKIP] Task triggers immediate rejection. No vault exploration needed.'\n"
    "Do NOT suggest vault paths, file creation, or exploration for rejected tasks.\n"
    "\n"
    "## COUNTING / SCANNING rule:\n"
    "For tasks that count, aggregate, or filter records ('how many', 'find all X where',\n"
    "'list contacts that', 'which accounts'): ALWAYS suggest code_eval to scan files in bulk.\n"
    "Never suggest sequential one-by-one file reads — the agent may stop too early.\n"
    "Channel data (Telegram, Discord) lives in docs/channels/ — always suggest code_eval there.\n"
    "Account/contact scanning: suggest listing /accounts/ or /contacts/ FIRST to get the exact\n"
    "file list from the vault tree or list tool, then pass ALL returned paths to code_eval.\n"
    "NEVER hardcode a range like acct_001..acct_010 — there may be more files beyond 10.\n"
    "\n"
    "## PERSON NAME rule:\n"
    "If the task mentions a person by name (not email address, not company name):\n"
    "- First bullet MUST be: search contacts/ for that person's record to get their contact ID\n"
    "- Include that contact file in grounding_refs — missing it fails verification\n"
    "- Only AFTER reading the contact file proceed to related accounts/reminders\n"
    "Never suggest going directly to accounts/ or reminders/ for a person name lookup.\n"
    "Manager names (e.g. 'Müller Sophie', 'Laura Albrecht') live in contacts/ as mgr_XXX.json.\n"
    "When filtering accounts by manager name: use case-insensitive matching and check both\n"
    "name orders (e.g. 'Voigt Carsten' AND 'Carsten Voigt') in the code_eval filter logic."
)


def build_dynamic_addendum(
    task_text: str,
    task_type: str,
    agents_md: str,
    vault_tree: str,
    model: str,
    cfg: dict,
    max_tokens: int = 300,
) -> tuple[str, int, int]:
    """Return (addendum, in_tokens, out_tokens). addendum='' if skipped or failed.

    Args:
        task_text: Full task description.
        task_type: Classified task type from classifier.
        agents_md: Content of AGENTS.MD (vault semantics).
        vault_tree: Rendered vault tree text from prephase.
        model: LLM model identifier.
        cfg: Model config dict (used for provider + ollama options).
        max_tokens: Max tokens for the addendum (default 300).

    Returns:
        (addendum_text, input_tokens, output_tokens) — tokens are 0 if skipped/failed.
    """
    if task_type not in _NEEDS_BUILDER:
        return "", 0, 0

    user_msg = (
        f"TASK_TYPE: {task_type}\n"
        f"TASK: {task_text}\n"
        f"VAULT TREE:\n{vault_tree[:600]}\n"
    )
    if agents_md:
        user_msg += f"\nAGENTS.MD:\n{agents_md}"

    if _LOG_LEVEL == "DEBUG":
        print(f"[prompt_builder] calling LLM for type={task_type!r}, task={task_text[:60]!r}")

    tok: dict = {}
    try:
        raw = call_llm_raw(
            system=_BUILDER_SYSTEM,
            user_msg=user_msg,
            model=model,
            cfg=cfg,
            max_tokens=max_tokens,
            think=False,
            max_retries=1,
            plain_text=True,
            token_out=tok,
        )
        if not raw:
            if _LOG_LEVEL == "DEBUG":
                print("[prompt_builder] LLM returned empty, skipping addendum")
            return "", tok.get("input", 0), tok.get("output", 0)
        result = raw.strip()
        if _LOG_LEVEL == "DEBUG":
            print(f"[prompt_builder] addendum ({len(result)} chars):\n{result}")
        return result, tok.get("input", 0), tok.get("output", 0)
    except Exception as exc:
        print(f"[prompt_builder] failed ({exc}), continuing without addendum")
        return "", 0, 0
