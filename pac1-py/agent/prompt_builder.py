"""Dynamic system prompt addendum builder — DSPy Predict (FIX-NNN, Variant 1).

Uses dspy.Predict(PromptAddendum) to generate a short task-specific guidance
section before run_loop. Replaces the hand-crafted _BUILDER_SYSTEM prompt with
a DSPy Signature whose docstring is optimised by COPRO (see optimize_prompts.py).

Design:
  - Fail-open: returns ("", 0, 0) on any error so the agent runs with base prompt.
  - Activated only for task types in _NEEDS_BUILDER.
  - Plain-text output only — no JSON, short bullet list (3-6 items).
  - Token budget: 300 tokens by default (enough for 6 bullet points).
  - Compiled program loaded from data/prompt_builder_program.json if present.
  - Uses dspy.context(lm=...) — no global DSPy state side-effects.
"""
from __future__ import annotations

import os
from pathlib import Path

import dspy

from .dspy_lm import DispatchLM

# preject skipped — single-step immediate rejection, no vault guidance needed.
_NEEDS_BUILDER: frozenset[str] = frozenset({
    "default", "queue", "capture", "crm", "temporal",
    "lookup", "email", "inbox", "distill",
})

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

_DATA = Path(__file__).parent.parent / "data"
_PROGRAM_PATH = _DATA / "prompt_builder_program.json"


def _get_program_path(task_type: str) -> Path:
    """Return per-type program path if it exists, else global fallback."""
    per_type = _DATA / f"prompt_builder_{task_type}_program.json"
    return per_type if per_type.exists() else _PROGRAM_PATH


# ---------------------------------------------------------------------------
# DSPy Signature
# ---------------------------------------------------------------------------

