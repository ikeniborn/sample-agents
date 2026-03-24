import hashlib
import json
import re
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel

from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import ListRequest, WriteRequest

from .models import Navigate, Inspect, Modify, Finish, MicroStep

# Keywords identifying policy/skill/rule files — used in prephase probing and loop tracking
POLICY_KEYWORDS = ("skill", "policy", "retention", "rule", "config", "hints", "schema")


def _truncate(text: str, max_len: int = 4000) -> str:
    """Truncate text and append marker if it exceeds max_len."""
    if len(text) > max_len:
        return text[:max_len] + "\n... (truncated)"
    return text


def _action_hash(action: BaseModel) -> str:
    """Hash action type+params for loop detection."""
    if isinstance(action, Navigate):
        key = f"navigate:{action.action}:{action.path}"
    elif isinstance(action, Inspect):
        key = f"inspect:{action.action}:{action.path}:{action.pattern}"
    elif isinstance(action, Modify):
        key = f"modify:{action.action}:{action.path}"
    elif isinstance(action, Finish):
        key = "finish"
    else:
        key = str(action)
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _compact_log(log: list, max_tool_pairs: int = 7, preserve_prefix: int = 6) -> list:
    """Keep system + user + hardcoded steps + last N assistant/tool message pairs.
    Older pairs are replaced with a single summary message.
    preserve_prefix: number of initial messages to always keep
      (default 6 = system + user + tree exchange + instruction file exchange)"""
    tail = log[preserve_prefix:]
    max_msgs = max_tool_pairs * 2
    if len(tail) <= max_msgs:
        return log

    old = tail[:-max_msgs]
    kept = tail[-max_msgs:]

    summary_parts = []
    for msg in old:
        if msg["role"] == "assistant":
            summary_parts.append(f"- {msg['content']}")
    summary = "Previous steps summary:\n" + "\n".join(summary_parts[-5:])

    return log[:preserve_prefix] + [{"role": "user", "content": summary}] + kept


