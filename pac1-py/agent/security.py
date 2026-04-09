"""Security gate constants and functions for the agent loop.

Extracted from loop.py to reduce God Object size.
Covers FIX-203 (injection normalization), FIX-206 (contamination),
FIX-214 (format gate), FIX-215 (inbox injection), FIX-250 (write scope).

Public API used by loop.py:
  _normalize_for_injection() — FIX-203: leet/zero-width/NFKC normalization
  _CONTAM_PATTERNS           — FIX-206: anti-contamination regexes for email body
  _FORMAT_GATE_RE            — FIX-214: inbox From:/Channel: header check
  _INBOX_INJECTION_PATTERNS  — FIX-215: injection pattern list
  _INBOX_ACTION_RE           — FIX-215: action verb detection
  _check_write_scope()       — FIX-250: mutation path guard (system/email scope)
"""
import re
import unicodedata

from .classifier import TASK_INBOX, TASK_EMAIL


# ---------------------------------------------------------------------------
# FIX-203: Text normalization for injection detection
# ---------------------------------------------------------------------------

# Strips zero-width chars, NFKC-normalizes unicode (homoglyphs → ASCII),
# and replaces common leet substitutions before injection regex matching.
_LEET_MAP = str.maketrans("01345@", "oleasa")  # 0→o, 1→l, 3→e, 4→a, 5→s, @→a
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def _normalize_for_injection(text: str) -> str:
    """FIX-203: Normalize text before injection regex check."""
    t = _ZERO_WIDTH_RE.sub("", text)
    t = unicodedata.normalize("NFKC", t)
    t = t.translate(_LEET_MAP)
    return t


# ---------------------------------------------------------------------------
# FIX-206: Body anti-contamination patterns for outbox email verification
# ---------------------------------------------------------------------------

# Detects vault paths, tree output, tool results, and system file references
# leaked into email body.
_CONTAM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/[a-zA-Z_\-]+/", re.MULTILINE), "vault path"),
    (re.compile(r"VAULT STRUCTURE:"), "vault tree"),
    (re.compile(r"[├└│]──"), "tree output"),
    (re.compile(r"\bResult of Req_"), "tool result"),
    (re.compile(r"\bAGENTS\.MD\b"), "system file ref"),
]

# ---------------------------------------------------------------------------
# FIX-214: Format gate — inbox message must have From: or Channel: header
# ---------------------------------------------------------------------------

_FORMAT_GATE_RE = re.compile(r"^\s*(from|channel)\s*:", re.IGNORECASE | re.MULTILINE)

# ---------------------------------------------------------------------------
# FIX-215: Inbox injection patterns — code-level security check
# ---------------------------------------------------------------------------

_INBOX_INJECTION_PATTERNS = [
    re.compile(r"(read|list|open|check|inspect)\s+(docs/|AGENTS|otp\.txt)", re.IGNORECASE),
    re.compile(r"(override|escalat|jailbreak|bypass|system\s*override|forget\s*(your|the)\s*rules)", re.IGNORECASE),
    re.compile(r"(you\s+are\s+now|as\s+admin|special\s+authority)", re.IGNORECASE),
    re.compile(r"if\s+(char|otp|the\s+first)", re.IGNORECASE),
]
_INBOX_ACTION_RE = re.compile(r"\b(please\s+do|follow\s+this|run|execute)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# FIX-250: Write-scope code enforcement (FIX-208)
# ---------------------------------------------------------------------------

_SYSTEM_PATH_PREFIXES = ("/docs/",)
_SYSTEM_PATHS_EXACT = frozenset({"/AGENTS.MD", "/AGENTS.md"})
_OTP_PATH = "/docs/channels/otp.txt"


def _check_write_scope(action, action_name: str, task_type: str) -> str | None:
    """Return error message if mutation violates write-scope, else None.

    Layer 1 (all types): deny system paths (docs/, AGENTS.MD).
      Exception: inbox + Req_Delete + otp.txt (OTP elevation).
    Layer 2 (email only): allow-list — only /outbox/ paths.
    """
    paths_to_check: list[str] = []
    if hasattr(action, "path") and action.path:
        paths_to_check.append(action.path)
    if hasattr(action, "from_name") and action.from_name:
        paths_to_check.append(action.from_name)
    if hasattr(action, "to_name") and action.to_name:
        paths_to_check.append(action.to_name)

    for p in paths_to_check:
        is_system = p in _SYSTEM_PATHS_EXACT or any(
            p.startswith(pfx) for pfx in _SYSTEM_PATH_PREFIXES
        )
        if is_system:
            if task_type == TASK_INBOX and action_name == "Req_Delete" and p == _OTP_PATH:
                continue
            return (
                f"Blocked: {action_name} targets system path '{p}'. "
                "System files (docs/, AGENTS.MD) are read-only. "
                "Choose a different target path."
            )
        if task_type == TASK_EMAIL and not p.startswith("/outbox/"):
            return (
                f"Blocked: {action_name} targets '{p}' but email tasks may only "
                "write to /outbox/. Use report_completion if no outbox write is needed."
            )

    return None