class PromptAddendum(dspy.Signature):
    """You are a prompt strategist for a file-system AI agent that manages a personal vault.
    Given the task and vault context, generate 3–6 bullet points of ADDITIONAL instructions
    specific to THIS task. Bullet 1: which folder to open first. Bullet 2: key risk.
    Bullet 3+: required output format or field names if the task specifies them.
    No preamble — token budget is 300, preamble wastes it. Each bullet starts with a dash (-).

    ## Rejection Rules
    Evaluate BEFORE generating any guidance. If the task involves ANY of the following,
    output EXACTLY one bullet and stop:
      - Calendar invites, meetings, events, scheduling → `OUTCOME_NONE_UNSUPPORTED`
      - External CRM, external URL, or external API → `OUTCOME_NONE_UNSUPPORTED`
      - Ambiguous, vague, truncated, or garbled request → `OUTCOME_NONE_CLARIFICATION`
    For these cases output: '- [SKIP] Task triggers immediate rejection. No vault exploration needed.'
    Do NOT suggest vault paths, file creation, or exploration for rejected tasks —
    it implies the task is actionable, which misleads the main loop.

    ## Bulk Scanning
    For tasks that count, aggregate, or filter records ('how many', 'find all X where',
    'list contacts that', 'which accounts'): ALWAYS suggest `code_eval` to scan files in bulk.
    Never suggest sequential one-by-one file reads — the agent may stop too early.
    Channel data (Telegram, Discord) lives in `docs/channels/` — always suggest `code_eval` there.
    Account/contact scanning: suggest listing `/accounts/` or `/contacts/` FIRST to get the exact
    file list from the vault tree or list tool, then pass ALL returned paths to `code_eval`.
    NEVER hardcode a range like `acct_001..acct_010` — vault may contain acct_011..acct_099;
    missed files cause wrong counts.

    ## Person Lookup
    If the task mentions a person by name (not email address, not company name):
    - First bullet MUST be: search `contacts/` for that person's record to get their contact ID
    - Only AFTER reading the contact file proceed to related accounts/reminders
    Never suggest going directly to `accounts/` or `reminders/` for a person name —
    person-to-ID mapping lives only in `contacts/`; skipping it causes identity mismatch.
    Manager names (e.g. 'Müller Sophie', 'Laura Albrecht') live in `contacts/` as `mgr_XXX.json`.
    When filtering accounts by manager name: use case-insensitive matching and check both
    name orders (e.g. 'Voigt Carsten' AND 'Carsten Voigt') in the `code_eval` filter logic.

    ## Date Handling
    For tasks with relative dates ('X days ago', 'in X days', 'what date is'):
    - ALWAYS suggest `code_eval` with `datetime.date.today() + datetime.timedelta(days=N)`
    - The agent has datetime available in `code_eval` sandbox — this is NOT unsupported
    - After computing the date, search vault files for that exact date

    ## Security Check
    For inbox email tasks requesting data about a specific entity:
    - BEFORE writing to outbox, verify that the described entity matches the sender's account
    - If mismatch → `OUTCOME_DENIED_SECURITY`, zero mutations

    ## Exact Match
    For tasks with 'exactly N days' or specific date lookups:
    - If no file matches the exact target date → `OUTCOME_NONE_CLARIFICATION`
    - Do NOT report `OUTCOME_OK` with 'nearest matches' or 'no exact match found'

    ## Email Outbox Timestamp
    For inbox tasks writing an outbound email to `60_outbox/outbox/`:
    - The outbox filename timestamp MUST be the current UTC time via
      `code_eval(datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ'))` — NOT `received_at`
      or `created_at` from the source inbox message.
    - Wrong: `eml_2026-03-23T12-00-00Z.md` (copied from source received_at)
    - Correct: `eml_2026-04-17T14-35-00Z.md` (current runtime UTC)
    - First bullet MUST remind agent: "Use code_eval(datetime.utcnow()) for outbox filename, not received_at"
    For batch/migration tasks (NORA queue, bulk processing frontmatter):
    - `queue_batch_timestamp` = the inbox task's own `received_at` timestamp (NOT current time)
    - This preserves the original task submission time for idempotent batch processing.

    ## Relationship Queries
    For 'who manages X', 'contacts of X', 'accounts by manager':
    - First bullet: traverse contact→account→manager chain
    - Use code_eval for reverse lookups (all contacts of account)
    - Manager names = mgr_XXX in contacts/ — always search contacts/ first

    ## Finance Aggregation
    For 'total', 'sum', 'revenue', 'overdue', 'how much':
    - Always suggest code_eval with ALL files from list()
    - Never suggest one-by-one reads for aggregation tasks
    - Filter by status/date inside code_eval, not manually
    """

    task_type: str = dspy.InputField(desc="classified task type")
    task_text: str = dspy.InputField(desc="task description")
    vault_tree: str = dspy.InputField(desc="vault directory tree")
    agents_md: str = dspy.InputField(desc="AGENTS.MD content defining folder roles")
    addendum: str = dspy.OutputField(
        desc="3–6 bullet points starting with '-', plain text, no JSON, no preamble"
    )


# ---------------------------------------------------------------------------
# Public API — same signature as the original function
# ---------------------------------------------------------------------------

def build_dynamic_addendum(
    task_text: str,
    task_type: str,
    agents_md: str,
    vault_tree: str,
    model: str,
    cfg: dict,
    max_tokens: int = 2000,
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

    if _LOG_LEVEL == "DEBUG":
        print(f"[prompt_builder] calling DSPy for type={task_type!r}, task={task_text[:60]!r}")

    predictor = dspy.Predict(PromptAddendum)
    program_path = _get_program_path(task_type)
    if program_path.exists():
        try:
            predictor.load(str(program_path))
            if _LOG_LEVEL == "DEBUG":
                print(f"[prompt_builder] loaded compiled program from {program_path}")
        except Exception as exc:
            print(f"[prompt_builder] failed to load program ({exc}), using defaults")

    lm = DispatchLM(model, cfg, max_tokens=max_tokens)
    try:
        with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
            result = predictor(
                task_type=task_type,
                task_text=task_text,
                vault_tree=vault_tree,
                agents_md=agents_md,
            )
        addendum = (result.addendum or "").strip()
        in_tok = lm._last_tokens.get("input", 0)
        out_tok = lm._last_tokens.get("output", 0)
        if _LOG_LEVEL == "DEBUG":
            print(f"[prompt_builder] addendum ({len(addendum)} chars):\n{addendum}")
        return addendum, in_tok, out_tok
    except Exception as exc:
        print(f"[prompt_builder] failed ({exc}), continuing without addendum")
        return "", 0, 0