def _validate_write(vm: MiniRuntimeClientSync, action: Modify, read_paths: set[str],
                    all_preloaded: set[str] | None = None) -> str | None:
    """Check if write target matches existing naming patterns in the directory.
    Returns a warning string if mismatch detected, None if OK."""
    if action.action != "write":
        return None
    target_path = action.path
    content = action.content

    # Instruction-bleed guard — reject content that contains instruction text.
    INSTRUCTION_BLEED = [
        r"preserve the same folder",
        r"filename pattern",
        r"body template",
        r"naming pattern.*already in use",
        r"create exactly one",
        r"do not edit",
        r"user instruction",
        r"keep the same",
        r"same folder.*already",
        r"\[TASK-DONE\]",
        r"has been written\. The task is now COMPLETE",
        r"Call finish IMMEDIATELY",
        r"PRE-LOADED file contents",
        r"do NOT re-read them",
        r"\$\d+_AMOUNT",
        r"\$[A-Z]+_AMOUNT",
        r"^title:\s+\S",
        r"^created_on:\s",
        r"^amount:\s+\d",
        r"this is a new file",
        r"this is the path[:\.]",
        r"please pay by the write",
        r"the file (?:is |was )?(?:created|written|located)",
        r"modify\.write tool",
        r"Looking at the conversation",
        r"the action field is",
        r"I see that the action",
        r"correct tool (?:setup|based on)",
        r"you need to ensure you have",
        r"tool for file creation",
        r"\[TASK-DONE\].*has been written",
        r"Call finish IMMEDIATELY with refs",
    ]
    for pat in INSTRUCTION_BLEED:
        if re.search(pat, content, re.IGNORECASE):
            return (
                f"ERROR: content field contains forbidden text (matched '{pat}'). "
                f"Write ONLY the actual file content — no YAML frontmatter, no placeholders, no reasoning. "
                f"Use the EXACT amount from the task (e.g. $190, not $12_AMOUNT). "
                f"Example: '# Invoice #12\\n\\nAmount: $190\\n\\nThank you for your business!'"
            )

    # ASCII guard: reject paths with non-ASCII chars (model hallucination)
    if not target_path.isascii():
        return (
            f"ERROR: path '{target_path}' contains non-ASCII characters. "
            f"File paths must use only ASCII letters, digits, hyphens, underscores, dots, slashes. "
            f"Re-check the instruction file for the correct path and try again."
        )

    # Extract directory
    if "/" in target_path:
        parent_dir = target_path.rsplit("/", 1)[0] + "/"
    else:
        parent_dir = "/"
    target_name = target_path.rsplit("/", 1)[-1] if "/" in target_path else target_path

    # Reject filenames with spaces
    if ' ' in target_name:
        return (
            f"ERROR: filename '{target_name}' contains spaces, which is not allowed in file paths. "
            f"Use hyphens or underscores instead of spaces. "
            f"For example: 'INVOICE-11.md' not 'IN invoice-11.md'. "
            f"Check the naming pattern of existing files and retry."
        )

    try:
        list_result = vm.list(ListRequest(path=parent_dir))
        mapped = MessageToDict(list_result)
        files = mapped.get("files", [])
        if not files:
            effective_reads = (read_paths | all_preloaded) if all_preloaded else read_paths
            target_prefix_m = re.match(r'^([A-Za-z]+-?\d*[-_]?\d+)', target_name)
            if target_prefix_m:
                base_pattern = re.sub(r'\d+', r'\\d+', re.escape(target_prefix_m.group(1)))
                for rp in effective_reads:
                    rp_name = Path(rp).name
                    rp_dir = str(Path(rp).parent)
                    if re.match(base_pattern, rp_name, re.IGNORECASE) and rp_dir != str(Path(target_path).parent):
                        return (
                            f"ERROR: '{target_path}' looks like it belongs in '{rp_dir}/', not '{parent_dir}'. "
                            f"Files with a similar naming pattern (e.g. '{rp_name}') exist in '{rp_dir}/'. "
                            f"Use path '{rp_dir}/{target_name}' instead."
                        )
            return None

        existing_names = [f.get("name", "") for f in files if f.get("name")]
        if not existing_names:
            return None

        # Block writes to existing files (overwrite prevention).
        if target_name in existing_names:
            _f39_nums = []
            for _n in existing_names:
                for _m in re.findall(r'\d+', _n):
                    _v = int(_m)
                    if _v < 1900:
                        _f39_nums.append(_v)
            if _f39_nums:
                _f39_next = max(_f39_nums) + 1
                _f39_stem = re.sub(r'\d+', str(_f39_next), target_name, count=1)
                _f39_hint = f"The correct NEW filename is '{_f39_stem}' (ID {_f39_next})."
            else:
                _f39_hint = "Choose a filename that does NOT exist yet."
            return (
                f"ERROR: '{target_path}' ALREADY EXISTS in the vault — do NOT overwrite it. "
                f"You must create a NEW file with a new sequence number. "
                f"{_f39_hint} "
                f"Existing files in '{parent_dir}': {existing_names[:5]}."
            )

        # Read-before-write enforcement
        dir_norm = parent_dir.rstrip("/")
        effective_reads = (read_paths | all_preloaded) if all_preloaded else read_paths
        already_read = any(
            p.startswith(dir_norm + "/") or p.startswith(dir_norm)
            for p in effective_reads
        )
        if not already_read:
            sample = existing_names[0]
            return (
                f"WARNING: You are about to write '{target_name}' in '{parent_dir}', "
                f"but you haven't read any existing file from that folder yet. "
                f"MANDATORY: first read '{parent_dir}{sample}' to learn the exact format, "
                f"then retry your write with the same format."
            )

        # Check extension match
        target_ext = Path(target_name).suffix
        existing_exts = {Path(n).suffix for n in existing_names if Path(n).suffix}
        if existing_exts and target_ext and target_ext not in existing_exts:
            return (f"WARNING: You are creating '{target_name}' with extension '{target_ext}', "
                    f"but existing files in '{parent_dir}' use extensions: {existing_exts}. "
                    f"Existing files: {existing_names[:5]}. "
                    f"Please check the naming pattern and try again.")

        # Block writes with no extension when existing files have extensions.
        if existing_exts and not target_ext:
            _sample_ext = sorted(existing_exts)[0]
            return (
                f"WARNING: You are creating '{target_name}' without a file extension, "
                f"but existing files in '{parent_dir}' use extensions: {existing_exts}. "
                f"Existing files: {existing_names[:5]}. "
                f"Add the correct extension (e.g. '{_sample_ext}') to your filename and retry."
            )

        # Check prefix pattern (e.g. PAY-, INV-, BILL-)
        existing_prefixes = set()
        for n in existing_names:
            m = re.match(r'^([A-Z]+-)', n)
            if m:
                existing_prefixes.add(m.group(1))
        if existing_prefixes:
            target_prefix_match = re.match(r'^([A-Z]+-)', target_name)
            target_prefix = target_prefix_match.group(1) if target_prefix_match else None
            if target_prefix and target_prefix not in existing_prefixes:
                return (f"WARNING: You are creating '{target_name}' with prefix '{target_prefix}', "
                        f"but existing files in '{parent_dir}' use prefixes: {existing_prefixes}. "
                        f"Existing files: {existing_names[:5]}. "
                        f"Please check the naming pattern and try again.")
            if not target_prefix:
                _sample_existing = existing_names[0]
                return (f"WARNING: You are creating '{target_name}' but it does not follow the naming "
                        f"pattern used in '{parent_dir}'. Existing files use prefixes: {existing_prefixes}. "
                        f"Example: '{_sample_existing}'. "
                        f"Use the same prefix pattern (e.g. '{next(iter(existing_prefixes))}N.ext') and retry.")

        return None
    except Exception:
        effective_reads = (read_paths | all_preloaded) if all_preloaded else read_paths
        target_prefix_m = re.match(r'^([A-Za-z]+-?\d*[-_]?\d+)', target_name)
        if target_prefix_m:
            base_pattern = re.sub(r'\d+', r'\\d+', re.escape(target_prefix_m.group(1)))
            for rp in effective_reads:
                rp_name = Path(rp).name
                rp_dir = str(Path(rp).parent)
                if (re.match(base_pattern, rp_name, re.IGNORECASE)
                        and rp_dir != str(Path(target_path).parent)):
                    return (
                        f"ERROR: '{target_path}' looks like it belongs in '{rp_dir}/', not '{parent_dir}'. "
                        f"Files with a similar naming pattern (e.g. '{rp_name}') exist in '{rp_dir}/'. "
                        f"Use path '{rp_dir}/{target_name}' instead."
                    )
        return None


