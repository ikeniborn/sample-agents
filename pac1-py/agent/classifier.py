"""Task type classifier and model router for multi-model PAC1 agent."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_TYPE_RE = re.compile(r'\{[^}]*"type"\s*:\s*"(\w+)"[^}]*\}')  # FIX-82: extract type from partial/wrapped JSON

from typing import TYPE_CHECKING

from .dispatch import call_llm_raw

if TYPE_CHECKING:
    from .prephase import PrephaseResult

# Task type literals
TASK_DEFAULT = "default"
TASK_THINK = "think"
TASK_LONG_CONTEXT = "longContext"


_THINK_WORDS = re.compile(
    r"\b(distill|analyze|analyse|summarize|summarise|compare|evaluate|review|infer|"
    r"explain|interpret|assess|what does|what is the|why does|how does|what should)\b",
    re.IGNORECASE,
)

_LONG_CONTEXT_WORDS = re.compile(
    r"\b(all files|every file|batch|multiple files|all cards|all threads|each file)\b",
    re.IGNORECASE,
)

_PATH_RE = re.compile(r"/[a-zA-Z0-9_\-\.]+")


def classify_task(task_text: str) -> str:
    """Classify task text into one of: default, think, longContext."""
    # longContext: many file paths OR explicit bulk keywords
    path_count = len(_PATH_RE.findall(task_text))
    if path_count >= 3 or _LONG_CONTEXT_WORDS.search(task_text):
        return TASK_LONG_CONTEXT

    # think: analysis/reasoning keywords
    if _THINK_WORDS.search(task_text):
        return TASK_THINK

    return TASK_DEFAULT


# ---------------------------------------------------------------------------
# FIX-75: LLM-based task classification (pre-requisite before agent start)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a task router. Classify the task into exactly one type. "
    'Reply ONLY with valid JSON: {"type": "<type>"} where <type> is one of: '
    "think, longContext, default.\n"
    "think = analysis/reasoning/summarize/compare/evaluate/explain/distill\n"
    "longContext = batch/all files/multiple files/3+ explicit file paths\n"
    "default = everything else (read, write, create, capture, delete, move, standard tasks)"
)

_VALID_TYPES = frozenset({TASK_THINK, TASK_LONG_CONTEXT, TASK_DEFAULT})


def classify_task_llm(task_text: str, model: str, model_config: dict) -> str:
    """FIX-75: Use LLM (classifier model) to classify task type before agent start.
    Uses FIX-76 call_llm_raw() for 3-tier routing + retry; falls back to regex.
    FIX-79: treat empty string same as None (empty response after retries).
    FIX-81: truncate to 150 chars — enough for task verb, avoids injection tail.
    FIX-82: JSON regex-extraction fallback if json.loads fails."""
    user_msg = f"Task: {task_text[:150]}"  # FIX-81: 600→150 to avoid injection content
    try:
        # FIX-87: thinking models (ollama_think=True) cannot disable think and need large budget;
        # non-thinking models use think=False + small budget (enough for short JSON answer).
        _needs_think = bool(model_config.get("ollama_think"))
        _max_tok = 2000 if _needs_think else 200
        _think_param: bool | None = None if _needs_think else False  # None = use cfg (True); False = disable
        raw = call_llm_raw(_CLASSIFY_SYSTEM, user_msg, model, model_config, max_tokens=_max_tok, think=_think_param)
        if not raw:  # FIX-79: catch both None and "" (empty string after retry exhaustion)
            print("[MODEL_ROUTER][FIX-75] All LLM tiers failed or empty, falling back to regex")
            return classify_task(task_text)
        # Try strict JSON parse first
        try:
            detected = str(json.loads(raw).get("type", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            # FIX-82: JSON parse failed — try regex extraction from response text
            m = _JSON_TYPE_RE.search(raw)
            detected = m.group(1).strip() if m else ""
            if detected:
                print(f"[MODEL_ROUTER][FIX-82] Extracted type via regex from: {raw!r}")
        if detected in _VALID_TYPES:
            print(f"[MODEL_ROUTER][FIX-75] LLM classified task as '{detected}'")
            return detected
        print(f"[MODEL_ROUTER][FIX-75] LLM returned unknown type '{detected}', falling back to regex")
    except Exception as exc:
        print(f"[MODEL_ROUTER][FIX-75] LLM classification failed ({exc}), falling back to regex")
    return classify_task(task_text)


@dataclass
class ModelRouter:
    """Routes tasks to appropriate models based on task type classification."""
    default: str
    think: str
    long_context: str
    # FIX-90: classifier is a first-class routing tier — dedicated model for classification only
    classifier: str
    configs: dict[str, dict] = field(default_factory=dict)

    def _select_model(self, task_type: str) -> str:
        return {
            TASK_THINK: self.think,
            TASK_LONG_CONTEXT: self.long_context,
        }.get(task_type, self.default)

    def resolve(self, task_text: str) -> tuple[str, dict, str]:
        """Return (model_id, model_config, task_type) for the given task text."""
        task_type = classify_task(task_text)
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def resolve_llm(self, task_text: str) -> tuple[str, dict, str]:
        """FIX-75: Use classifier model to classify task, then return (model_id, config, task_type).
        Falls back to regex-based resolve() if LLM classification fails."""
        task_type = classify_task_llm(task_text, self.classifier, self.configs.get(self.classifier, {}))
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER][FIX-75] LLM type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def model_for_type(self, task_type: str) -> tuple[str, dict]:
        """FIX-89: Return (model_id, config) for an already-known task_type."""
        model_id = self._select_model(task_type)
        return model_id, self.configs.get(model_id, {})


# ---------------------------------------------------------------------------
# FIX-89: Post-prephase reclassification using vault context
# ---------------------------------------------------------------------------

# Bulk-scope words in task text
_BULK_TASK_RE = re.compile(
    r"\b(all|every|each|batch|multiple|entire|whole)\b",
    re.IGNORECASE,
)


def _count_tree_files(prephase_log: list) -> int:
    """Extract tree text from prephase log and count file entries (non-directory lines)."""
    for msg in prephase_log:
        if msg.get("role") == "user" and "VAULT STRUCTURE:" in msg.get("content", ""):
            tree_block = msg["content"]
            break
    else:
        return 0
    # File lines: contain └/├/─ and do NOT end with /
    file_lines = [
        ln for ln in tree_block.splitlines()
        if ("─" in ln or "└" in ln or "├" in ln) and not ln.rstrip().endswith("/")
    ]
    return len(file_lines)


def reclassify_with_prephase(task_type: str, task_text: str, pre: PrephaseResult) -> str:
    """FIX-89: Refine task_type using vault context loaded during prephase.
    Called after run_prephase(). Returns adjusted task_type string.

    Signal — LONG_CONTEXT upgrade:
      Vault tree has many file entries (>= 8) AND task text uses bulk-scope words
      (all/every/each/batch). Applies to DEFAULT and THINK; LONG_CONTEXT stays as-is.
    """
    task_lower = task_text.lower()

    # Signal: large vault + bulk-scope task → longContext
    if task_type in (TASK_DEFAULT, TASK_THINK) and _BULK_TASK_RE.search(task_lower):
        file_count = _count_tree_files(pre.log)
        if file_count >= 8:
            print(
                f"[MODEL_ROUTER][FIX-89] {file_count} files in vault tree + bulk task "
                f"→ override '{task_type}' → 'longContext'"
            )
            return TASK_LONG_CONTEXT

    return task_type
