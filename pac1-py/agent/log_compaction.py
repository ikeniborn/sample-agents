"""Log compaction, step facts accumulation, and state digest for the agent loop.

Extracted from loop.py to reduce God Object size.
Public API used by loop.py:
  _StepFact       — dataclass for one key step fact
  _extract_fact() — extract fact from a completed step result
  build_digest()  — build compact state digest from accumulated facts
  _compact_log()  — sliding-window log compaction with digest injection
  _compact_tool_result() — compact individual tool result for log history
  _history_action_repr() — compact assistant message for log history
"""
import json
from dataclasses import dataclass, field as dc_field


# ---------------------------------------------------------------------------
# Tool result compaction for log history
# ---------------------------------------------------------------------------

_MAX_READ_HISTORY = 4000  # chars of file content kept in history (model saw full text already)  # FIX-147


def _compact_tool_result(action_name: str, txt: str) -> str:
    """Compact tool result before storing in log history.
    The model already received the full result in the current step's user message;
    history only needs a reference-quality summary to avoid token accumulation."""
    if txt.startswith("WRITTEN:") or txt.startswith("DELETED:") or \
            txt.startswith("CREATED DIR:") or txt.startswith("MOVED:") or \
            txt.startswith("ERROR") or txt.startswith("VAULT STRUCTURE:"):
        return txt  # already compact or important verbatim

    if action_name == "Req_Read":
        try:
            d = json.loads(txt)
            content = d.get("content", "")
            path = d.get("path", "")
            if len(content) > _MAX_READ_HISTORY:
                return f"{path}: {content[:_MAX_READ_HISTORY]}...[+{len(content) - _MAX_READ_HISTORY} chars]"
        except (json.JSONDecodeError, ValueError):
            pass
        return txt[:_MAX_READ_HISTORY + 30] + ("..." if len(txt) > _MAX_READ_HISTORY + 30 else "")

    if action_name == "Req_List":
        try:
            d = json.loads(txt)
            names = [e["name"] for e in d.get("entries", [])]
            return f"entries: {', '.join(names)}" if names else "entries: (empty)"
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    if action_name == "Req_Search":
        try:
            d = json.loads(txt)
            hits = [f"{m['path']}:{m.get('line', '')}" for m in d.get("matches", [])]
            if hits:
                return f"matches: {', '.join(hits)}"
            return "matches: (none)"
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    return txt  # fallback: unchanged


# ---------------------------------------------------------------------------
# Assistant message schema strip for log history
# ---------------------------------------------------------------------------

def _history_action_repr(action_name: str, action) -> str:
    """Compact function call representation for log history.
    Drops None/False/0/'' defaults (e.g. number=false, start_line=0) that waste tokens
    without carrying information. Full args still used for actual dispatch."""
    try:
        d = action.model_dump(exclude_none=True)
        d = {k: v for k, v in d.items() if v not in (False, 0, "")}
        args_str = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        return f"Action: {action_name}({args_str})"
    except Exception:
        return f"Action: {action_name}({action.model_dump_json()})"


# ---------------------------------------------------------------------------
# Step facts accumulation for rolling state digest
# ---------------------------------------------------------------------------

@dataclass
class _StepFact:
    """One key fact extracted from a completed step for rolling digest."""
    kind: str    # "list", "read", "search", "write", "delete", "move", "mkdir", "stall"
    path: str
    summary: str  # compact 1-line description
    error: str = dc_field(default="")  # FIX-199: preserve error details through compaction


def _extract_fact(action_name: str, action, result_txt: str) -> "_StepFact | None":
    """Extract key fact from a completed step — used to build state digest."""
    path = getattr(action, "path", getattr(action, "from_name", ""))

    if action_name == "Req_Read":
        try:
            d = json.loads(result_txt)
            content = d.get("content", "").replace("\n", " ").strip()
            if "accounts/" in path:
                # [FIX-244] Structured digest for account files: extract key lookup fields
                # instead of char-truncating. Truncation at 250 cuts off `account_manager`
                # mid-value, causing agent to hallucinate manager names from partial data.
                try:
                    acct = json.loads(d.get("content", ""))
                    summary = json.dumps(
                        {
                            "name": acct.get("name", ""),
                            "account_manager": acct.get("account_manager", ""),
                            "status": acct.get("status", ""),
                            "industry": acct.get("industry", ""),
                        },
                        ensure_ascii=False,
                    )
                    return _StepFact("read", path, summary)
                except (json.JSONDecodeError, ValueError):
                    return _StepFact("read", path, content[:250])
            elif "inbox/" in path:
                _limit = 500  # FIX-258: evaluator needs full message for cross-account check
            else:
                _limit = 120
            return _StepFact("read", path, content[:_limit])
        except (json.JSONDecodeError, ValueError):
            pass
        return _StepFact("read", path, result_txt[:80].replace("\n", " "))

    if action_name == "Req_List":
        try:
            d = json.loads(result_txt)
            names = [e["name"] for e in d.get("entries", [])]
            return _StepFact("list", path, ", ".join(names[:10]))
        except (json.JSONDecodeError, ValueError, KeyError):
            return _StepFact("list", path, result_txt[:60])

    if action_name == "Req_Search":
        try:
            d = json.loads(result_txt)
            hits = [f"{m['path']}:{m.get('line', '')}" for m in d.get("matches", [])]
            summary = ", ".join(hits) if hits else "(no matches)"
            return _StepFact("search", path, summary)
        except (json.JSONDecodeError, ValueError, KeyError):
            return _StepFact("search", path, result_txt[:60])

    # For mutating operations, check result_txt for errors before reporting success
    _is_err = result_txt.startswith("ERROR")
    _err_detail = result_txt[:120] if _is_err else ""  # FIX-199: capture error for digest
    if action_name == "Req_Write":
        summary = result_txt[:80] if _is_err else f"WRITTEN: {path}"
        return _StepFact("write", path, summary, error=_err_detail)
    if action_name == "Req_Delete":
        summary = result_txt[:80] if _is_err else f"DELETED: {path}"
        return _StepFact("delete", path, summary, error=_err_detail)
    if action_name == "Req_Move":
        to = getattr(action, "to_name", "?")
        summary = result_txt[:80] if _is_err else f"MOVED: {path} → {to}"
        return _StepFact("move", path, summary, error=_err_detail)
    if action_name == "Req_MkDir":
        summary = result_txt[:80] if _is_err else f"CREATED DIR: {path}"
        return _StepFact("mkdir", path, summary, error=_err_detail)

    # FIX-276: code_eval — track paths for auto-grounding
    # FIX-278: preserve task + result in summary (was 80 chars — too short, lost after compaction)
    if action_name == "Req_CodeEval":
        paths = getattr(action, "paths", []) or []
        task_desc = getattr(action, "task", "")[:80]
        result_short = result_txt[:150]
        summary = f"code_eval({task_desc}): {result_short}"
        return _StepFact("code_eval", ",".join(paths), summary)

    return None