def _try_parse_microstep(raw: str) -> MicroStep | None:
    """Try to parse MicroStep from raw JSON string."""
    try:
        data = json.loads(raw)
        return MicroStep.model_validate(data)
    except Exception:
        return None


def _ancestors(path: str) -> set[str]:
    """Extract all ancestor directories from a file path.
    "a/b/c/file.md" → {"a/", "a/b/", "a/b/c/"}
    """
    parts = path.split("/")
    result = set()
    for i in range(1, len(parts)):
        result.add("/".join(parts[:i]) + "/")
    return result


def _build_vault_map(tree_data: dict, max_chars: int = 3000) -> str:
    """Build a compact indented text map of the vault from outline data."""
    files = tree_data.get("files", [])
    if not files:
        return "(empty vault)"

    dir_files: dict[str, list[tuple[str, list[str]]]] = {}
    all_dirs: set[str] = set()

    for f in files:
        fpath = f.get("path", "")
        if not fpath:
            continue
        headers = [h for h in f.get("headers", []) if isinstance(h, str) and h]
        if "/" in fpath:
            parent = fpath.rsplit("/", 1)[0] + "/"
            fname = fpath.rsplit("/", 1)[1]
        else:
            parent = "/"
            fname = fpath
        dir_files.setdefault(parent, []).append((fname, headers))
        all_dirs.update(_ancestors(fpath))

    dir_total: dict[str, int] = {}
    for d in all_dirs | {"/"}:
        count = 0
        for fpath_entry in files:
            fp = fpath_entry.get("path", "")
            if d == "/" or fp.startswith(d.rstrip("/") + "/") or (d == "/" and "/" not in fp):
                count += 1
        dir_total[d] = count
    dir_total["/"] = len(files)

    lines: list[str] = []
    max_files_per_dir = 8
    first_n = 5

    def render_dir(d: str, depth: int):
        indent = "  " * depth
        child_dirs = sorted([
            cd for cd in all_dirs
            if cd != d and cd.startswith(d if d != "/" else "")
            and cd[len(d if d != "/" else ""):].count("/") == 1
        ])
        if d == "/":
            child_dirs = sorted([cd for cd in all_dirs if cd.count("/") == 1])

        dir_entries = dir_files.get(d, [])

        items: list[tuple[str, str | None]] = []
        for fname, _hdrs in dir_entries:
            items.append((fname, "file"))
        for cd in child_dirs:
            dirname = cd.rstrip("/").rsplit("/", 1)[-1] if "/" in cd.rstrip("/") else cd.rstrip("/")
            items.append((dirname + "/", "dir"))

        items.sort(key=lambda x: x[0].lower())

        file_count = 0
        for name, kind in items:
            if kind == "dir":
                cd_path = (d if d != "/" else "") + name
                total = dir_total.get(cd_path, 0)
                lines.append(f"{indent}{name} ({total} files)")
                render_dir(cd_path, depth + 1)
            else:
                file_count += 1
                if file_count <= first_n or len(dir_entries) <= max_files_per_dir:
                    hdrs = []
                    for fn, h in dir_entries:
                        if fn == name:
                            hdrs = h
                            break
                    hdr_str = f" [{', '.join(hdrs[:3])}]" if hdrs else ""
                    lines.append(f"{indent}{name}{hdr_str}")
                elif file_count == first_n + 1:
                    remaining = len(dir_entries) - first_n
                    lines.append(f"{indent}... (+{remaining} more)")

    total = len(files)
    lines.append(f"/ ({total} files)")
    render_dir("/", 1)

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... (truncated)"
    return result


