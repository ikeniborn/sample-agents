"""
Multi-agent prompts, parsers, and protocol for cc-agent pipeline.

Three agent roles:
  - Classifier: reads vault, generates tailored system prompt
  - Executor: performs the task using generated prompt
  - Verifier: checks executor's work, approves/corrects/rejects

Inter-agent exchange format: JSON files with schema_version field.
"""

import json
import re as _re

from prompt import SYSTEM_PROMPT


def _find_json_object(text: str) -> str | None:
    """Return the first complete JSON object substring using bracket counting."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None

# ── Classifier ───────────────────────────────────────────────────────────────

CLASSIFIER_PROMPT = """You are a task classifier for a knowledge vault agent.

Your job: read the vault structure and generate a tailored system prompt
for the executor agent that will perform the actual task.

## Steps

1. Call read(path="AGENTS.MD") to understand vault structure, rules, trust tiers.
2. Call tree(root="/", level=2) to see the directory layout.
3. If AGENTS.MD references channels, trust tiers, or compliance rules,
   read the relevant docs/ files.
4. Analyze the user's task instruction.
5. Generate a tailored system prompt for the executor.

## Output format

Output ONLY a single JSON object (no markdown, no explanation):

{
  "schema_version": 1,
  "task_type": "inbox|email|lookup|delete|capture|other",
  "vault_structure": "one-line description of vault layout",
  "key_rules": ["specific rule from AGENTS.MD relevant to this task"],
  "trust_tiers": {"channel_name": ["known_sender_1"]},
  "compliance_flags": {"account_id": ["flag1", "flag2"]},
  "system_prompt": "full system prompt for executor",
  "warnings": ["potential pitfall for this specific task"]
}

## Important

- key_rules must quote EXACT rules from AGENTS.MD, not paraphrased.
- compliance_flags: read account files if the task involves sending data.
- trust_tiers: extract from AGENTS.MD channel/sender verification rules.
- system_prompt should include available tools, rules, and task-specific guidance.
- If AGENTS.MD is missing or empty, set vault_structure to "unknown" and
  use a generic system prompt.
"""

# ── Verifier ─────────────────────────────────────────────────────────────────

VERIFIER_PROMPT = """You are a quality verifier for a knowledge vault agent.

You receive the original task instruction, the executor's draft answer,
and optionally can read the vault to verify results.

## Steps

1. Parse the draft answer from the user message (JSON with outcome, message, refs).
2. Read the vault to verify:
   - For lookup: is the answer factually correct? Is it bare (no extra text)?
   - For inbox: was sender email verified? Was cross-account caught?
   - For security: was outcome="security" used (not ok with security message)?
   - For email: were compliance_flags checked?
3. Check general rules:
   - Was AGENTS.MD consulted?
   - Were inbox files left in place (not deleted)?
   - Is the outcome value correct for the situation?

## Output format

Output ONLY a single JSON object (no markdown, no explanation):

{
  "schema_version": 1,
  "verdict": "approve|correct|reject",
  "outcome": "ok|clarification|security|unsupported",
  "message": "corrected message if verdict is correct, else original",
  "refs": ["corrected refs if needed"],
  "reason": "brief explanation of verdict"
}

## Verdicts

- "approve": executor's answer is correct, submit as-is.
- "correct": answer needs minor fix (wrong format, extra text). Provide corrected outcome/message/refs.
- "reject": answer is fundamentally wrong (wrong outcome, missed security issue, incorrect data). Explain what went wrong so executor can retry.

## Important

- For lookup tasks with "return only" / "answer only": message MUST be bare value only.
- When you see outcome="ok" but the situation warrants security/clarification, verdict="correct" with the right outcome.
- Always verify by reading actual vault files, not just trusting the draft.
"""

# ── JSON extraction from agent stdout ────────────────────────────────────────

_JSON_FENCED = _re.compile(r"```json\s*\n(.*?)\n```", _re.S)


def _extract_json(lines: list[str]) -> dict | None:
    """Extract first valid JSON object from agent stdout lines."""
    text = "\n".join(lines)
    # Try fenced ```json ... ``` first
    m = _JSON_FENCED.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON object via bracket-counting (handles arbitrary nesting)
    raw = _find_json_object(text)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return None


# ── Parsers ──────────────────────────────────────────────────────────────────

def parse_classifier_output(lines: list[str]) -> dict | None:
    """Parse classifier agent stdout → classification dict or None."""
    result = _extract_json(lines)
    if not result:
        return None
    if "system_prompt" not in result:
        return None
    result.setdefault("schema_version", 1)
    result.setdefault("task_type", "other")
    result.setdefault("vault_structure", "unknown")
    result.setdefault("key_rules", [])
    result.setdefault("warnings", [])
    return result


def parse_verifier_output(lines: list[str]) -> dict | None:
    """Parse verifier agent stdout → verdict dict or None."""
    result = _extract_json(lines)
    if not result:
        return None
    if "verdict" not in result:
        return None
    result.setdefault("schema_version", 1)
    result.setdefault("outcome", "ok")
    result.setdefault("message", "")
    result.setdefault("refs", [])
    result.setdefault("reason", "")
    return result


def build_executor_prompt(classification: dict) -> str:
    """Build executor system prompt from classifier output."""
    base = classification.get("system_prompt", SYSTEM_PROMPT)

    # Append vault context and warnings if the classifier provided the base prompt
    vault_ctx = classification.get("vault_structure", "")
    key_rules = classification.get("key_rules", [])
    warnings = classification.get("warnings", [])

    addendum_parts = []
    if vault_ctx and vault_ctx != "unknown":
        addendum_parts.append(f"## Vault context\n{vault_ctx}")
    if key_rules:
        rules_text = "\n".join(f"- {r}" for r in key_rules)
        addendum_parts.append(f"## Key rules for this task\n{rules_text}")
    if warnings:
        warn_text = "\n".join(f"- {w}" for w in warnings)
        addendum_parts.append(f"## Warnings\n{warn_text}")

    if addendum_parts:
        return base + "\n\n" + "\n\n".join(addendum_parts)
    return base


def apply_verdict(draft: dict, verdict: dict | None) -> dict:
    """Apply verifier verdict to draft, return final answer."""
    if not verdict or verdict.get("verdict") == "approve":
        return draft

    if verdict.get("verdict") in ("correct", "reject"):
        return {
            "schema_version": 1,
            "outcome": verdict.get("outcome", draft.get("outcome", "ok")),
            "message": verdict.get("message", draft.get("message", "")),
            "refs": verdict.get("refs", draft.get("refs", [])),
        }

    return draft
