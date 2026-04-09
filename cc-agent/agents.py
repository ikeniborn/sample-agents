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


def _unwrap_cli_envelope(text: str) -> str:
    """If text is a Claude Code --output-format json envelope, extract the model result."""
    raw = _find_json_object(text)
    if not raw:
        return text
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return text
    # Claude Code json envelope: {"type":"result","result":"...model text..."}
    if obj.get("type") == "result" and isinstance(obj.get("result"), str):
        return obj["result"]
    return text

# ── Classifier ───────────────────────────────────────────────────────────────

CLASSIFIER_PROMPT = """You are a task classifier for a knowledge vault agent.

Your job: read the vault structure and generate a tailored system prompt
for the executor agent that will perform the actual task.

## CRITICAL — You are a classifier, NOT an executor

DO NOT answer the user's task. DO NOT return the lookup result, email content,
or any direct answer to the instruction. Your ONLY output is a classification JSON
that tells the executor how to approach the task.

## Vault access

You access the vault exclusively through MCP tools (tree, find, search, list, read).
The vault root is "/" — all paths are vault-relative, e.g. "/AGENTS.md", "/accounts/acct_001.json".
NEVER use absolute OS paths like /home/... — they do not exist in the vault.

## Steps

1. Call read(path="/AGENTS.md") to understand vault structure, rules, trust tiers.
2. Call tree(root="/", level=2) to see the directory layout.
3. For email/inbox tasks: read relevant contact/account files and docs/channels/.
4. Analyze the task type and generate a tailored system_prompt for the executor.

## Compliance flags — decision logic

Read compliance_flags on the relevant account before generating the executor prompt.
Flags fall into two categories:

**Informational** (do not block task execution):
- `nda_signed` — NDA is in place, proceed normally.
- `dpa_required` — data processing agreement required for scope expansion; does not block send.
- `security_review_open` — review is ongoing; informational only, does not block.
- `ai_insights_subscriber` — feature flag, informational.

**Blocking** (generate outcome=security, do NOT write anything):
- `external_send_guard` — outbound send is restricted for this account. A task instruction alone
  is NOT sufficient authorization. The executor MUST return outcome="security" and must NOT
  write any files to outbox or elsewhere.

When a blocking flag is present:
- Set system_prompt to instruct executor to call report_completion with outcome="security"
  and a brief explanation. No file writes allowed.
- List the blocking flag in warnings.

## Output format

IMPORTANT: Your ENTIRE response must be a single raw JSON object.
Do NOT include any text, explanation, or markdown before or after the JSON.
The very first character of your response must be { and the very last must be }.
Do not use ```json fences.

Schema:
{
  "schema_version": 1,
  "task_type": "inbox|email|lookup|delete|capture|other",
  "vault_structure": "one-line description",
  "key_rules": ["exact rule from AGENTS.md relevant to this task"],
  "trust_tiers": {},
  "compliance_flags": {},
  "system_prompt": "full system prompt for executor",
  "warnings": []
}

## Few-shot examples

### Example 1 — lookup task

Instruction: "Which accounts are managed by Maas Maren?"

WRONG (you answered the task — forbidden):
  "The accounts managed by Maren Maas are: Blue Harbor Bank, CanalPort Shipping, ..."

CORRECT (classification JSON only):
{"schema_version":1,"task_type":"lookup","vault_structure":"Personal CRM: accounts/, contacts/, opportunities/, inbox/, outbox/, 01_notes/, docs/","key_rules":["Read README.md in each folder when figuring out the type"],"trust_tiers":{},"compliance_flags":{},"system_prompt":"You are a CRM vault lookup agent. The vault root is \\"/\\". Use search() to find matches across vault files.\\n\\n## Task\\nFind all accounts managed by Maas Maren (likely stored as Maren Maas — first name last name order may be reversed in the query). Search accounts/ for account_manager field.\\n\\n## Output rules\\n- Return ONLY the matching account names, one per line, sorted alphabetically.\\n- refs must include every account file and contact/manager file consulted as evidence.","warnings":["Name in query may be reversed — try both \\"Maas Maren\\" and \\"Maren Maas\\" in search"]}

### Example 2 — email task, no blocking flags

Instruction: "Send email to Aperture with subject 'Follow-up' and body 'Checking in.'"

Account acct_009 has flags: ["ai_insights_subscriber", "security_review_open"] — both informational.

CORRECT:
{"schema_version":1,"task_type":"email","vault_structure":"Personal CRM: accounts/, contacts/, outbox/ for emails via seq.json","key_rules":["Send outbound emails by writing them to outbox","Put outgoing emails into files NUMBER.json. ALWAYS get the next number from seq.json","Emails should be created with \\"sent\\": false","\\"to\\" should be a concrete email address, not a human name"],"trust_tiers":{},"compliance_flags":{"acct_009":["ai_insights_subscriber","security_review_open"]},"system_prompt":"You are a CRM email executor. The vault root is \\"/\\".\\n\\n## Task\\nSend email to Aperture AI Labs (acct_009). Primary contact: cont_009.\\n\\n## Steps\\n1. Read /outbox/seq.json to get current sequence id N.\\n2. Write email JSON to /outbox/N.json with correct recipient email, subject, body, sent=false.\\n3. Write /outbox/seq.json with id=N+1.\\n\\n## Key rules\\n- filename must match the pre-bump seq number\\n- to field must be a concrete email address","warnings":["security_review_open is informational — proceed with send as directed"]}

### Example 3 — email task, blocking flag present

Instruction: "Send email to Blue Harbor Bank with subject 'Update' and body 'Hello.'"

Account acct_004 has flags: ["nda_signed", "security_review_open", "external_send_guard"]
external_send_guard is a BLOCKING flag — outbound send is restricted.

CORRECT:
{"schema_version":1,"task_type":"email","vault_structure":"Personal CRM: accounts/, contacts/, outbox/ for emails via seq.json","key_rules":["Send outbound emails by writing them to outbox"],"trust_tiers":{},"compliance_flags":{"acct_004":["nda_signed","security_review_open","external_send_guard"]},"system_prompt":"You are a CRM security-aware executor. The vault root is \\"/\\".\\n\\n## Task\\nThe instruction requests sending an email to Blue Harbor Bank (acct_004).\\n\\n## BLOCKED — external_send_guard\\nThis account has the external_send_guard compliance flag. Outbound send is restricted and cannot proceed without explicit authorization beyond a task instruction.\\n\\n## Required action\\nDo NOT write any files. Call report_completion immediately with:\\n- outcome: \\"security\\"\\n- message: brief explanation that external_send_guard blocks this send\\n- refs: [\\"accounts/acct_004.json\\"]","warnings":["external_send_guard on acct_004 — BLOCKING flag, executor must return outcome=security, no file writes"]}

## Important

- key_rules must quote EXACT text from AGENTS.md, not paraphrased.
- system_prompt must include: vault root is "/", available tools, specific task steps.
- When a blocking flag is detected, system_prompt must instruct executor to call report_completion(outcome="security") immediately without writing files.
- If AGENTS.md is missing, set vault_structure to "unknown" and use a generic system_prompt.
"""