def _extract_task_dirs(task_text: str, known_dirs: set[str]) -> list[str]:
    """Extract task-relevant directories by matching path-like tokens and keywords."""
    matches: set[str] = set()

    path_tokens = re.findall(r'[\w./-]{2,}/', task_text)
    for token in path_tokens:
        token_clean = token if token.endswith("/") else token + "/"
        if token_clean in known_dirs:
            matches.add(token_clean)

    task_words = set(re.findall(r'[a-zA-Z]{3,}', task_text.lower()))
    for d in known_dirs:
        dir_name = d.rstrip("/").rsplit("/", 1)[-1].lower() if "/" in d.rstrip("/") else d.rstrip("/").lower()
        if dir_name in task_words:
            matches.add(d)

    return sorted(matches, key=lambda x: x.count("/"), reverse=True)[:2]


def _extract_dirs_from_text(text: str) -> list[str]:
    """Extract potential directory names mentioned in text."""
    dirs: list[str] = []
    for m in re.finditer(r'\b([a-zA-Z][\w-]*)/\b', text):
        dirs.append(m.group(1))
    for m in re.finditer(r'\b(\w+)\s+(?:folder|directory|dir)\b', text, re.IGNORECASE):
        dirs.append(m.group(1))
    for m in re.finditer(r'(?:folder|directory|dir)\s+(\w+)\b', text, re.IGNORECASE):
        dirs.append(m.group(1))
    for m in re.finditer(r'(?:outline of|scan|scan the|check|explore)\s+(\w+)\b', text, re.IGNORECASE):
        dirs.append(m.group(1))
    seen = set()
    result = []
    noise = {"the", "a", "an", "and", "or", "for", "with", "from", "this", "that",
             "file", "files", "your", "all", "any", "each", "existing", "relevant",
             "new", "next", "first", "when", "before", "after", "use", "not"}
    for d in dirs:
        dl = d.lower()
        if dl not in seen and dl not in noise and len(dl) >= 2:
            seen.add(dl)
            result.append(d)
    return result


def _is_valid_path(path: str) -> bool:
    """Check if a string looks like a valid file/folder path (not a description)."""
    if not path:
        return False
    if "?" in path:
        return False
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return False
    invalid_chars = set('{}|*<>:;"\'\\!@#$%^&+=[]`~,')
    if any(c in invalid_chars for c in path):
        return False
    if " " in path:
        return False
    if len(path) > 200:
        return False
    return True


def _clean_ref(path: str) -> str | None:
    """Clean and validate a ref path. Returns cleaned path or None if invalid."""
    if not path:
        return None
    path = path.lstrip("/")
    if not path:
        return None
    # Reject paths with uppercase directory components that look hallucinated
    parts = path.split("/")
    if len(parts) > 1:
        for part in parts[:-1]:  # check directory parts (not filename)
            if part.isupper() and len(part) > 3 and part not in ("MD",):
                return None
    if not _is_valid_path(path):
        return None
    return path
