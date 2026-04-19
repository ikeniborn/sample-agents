"""Task type classifier and model router for multi-model PAC1 agent."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_TYPE_RE = re.compile(r'\{[^}]*"type"\s*:\s*"(\w+)"[^}]*\}')  # extract type from partial/wrapped JSON

from typing import TYPE_CHECKING

from .dispatch import call_llm_raw

if TYPE_CHECKING:
    from .prephase import PrephaseResult

# Task type literals
TASK_DEFAULT = "default"
TASK_QUEUE = "queue"
TASK_CAPTURE = "capture"
TASK_CRM = "crm"
TASK_TEMPORAL = "temporal"
TASK_PREJECT = "preject"
TASK_EMAIL = "email"
TASK_LOOKUP = "lookup"
TASK_INBOX = "inbox"
TASK_DISTILL = "distill"
TASK_CODER = "coder"


# Think words — kept for distill detection only (think + write → distill)
_THINK_WORDS = re.compile(
    r"\b(distill|analyze|analyse|summarize|summarise|compare|evaluate|review|infer"
    r"|explain|interpret|assess|what does|what is the|why does|how does|what should)\b",
    re.IGNORECASE,
)

# FIX-265b: broadened inbox detection — also matches "review inbound note", "next inbox message"
_INBOX_RE = re.compile(
    r"\b(process|check|handle|review)\s+(the\s+)?(next\s+)?(inbox|inbound)\b",
    re.IGNORECASE,
)

# Queue: bulk/batch inbox processing — "work through", "take care of", queue/inbox/items variants
_QUEUE_RE = re.compile(
    r"\b(work\s+through|take\s+care\s+of|work\s+on|process|handle)\s+(the\s+|all\s+)?"
    r"(incoming\s+|pending\s+|inbound\s+)?(queue|inbox|items|inbound)\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(
    r"\b(send|compose|write|email)\b.*\b(to|recipient|subject)\b"
    r"|\bemail\s+(?!(?:address|of|from|in)\b)[A-Za-z]+\s+(a\b|an\b|brief\b|reminder\b|summary\b|short\b)",
    re.IGNORECASE,
)

_LOOKUP_RE = re.compile(
    r"\b(what\s+is|find|lookup|search\s+for)\b.*\b(email|phone|contact|account)\b"
    r"|\bwhich\s+(accounts?|contacts?|files?)\b.*\b(managed\s+by|by|in|from|with)\b",
    re.IGNORECASE,
)

# Write-verbs used to distinguish lookup from distill/email
# FIX-264: add(?![-]) prevents matching "add" in compound words like "add-on"
_WRITE_VERBS_RE = re.compile(
    r"\b(write|create|add(?![-])|update|send|compose|delete|move|rename)\b",
    re.IGNORECASE,
)

# FIX-175: counting/aggregation queries without write intent → lookup (read-only vault data query).
_COUNT_QUERY_RE = re.compile(
    r"\b(how\s+many|count|sum\s+of|total\s+of|average|aggregate)\b",
    re.IGNORECASE,
)

_FINANCE_RE = re.compile(
    r"\b(invoice|revenue|spend|overdue|payment|bill|amount|balance)\b",
    re.IGNORECASE,
)

# Capture: explicit content snippet with source/destination
_CAPTURE_RE = re.compile(
    r"\bcapture\b.{0,60}\b(snippet|from|into|content|text|this)\b",
    re.IGNORECASE,
)

# CRM: follow-up date arithmetic — reschedule, reconnect, next-contact date changes
_CRM_RE = re.compile(
    r"\b(reschedule|reconnect|follow.?up|next\s+contact"
    r"|asked\s+to\s+(move|reconnect|reschedule)"
    r"|move\s+the\s+(follow|date|next)"
    r"|fix\s+the\s+(follow.?up|due\s+date))\b",
    re.IGNORECASE,
)

# Temporal: date-relative queries needing datetime arithmetic
_TEMPORAL_RE = re.compile(
    r"\b(\d+\s+days?\s+ago|in\s+\d+\s+days?|what\s+date\s+is|days?\s+from\s+now"
    r"|exactly\s+\d+\s+days?|looking\s+back\s+\d+|back\s+\d+\s+days?)\b",
    re.IGNORECASE,
)

# Preject: external API / calendar / sync to external service — immediate reject
# Note: "invoice" excluded — vault invoices are supported via INVOICE WORKFLOW
_PREJECT_RE = re.compile(
    r"\b(calendar\s+invite|create\s+(meeting|event|ticket)"
    r"|sync\s+(to|with)\s+\w+|upload\s+to\s+https?"
    r"|salesforce|hubspot|zendesk|jira|external\s+(api|crm|url)"
    r"|send\s+to\s+https?)\b",
    re.IGNORECASE,
)


@dataclass
class _Rule:
    must: list[re.Pattern]
    must_not: list[re.Pattern]
    result: str
    label: str  # for logging


# Priority-ordered rule matrix
# Priority: preject > queue > inbox > email > lookup > capture > crm > temporal > distill > default
_RULE_MATRIX: list[_Rule] = [
    # Rule 0: external API / calendar / sync to external service → preject (immediate rejection)
    _Rule(
        must=[_PREJECT_RE],
        must_not=[],
        result=TASK_PREJECT,
        label="preject-keywords",
    ),
    # Rule 1: bulk queue processing — "work through queue", "take care of inbox" → queue
    _Rule(
        must=[_QUEUE_RE],
        must_not=[_PREJECT_RE],
        result=TASK_QUEUE,
        label="queue-keywords",
    ),
    # Rule 2: inbox process/check/handle single item → inbox
    _Rule(
        must=[_INBOX_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE],
        result=TASK_INBOX,
        label="inbox-keywords",
    ),
    # Rule 3: send/compose email with recipient/subject → email
    _Rule(
        must=[_EMAIL_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE],
        result=TASK_EMAIL,
        label="email-keywords",
    ),
    # Rule 4: lookup contact/email/phone with no write intent → lookup
    _Rule(
        must=[_LOOKUP_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE, _WRITE_VERBS_RE],
        result=TASK_LOOKUP,
        label="lookup-keywords",
    ),
    # Rule 4b: counting/aggregation query with no write intent → lookup
    _Rule(
        must=[_COUNT_QUERY_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE, _WRITE_VERBS_RE],
        result=TASK_LOOKUP,
        label="count-query",
    ),
    # Rule 4c: finance-specific keywords with no write intent → lookup
    _Rule(
        must=[_FINANCE_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE, _WRITE_VERBS_RE],
        result=TASK_LOOKUP,
        label="finance-query",
    ),
    # Rule 5: explicit content capture with source/destination → capture
    _Rule(
        must=[_CAPTURE_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE],
        result=TASK_CAPTURE,
        label="capture-keywords",
    ),
    # Rule 6: CRM follow-up date arithmetic — reschedule/reconnect → crm
    _Rule(
        must=[_CRM_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE],
        result=TASK_CRM,
        label="crm-keywords",
    ),
    # Rule 7: date-relative queries → temporal
    _Rule(
        must=[_TEMPORAL_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE, _WRITE_VERBS_RE],
        result=TASK_TEMPORAL,
        label="temporal-keywords",
    ),
    # Rule 8: think-words AND write-verbs simultaneously → distill
    _Rule(
        must=[_THINK_WORDS, _WRITE_VERBS_RE],
        must_not=[_PREJECT_RE, _QUEUE_RE, _INBOX_RE, _EMAIL_RE],
        result=TASK_DISTILL,
        label="distill-keywords",
    ),
]


def classify_task(task_text: str) -> str:
    """Regex-based structured rule engine for task type classification."""
    for rule in _RULE_MATRIX:
        if (all(r.search(task_text) for r in rule.must)
                and not any(r.search(task_text) for r in rule.must_not)):
            return rule.result
    return TASK_DEFAULT


# ---------------------------------------------------------------------------
# LLM-based task classification (pre-requisite before agent start)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a task router. Classify the task into exactly one type. "
    'Reply ONLY with valid JSON: {"type": "<type>"} where <type> is one of: '
    "preject, queue, capture, crm, temporal, email, lookup, inbox, distill, default.\n"
    "preject = calendar invites, sync to external CRM/service, upload to external URL\n"
    "queue = work through / take care of / handle the incoming queue or all inbox items\n"
    "inbox = process/check/handle/review single inbox or inbound note\n"
    "email = send/compose/write email to a recipient\n"
    "lookup = find, count, or query vault data (contacts, files, channels) with no write action\n"
    "capture = capture explicit snippet/content from source into a specific vault path\n"
    "crm = reschedule follow-up, reconnect date, fix follow-up regression, date arithmetic + write\n"
    "temporal = date-relative queries needing datetime arithmetic (N days ago, in N days, what date)\n"
    "distill = analysis/reasoning AND writing a card/note/summary\n"
    "default = everything else (read, write, create, delete, move, standard tasks)"
)

_VALID_TYPES = frozenset({
    TASK_PREJECT, TASK_QUEUE, TASK_CAPTURE, TASK_CRM, TASK_TEMPORAL,
    TASK_DEFAULT, TASK_EMAIL, TASK_LOOKUP, TASK_INBOX, TASK_DISTILL,
})

# Ordered keyword → task_type table for plain-text LLM response fallback.
_PLAINTEXT_FALLBACK: list[tuple[tuple[str, ...], str]] = [
    (("preject",),  TASK_PREJECT),
    (("queue",),    TASK_QUEUE),
    (("inbox",),    TASK_INBOX),
    (("email",),    TASK_EMAIL),
    (("capture",),  TASK_CAPTURE),
    (("crm",),      TASK_CRM),
    (("temporal",), TASK_TEMPORAL),
    (("lookup",),   TASK_LOOKUP),
    (("distill",),  TASK_DISTILL),
    (("default",),  TASK_DEFAULT),
]


def _count_tree_files(prephase_log: list) -> int:
    """Extract tree text from prephase log and count file entries (non-directory lines)."""
    for msg in prephase_log:
        if msg.get("role") == "user" and "VAULT STRUCTURE:" in msg.get("content", ""):
            tree_block = msg["content"]
            break
    else:
        return 0
    file_lines = [
        ln for ln in tree_block.splitlines()
        if ("─" in ln or "└" in ln or "├" in ln) and not ln.rstrip().endswith("/")
    ]
    return len(file_lines)


def classify_task_llm(task_text: str, model: str, model_config: dict,
                      vault_hint: str | None = None) -> str:
    """Classify task type using an LLM, with regex fast-path and multi-tier fallbacks.

    Fast-path: if regex already returns a non-default type, the LLM call is skipped
    for high-confidence types (preject, email). Others consult the LLM when vault
    context (AGENTS.MD) might change the classification.
    """
    _HIGH_CONF_TYPES = frozenset({TASK_PREJECT, TASK_EMAIL})
    _regex_pre = classify_task(task_text)
    if _regex_pre in _HIGH_CONF_TYPES:
        print(f"[MODEL_ROUTER] Regex-confident type={_regex_pre!r}, skipping LLM")
        return _regex_pre
    user_msg = f"Task: {task_text[:150]}"
    if vault_hint:
        user_msg += f"\nContext: {vault_hint[:400]}"
    _base_opts = model_config.get("ollama_options_classifier") or model_config.get("ollama_options", {})
    _cls_opts = {k: v for k, v in _base_opts.items() if k in ("num_ctx", "temperature", "seed")}
    _cls_cfg = {
        **model_config,
        "max_completion_tokens": min(model_config.get("max_completion_tokens", 512), 512),
        "ollama_options": _cls_opts or None,
    }
    try:
        raw = call_llm_raw(_CLASSIFY_SYSTEM, user_msg, model, _cls_cfg,
                           max_tokens=_cls_cfg["max_completion_tokens"],
                           think=False,
                           max_retries=1)
        if not raw:
            print("[MODEL_ROUTER] All LLM tiers failed or empty, falling back to regex")
            return classify_task(task_text)
        try:
            detected = str(json.loads(raw).get("type", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            m = _JSON_TYPE_RE.search(raw)
            detected = m.group(1).strip() if m else ""
            if detected:
                print(f"[MODEL_ROUTER] Extracted type via regex from: {raw!r}")
        if not detected:
            raw_lower = raw.lower()
            for keywords, task_type in _PLAINTEXT_FALLBACK:
                if any(kw in raw_lower for kw in keywords):
                    detected = task_type
                    print(f"[MODEL_ROUTER] Extracted type {task_type!r} from plain text: {raw[:60]!r}")
                    break
        if detected in _VALID_TYPES:
            print(f"[MODEL_ROUTER] LLM classified task as '{detected}'")
            return detected
        print(f"[MODEL_ROUTER] LLM returned unknown type '{detected}', falling back to regex")
    except Exception as exc:
        print(f"[MODEL_ROUTER] LLM classification failed ({exc}), falling back to regex")
    return classify_task(task_text)


@dataclass
class ModelRouter:
    """Routes tasks to appropriate models based on task type classification."""
    default: str
    classifier: str
    # Optional per-type overrides — fall back to default if not provided
    email: str = ""
    lookup: str = ""
    inbox: str = ""
    queue: str = ""
    capture: str = ""
    crm: str = ""
    temporal: str = ""
    preject: str = ""
    coder: str = ""
    evaluator: str = ""
    prompt_builder: str = ""
    configs: dict[str, dict] = field(default_factory=dict)

    def _select_model(self, task_type: str) -> str:
        return {
            TASK_EMAIL:    self.email    or self.default,
            TASK_LOOKUP:   self.lookup   or self.default,
            TASK_INBOX:    self.inbox    or self.default,
            TASK_QUEUE:    self.queue    or self.inbox or self.default,
            TASK_CAPTURE:  self.capture  or self.default,
            TASK_CRM:      self.crm      or self.default,
            TASK_TEMPORAL: self.temporal or self.lookup or self.default,
            TASK_PREJECT:  self.preject  or self.default,
            TASK_DISTILL:  self.default,
            TASK_CODER:    self.default,
        }.get(task_type, self.default)

    def resolve(self, task_text: str) -> tuple[str, dict, str]:
        """Return (model_id, model_config, task_type) using regex-only classification."""
        task_type = classify_task(task_text)
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def _adapt_config(self, cfg: dict, task_type: str) -> dict:
        """Apply task-type specific ollama_options overlay (shallow merge)."""
        key = f"ollama_options_{task_type}"
        override = cfg.get(key)
        if not override:
            return cfg
        adapted = {**cfg, "ollama_options": {**cfg.get("ollama_options", {}), **override}}
        print(f"[MODEL_ROUTER] Adapted ollama_options for type={task_type}: {adapted['ollama_options']}")
        return adapted

    def resolve_after_prephase(self, task_text: str, pre: "PrephaseResult") -> tuple[str, dict, str]:
        """Classify once after prephase using AGENTS.MD content as context."""
        file_count = _count_tree_files(pre.log)
        vault_hint = None
        if pre.agents_md_content:
            vault_hint = f"AGENTS.MD:\n{pre.agents_md_content}\nvault files: {file_count}"
        task_type = classify_task_llm(
            task_text, self.classifier, self.configs.get(self.classifier, {}),
            vault_hint=vault_hint,
        )
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        adapted_cfg = self._adapt_config(self.configs.get(model_id, {}), task_type)
        return model_id, adapted_cfg, task_type