def build_digest(facts: "list[_StepFact]") -> str:
    """Build compact state digest from accumulated step facts."""
    sections: dict[str, list[str]] = {
        "LISTED": [], "READ": [], "FOUND": [],
        "CODE_EVAL": [],  # FIX-278: code_eval results survive compaction
        "DONE": [],
        "ERRORS": [],   # FIX-199: preserve error details through compaction
        "STALLS": [],   # FIX-200: preserve stall events through compaction
    }
    for f in facts:
        if f.kind == "list":
            sections["LISTED"].append(f"  {f.path}: {f.summary}")
        elif f.kind == "read":
            sections["READ"].append(f"  {f.path}: {f.summary}")
        elif f.kind == "search":
            sections["FOUND"].append(f"  {f.summary}")
        elif f.kind == "code_eval":  # FIX-278: dedicated section
            sections["CODE_EVAL"].append(f"  {f.summary}")
        elif f.kind in ("write", "delete", "move", "mkdir"):
            sections["DONE"].append(f"  {f.summary}")
        elif f.kind == "stall":  # FIX-200
            sections["STALLS"].append(f"  {f.summary}")
        if f.error:  # FIX-199: errors on any kind propagate to ERRORS section
            sections["ERRORS"].append(f"  {f.kind}({f.path}): {f.error}")
    parts = [
        f"{label}:\n" + "\n".join(lines)
        for label, lines in sections.items()
        if lines
    ]
    return "State digest:\n" + ("\n".join(parts) if parts else "(no facts)")


# ---------------------------------------------------------------------------
# Log compaction (sliding window)
# ---------------------------------------------------------------------------

def _compact_log(log: list, max_tool_pairs: int = 7, preserve_prefix: list | None = None,
                 step_facts: "list[_StepFact] | None" = None) -> list:
    """Keep preserved prefix + last N assistant/tool message pairs.
    Older pairs are replaced with a single summary message.
    If step_facts provided, uses build_digest() instead of 'Actions taken:'."""
    prefix_len = len(preserve_prefix) if preserve_prefix else 0
    tail = log[prefix_len:]
    max_msgs = max_tool_pairs * 2

    if len(tail) <= max_msgs:
        return log

    old = tail[:-max_msgs]
    kept = tail[-max_msgs:]

    # Extract confirmed operations from compacted pairs (safety net for done_ops)
    confirmed_ops = []
    for msg in old:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and content:
            for line in content.splitlines():
                if line.startswith(("WRITTEN:", "DELETED:", "MOVED:", "CREATED DIR:")):
                    confirmed_ops.append(line)

    parts: list[str] = []
    if confirmed_ops:
        parts.append("Confirmed ops (already done, do NOT redo):\n" + "\n".join(f"  {op}" for op in confirmed_ops))

    # Use ALL accumulated step facts as the complete state digest.
    # Always use the full step_facts list — never slice by old_step_count, because:
    # 1. Extra injected messages (auto-lists, stall hints, JSON retries) shift len(old)//2
    # 2. After a previous compaction the old summary message itself lands in `old`, skewing the count
    # 3. step_facts is the authoritative ground truth regardless of how many compactions occurred
    if step_facts:
        parts.append(build_digest(step_facts))
        print(f"\x1B[33m[compact] Compacted {len(old)} msgs into digest ({len(step_facts)} facts)\x1B[0m")
    else:
        # Fallback: plain text summary from assistant messages (legacy behaviour)
        summary_parts = []
        for msg in old:
            if msg.get("role") == "assistant" and msg.get("content"):
                summary_parts.append(f"- {msg['content'][:120]}")
        if summary_parts:
            parts.append("Actions taken:\n" + "\n".join(summary_parts[-5:]))

    summary = "Previous steps summary:\n" + ("\n".join(parts) if parts else "(none)")

    base = preserve_prefix if preserve_prefix is not None else log[:prefix_len]
    return list(base) + [{"role": "user", "content": summary}] + kept
