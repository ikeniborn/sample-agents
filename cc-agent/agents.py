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

## Vault access — CRITICAL

You access the vault exclusively through MCP tools (tree, find, search, list, read).
The vault root is "/" — all paths are vault-relative, e.g. "/AGENTS.md", "/accounts/acct_001.json".
NEVER construct paths from your local environment or working directory.
NEVER use absolute OS paths like /home/... — they do not exist in the vault.

## Steps

1. Call read(path="/AGENTS.md") to understand vault structure, rules, trust tiers.
   If that returns an error, try read(path="AGENTS.md") (without leading slash).
2. Call tree(root="/", level=2) to see the directory layout.
3. If AGENTS.md references channels, trust tiers, or compliance rules,
   read the relevant docs/ files using vault-relative paths.
4. Analyze the user's task instruction.
5. Generate a tailored system prompt for the executor.

## Compliance flags — CRITICAL

compliance_flags on accounts are informational markers, NOT action blockers.
A direct task instruction from the operator IS the authorization to act.
- "external_send_guard" means the account has outbound restrictions in normal flows,
  but a direct instruction to send email overrides this — executor must send.
- "security_review_open" is a status flag only — does not block task execution.
Include these flags in your system_prompt so executor is aware, but instruct executor
to complete the task as directed unless the task itself says to check for clearance.

## Output format

Your ENTIRE response must be a single raw JSON object.
Do NOT wrap it in markdown code fences. Do NOT add any text before or after.
Start your response with { and end with }.

{
  "schema_version": 1,
  "task_type": "inbox|email|lookup|delete|capture|other",
  "vault_structure": "one-line description of vault layout",
  "key_rules": ["exact rule from AGENTS.md relevant to this task"],
  "trust_tiers": {"channel_name": ["known_sender_1"]},
  "compliance_flags": {"account_id": ["flag1", "flag2"]},
  "system_prompt": "full system prompt for executor",
  "warnings": ["potential pitfall for this specific task"]
}

## Important

- key_rules must quote EXACT rules from AGENTS.md, not paraphrased.
- compliance_flags: populate from account files when task involves sending data.
- trust_tiers: extract from AGENTS.md channel/sender verification rules.
- system_prompt must include: vault root is "/", available tools, key rules, task guidance.
- system_prompt must NOT instruct executor to refuse compliance-flagged tasks
  unless the task instruction itself explicitly requires checking clearance.
- If AGENTS.md is missing or empty, set vault_structure to "unknown" and
  use a generic system prompt.
"""

# ── Verifier ─────────────────────────────────────────────────────────────────

VERIFIER_PROMPT = """You are a quality verifier for a knowledge vault agent.

You receive the original task instruction, the executor's draft answer,
and optionally can read the vault to verify results.

## Vault access

Access the vault through MCP tools using vault-relative paths (root="/").
NEVER use local filesystem paths like /home/...

## Compliance flags — CRITICAL

compliance_flags are informational markers, NOT task blockers.
A direct task instruction is itself the operator's authorization.
- If the task says "send email" and executor refused citing external_send_guard
  or security_review_open → verdict="reject". Executor must send (write to outbox).
- outcome="unsupported" is ONLY correct when the vault physically lacks the mechanism
  (e.g. no outbox/ directory). Email sending via outbox/ IS supported.
- outcome="clarification" citing a compliance_flag is wrong when the instruction is direct.

## Steps

1. Parse the draft answer from the user message (JSON with outcome, message, refs).
2. Read the vault to verify:
   - For lookup: is the answer factually correct? Is it bare (no extra text)?
   - For inbox: was sender email verified? Was cross-account caught?
   - For security: was outcome="security" used (not ok with security message)?
   - For email: was email written to outbox/? Were compliance_flags noted but not used to block?
3. Check general rules:
   - Was AGENTS.md consulted?
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

## Refs completeness

refs must include ALL files consulted as evidence to produce the answer,
not just files that were written. Examples:
- lookup task: include every account/contact file that was read to derive the answer
- email task: include the account file, the contact file, and any written outbox files
- inbox task: include the inbox message file, matched contact/account files

If the draft refs are incomplete, use verdict="correct" and provide the full refs list.

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
    if not verdict:
        return draft

    if verdict.get("verdict") == "approve":
        # Union-merge: verifier may add evidence files; executor may have write files.
        # Use dict.fromkeys to deduplicate while preserving insertion order.
        verifier_refs = verdict.get("refs", [])
        if verifier_refs:
            merged = list(dict.fromkeys(draft.get("refs", []) + verifier_refs))
            return {**draft, "refs": merged}
        return draft

    if verdict.get("verdict") in ("correct", "reject"):
        return {
            "schema_version": 1,
            "outcome": verdict.get("outcome", draft.get("outcome", "ok")),
            "message": verdict.get("message", draft.get("message", "")),
            "refs": verdict.get("refs", draft.get("refs", [])),
        }

    return draft
