"""JSON extraction from free-form LLM text output.

Extracted from loop.py to reduce God Object size.
Public API used by loop.py:
  _obj_mutation_tool()    — check if a JSON object is a mutation action
  _richness_key()         — deterministic tie-break for same-tier candidates (FIX-212)
  _extract_json_from_text() — 7-level priority JSON extraction (FIX-146)
  _normalize_parsed()     — normalize raw parsed dict to valid NextStep structure (FIX-207)
"""
import json
import re

from .dispatch import CLI_YELLOW, CLI_CLR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MUTATION_TOOLS = frozenset({"write", "delete", "move", "mkdir"})

# Maps Req_XXX class names to canonical tool names used in JSON payloads.
# Some models (e.g. minimax) emit "Action: Req_Read({...})" without a "tool" field inside the JSON.
_REQ_CLASS_TO_TOOL: dict[str, str] = {
    "req_read": "read", "req_write": "write", "req_delete": "delete",
    "req_list": "list", "req_search": "search", "req_find": "find",
    "req_tree": "tree", "req_move": "move", "req_mkdir": "mkdir",
    "req_code_eval": "code_eval",
}
# Regex: capture "Req_Xxx" prefix immediately before a JSON object — FIX-150
_REQ_PREFIX_RE = re.compile(r"Req_(\w+)\s*\(", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _obj_mutation_tool(obj: dict) -> str | None:
    """Return the mutation tool name if obj is a write/delete/move/mkdir action, else None."""
    tool = obj.get("tool") or (obj.get("function") or {}).get("tool", "")
    return tool if tool in _MUTATION_TOOLS else None


def _richness_key(obj: dict) -> tuple:  # FIX-212: deterministic tie-break for same-tier candidates
    """Lower tuple = preferred. Used by min() to break ties when multiple candidates share a tier."""
    has_full = "current_state" in obj and "function" in obj
    fn_tool = (obj.get("function") or {}).get("tool", "")
    return (
        -len(obj),                          # more keys = richer
        not has_full,                       # full NextStep preferred (False < True)
        fn_tool == "report_completion",     # actionable tools preferred over report
    )


# ---------------------------------------------------------------------------
# 7-level JSON extraction
# ---------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> dict | None:  # FIX-146 (revised FIX-149, FIX-150)
    """Extract the most actionable valid JSON object from free-form model output.

    Priority (highest first):
    1. ```json fenced block — explicit, return immediately
    2. First object whose tool is a mutation (write/delete/move/mkdir) — bare or wrapped
       Rationale: multi-action responses often end with report_completion AFTER the writes;
       executing report_completion first would skip the writes entirely.
    3. First bare object with any known 'tool' key (non-mutation, e.g. search/read/list)
    4. First full NextStep (current_state + function) with a non-report_completion tool
    5. First full NextStep with any tool (including report_completion)
    6. First object with a 'function' key
    7. First valid JSON object
    8. YAML fallback
    """
    # 1. ```json ... ``` fenced block — explicit, return immediately
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Collect ALL valid bracket-matched JSON objects.
    # FIX-150: also detect "Req_XXX({...})" patterns and inject "tool" when absent,
    # since some models (minimax) omit the tool field inside the JSON payload.
    candidates: list[dict] = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        # Check for Req_XXX prefix immediately before this {
        prefix_match = None
        prefix_region = text[max(0, start - 20):start]
        pm = _REQ_PREFIX_RE.search(prefix_region)
        if pm:
            req_name = pm.group(1).lower()
            inferred_tool = _REQ_CLASS_TO_TOOL.get(f"req_{req_name}")
            if inferred_tool:
                prefix_match = inferred_tool
        depth = 0
        for idx in range(start, len(text)):
            if text[idx] == "{":
                depth += 1
            elif text[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:idx + 1])
                        if isinstance(obj, dict):
                            # Inject inferred tool name when model omits it (e.g. Req_Read({"path":"..."}))
                            if prefix_match and "tool" not in obj:
                                obj = {"tool": prefix_match, **obj}
                            candidates.append(obj)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    pos = idx + 1
                    break
        else:
            break

    if candidates:
        # FIX-265: multi-step plan detection — when model outputs ≥3 full NextStep objects
        # (a sequence of planned actions), take the first non-mutation step to force
        # step-by-step execution. Without this, the mutation-priority rule (below) picks
        # a write whose arguments were hallucinated (reads never actually executed).
        _full_steps = [o for o in candidates if "current_state" in o and "function" in o]
        if len(_full_steps) >= 3:
            _SKIP_TOOLS = {"write", "delete", "move", "mkdir", "report_completion"}
            _first_read = [o for o in _full_steps
                           if (o.get("function") or {}).get("tool", "") not in _SKIP_TOOLS]
            if _first_read:
                print(f"{CLI_YELLOW}[FIX-265] Multi-step plan detected ({len(_full_steps)} steps) "
                      f"— taking first non-mutation step{CLI_CLR}")
                return _first_read[0]
        # FIX-212: use min(filtered, key=_richness_key) for deterministic tie-breaking
        # 2. Mutation (write/delete/move/mkdir) — bare {"tool":...} or wrapped {"function":{...}}
        _muts = [o for o in candidates if _obj_mutation_tool(o)]
        if _muts:
            return min(_muts, key=_richness_key)
        # 3. Bare object with any known tool key (non-mutation: search/read/list/etc.)
        _bare = [o for o in candidates if "tool" in o and "current_state" not in o]
        if _bare:
            return min(_bare, key=_richness_key)
        # 4. Full NextStep with non-report_completion tool
        _full_nr = [o for o in candidates
                     if "current_state" in o and "function" in o
                     and (o.get("function") or {}).get("tool", "") != "report_completion"]
        if _full_nr:
            return min(_full_nr, key=_richness_key)
        # 5. Full NextStep (any tool, including report_completion)
        _full = [o for o in candidates if "current_state" in o and "function" in o]
        if _full:
            return min(_full, key=_richness_key)
        # 6. Object with function key
        _fn = [o for o in candidates if "function" in o]
        if _fn:
            return min(_fn, key=_richness_key)
        # 7. Richest candidate
        return min(candidates, key=_richness_key)

    # 8. YAML fallback — for models that output YAML or Markdown when JSON schema not supported
    try:
        import yaml  # pyyaml
        stripped = re.sub(r"```(?:yaml|markdown)?\s*", "", text.strip()).replace("```", "").strip()
        parsed_yaml = yaml.safe_load(stripped)
        if isinstance(parsed_yaml, dict) and any(k in parsed_yaml for k in ("current_state", "function", "tool")):
            print(f"\x1B[33m[fallback] YAML fallback parsed successfully\x1B[0m")
            return parsed_yaml
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# NextStep normalization
# ---------------------------------------------------------------------------

def _normalize_parsed(parsed: dict) -> dict:
    """Normalize a raw parsed dict into a valid NextStep structure.  # FIX-207
    Handles bare function objects, plan truncation, and missing task_completed.
    Shared by Anthropic and OpenRouter/Ollama tiers."""
    if "tool" in parsed and "current_state" not in parsed:
        parsed = {
            "current_state": "continuing",
            "plan_remaining_steps_brief": ["execute action"],
            "task_completed": False,
            "function": parsed,
        }
    elif "reasoning" in parsed and "current_state" not in parsed:
        parsed = {
            "current_state": "reasoning stripped",
            "plan_remaining_steps_brief": ["explore vault"],
            "task_completed": False,
            "function": {"tool": "list", "path": "/"},
        }
    if isinstance(parsed.get("plan_remaining_steps_brief"), list):
        steps = [s for s in parsed["plan_remaining_steps_brief"] if s]
        parsed["plan_remaining_steps_brief"] = steps[:5] if steps else ["continue"]
    if "task_completed" not in parsed:
        parsed["task_completed"] = False
    return parsed