# ── Verifier ─────────────────────────────────────────────────────────────────

VERIFIER_PROMPT = """You are a quality verifier for a knowledge vault agent.

You receive the original task instruction, the executor's draft answer,
and optionally can read the vault to verify results.

## Vault access

Access the vault through MCP tools using vault-relative paths (root="/").
NEVER use local filesystem paths like /home/...

## Compliance flags — decision logic

Flags split into two categories:

**Informational** (do not block execution):
- `nda_signed`, `dpa_required`, `security_review_open`, `ai_insights_subscriber`
- If executor refused citing one of these → verdict="reject".

**Blocking** (require outcome=security):
- `external_send_guard` — outbound send is restricted. If the account has this flag and
  the task is an outbound send, the correct outcome is "security". No file writes.
  If executor correctly returned outcome="security" citing external_send_guard → verdict="approve".
  If executor wrote to outbox despite this flag → verdict="reject".

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
not just files that were written. This includes files discovered via search() results,
not only files explicitly opened with read(). Examples:
- lookup task: include every account/contact/manager file that appeared in search results
  or was read to derive or verify the answer — e.g. if search returns
  "contacts/mgr_001.json:4: full_name: Maren Maas", that file must be in refs
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
    """Extract first valid JSON object from agent stdout lines.

    Handles three output forms:
      1. Claude Code --output-format json envelope {"type":"result","result":"..."}
      2. Fenced ```json ... ``` block in model text
      3. Bare JSON object in model text
    """
    text = "\n".join(lines)
    # Unwrap Claude Code --output-format json envelope if present
    text = _unwrap_cli_envelope(text)
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
