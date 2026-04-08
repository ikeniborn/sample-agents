import os
import re
from dataclasses import dataclass, field

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import ContextRequest, ListRequest, ReadRequest, TreeRequest

from .dispatch import CLI_BLUE, CLI_CLR, CLI_GREEN, CLI_YELLOW

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

_AGENTS_MD_BUDGET = 8000  # FIX-209: increased from 2500; full AGENTS.MD eliminates non-determinism (audit 3.3)


def _filter_agents_md(content: str, task_text: str) -> tuple[str, bool]:
    """Include full AGENTS.MD up to budget; deterministic truncation if over.

    Previous word-overlap heuristic removed — it caused non-deterministic section
    selection based on task phrasing (audit 3.3).
    """
    if len(content) <= _AGENTS_MD_BUDGET:
        return content, False
    return content[:_AGENTS_MD_BUDGET] + f"\n[...truncated — full AGENTS.MD is {len(content)} chars]", True


@dataclass
class PrephaseResult:
    log: list
    preserve_prefix: list  # messages to never compact
    agents_md_content: str = ""  # content of AGENTS.md if found
    agents_md_path: str = ""  # path where AGENTS.md was found
    # Inbox files loaded during prephase: list of (path, content) sorted alphabetically.
    # Used by _run_pre_route for preloop injection check before the main loop starts.
    inbox_files: list = field(default_factory=list)
    # Vault tree text from step 1 — passed to prompt_builder for task-specific guidance.
    vault_tree_text: str = ""


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "└── " if is_last else "├── "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(_format_tree_entry(child, prefix=child_prefix, is_last=idx == len(children) - 1))
    return lines


def _render_tree_result(result, root_path: str = "/", level: int = 2) -> str:
    """Render TreeResponse into compact shell-like output."""
    root = result.root
    if not root.name:
        body = "."
    else:
        lines = [root.name]
        children = list(root.children)
        for idx, child in enumerate(children):
            lines.extend(_format_tree_entry(child, is_last=idx == len(children) - 1))
        body = "\n".join(lines)
    level_arg = f" -L {level}" if level > 0 else ""
    return f"tree{level_arg} {root_path}\n{body}"


# Few-shot user→assistant pair — strongest signal for JSON-only output.
# Placed immediately after system prompt so the model sees its own expected format
# before any task context. More reliable than response_format for Ollama-proxied
# cloud models that ignore json_object enforcement.
# NOTE: generic path used intentionally — discovery-first principle (no vault-specific hardcoding).
_FEW_SHOT_USER = "Example: what files are in the notes folder?"
_FEW_SHOT_ASSISTANT = (
    '{"current_state":"listing notes folder to identify files",'
    '"plan_remaining_steps_brief":["list /notes","act on result"],'
    '"task_completed":false,'
    '"function":{"tool":"list","path":"/notes"}}'
)


def run_prephase(
    vm: PcmRuntimeClientSync,
    task_text: str,
    system_prompt_text: str,
) -> PrephaseResult:
    """Build the initial conversation log before the main agent loop.

    Steps performed:
    1. tree -L 2 / — captures top-level vault layout so the agent knows folder names upfront.
    2. Read AGENTS.MD — source of truth for vault semantics and folder roles.
    3. Auto-preload directories referenced in AGENTS.MD: extracts top-level dir names from
       the tree, intersects with dirs mentioned in AGENTS.MD, then recursively reads all
       non-template files from those dirs. No folder names are hardcoded — the intersection
       logic works for any vault layout.
    4. context() — task-level metadata injected by the harness (e.g. current date, user info).

    The resulting log and preserve_prefix are passed directly to run_loop(). The
    preserve_prefix is never compacted, so vault structure and AGENTS.MD remain visible
    throughout the entire task execution.
    """
    print(f"\n{CLI_BLUE}[prephase] Starting pre-phase exploration{CLI_CLR}")

    log: list = [
        {"role": "system", "content": system_prompt_text},
        {"role": "user", "content": _FEW_SHOT_USER},
        {"role": "assistant", "content": _FEW_SHOT_ASSISTANT},
    ]

    # Step 1: tree "/" -L 2 — gives the agent the top-level vault layout upfront
    print(f"{CLI_BLUE}[prephase] tree -L 2 /...{CLI_CLR}", end=" ")
    tree_txt = ""
    tree_result = None
    try:
        tree_result = vm.tree(TreeRequest(root="/", level=2))
        tree_txt = _render_tree_result(tree_result, root_path="/", level=2)
        print(f"{CLI_GREEN}ok{CLI_CLR}")
    except Exception as e:
        tree_txt = f"(tree failed: {e})"
        print(f"{CLI_YELLOW}failed: {e}{CLI_CLR}")

    # Step 2: read AGENTS.MD — source of truth for vault semantics and folder roles
    agents_md_content = ""
    agents_md_path = ""
    for candidate in ["/AGENTS.MD", "/AGENTS.md", "/02_distill/AGENTS.md"]:
        try:
            r = vm.read(ReadRequest(path=candidate))
            if r.content:
                agents_md_content = r.content
                agents_md_path = candidate
                print(f"{CLI_BLUE}[prephase] read {candidate}:{CLI_CLR} {CLI_GREEN}ok{CLI_CLR}")
                break
        except Exception:
            pass

    # Step 2.5: auto-preload directories referenced in AGENTS.MD
    # Algorithm:
    #   1. Extract top-level directory names from the tree result
    #   2. Extract directory names mentioned in AGENTS.MD (backtick or plain `name/` patterns)
    #   3. Intersection → list + read each file in those dirs (skip templates/README)
    # No hardcoded folder names — works for any vault layout.
    docs_content_parts: list[str] = []
    inbox_files: list[tuple[str, str]] = []
    if agents_md_content and tree_result is not None:
        # Top-level dirs from tree
        top_level_dirs = {entry.name for entry in tree_result.root.children if entry.children or True}
        # Dir names mentioned in AGENTS.MD: match `name/` or plain word/
        mentioned = set(re.findall(r'`?(\w[\w-]*)/`?', agents_md_content))
        # Intersect with actual dirs in vault
        to_preload = sorted(mentioned & top_level_dirs)
        # Skip dirs that are primary data stores — they are too large and agent reads selectively
        _skip_data_dirs = {"contacts", "accounts", "opportunities", "reminders", "my-invoices", "outbox", "inbox"}
        to_preload = [d for d in to_preload if d not in _skip_data_dirs]
        if to_preload:
            print(f"{CLI_BLUE}[prephase] referenced dirs to preload: {to_preload}{CLI_CLR}")
        # _read_dir: recursively reads all files from a directory path
        def _read_dir(dir_path: str, seen: set) -> None:
            try:
                entries = vm.list(ListRequest(name=dir_path))
            except Exception as e:
                print(f"{CLI_YELLOW}[prephase] {dir_path}/: {e}{CLI_CLR}")
                return
            for entry in entries.entries:
                if entry.name.startswith("_") or entry.name.upper() == "README.MD":
                    continue
                child_path = f"{dir_path}/{entry.name}"
                if child_path in seen:
                    continue
                seen.add(child_path)
                # Try to read as file first; if it fails with no content, treat as subdir
                try:
                    file_r = vm.read(ReadRequest(path=child_path))
                    if file_r.content:
                        _fc = file_r.content
                        # [FIX-133] PCM runtime may return partial content for large files.
                        # Warn agent to re-read for exact counts/enumerations.
                        if len(_fc) >= 500:
                            _fc += (
                                f"\n[PREPHASE EXCERPT — content may be partial."
                                f" For exact counts or full content use: read('{child_path}')]"
                            )
                        docs_content_parts.append(f"--- {child_path} ---\n{_fc}")
                        # Collect raw content for inbox dirs — used for preloop injection check
                        if "inbox" in dir_path.lower():
                            inbox_files.append((child_path, file_r.content))
                        print(f"{CLI_BLUE}[prephase] read {child_path}:{CLI_CLR} {CLI_GREEN}ok{CLI_CLR}")
                        if _LOG_LEVEL == "DEBUG":
                            print(f"{CLI_BLUE}[prephase] {child_path} content:\n{file_r.content}{CLI_CLR}")
                        continue
                    # [FIX-244] Empty content = file too large for preload read.
                    # Do NOT fall through to _read_dir — that would try to list the file as a
                    # directory and produce a confusing "path must reference a folder" error.
                    # Instead, annotate so the agent knows to use code_eval / read directly.
                    docs_content_parts.append(
                        f"--- {child_path} ---\n"
                        f"[FILE TOO LARGE FOR PRELOAD — use code_eval to count/query or read directly]"
                    )
                    print(f"{CLI_YELLOW}[prephase] {child_path}: empty content (too large), annotated{CLI_CLR}")
                    continue
                except Exception:
                    pass
                # [FIX-244] Exception on read (e.g. timeout for large files) —
                # if entry has a file extension it's a file, not a directory.
                # Annotate so agent uses code_eval; do NOT recurse (_read_dir would
                # call vm.list on a file path and log "path must reference a folder").
                if "." in entry.name:
                    docs_content_parts.append(
                        f"--- {child_path} ---\n"
                        f"[FILE UNREADABLE (read error/timeout) — use code_eval to count/query or read directly]"
                    )
                    print(f"{CLI_YELLOW}[prephase] {child_path}: read error (timeout?), annotated{CLI_CLR}")
                    continue
                # No file extension → treat as subdirectory, recurse
                _read_dir(child_path, seen)

        for dir_name in to_preload:
            _read_dir(f"/{dir_name}", set())

    # Inject vault layout + AGENTS.MD as context — the agent reads this to discover
    # where "cards", "threads", "inbox", etc. actually live in the vault.
    prephase_parts = [f"TASK: {task_text}", f"VAULT STRUCTURE:\n{tree_txt}"]
    if agents_md_content:
        agents_md_injected, was_filtered = _filter_agents_md(agents_md_content, task_text)
        if was_filtered:
            print(f"{CLI_YELLOW}[prephase] AGENTS.MD filtered: {len(agents_md_content)} → {len(agents_md_injected)} chars{CLI_CLR}")
        if _LOG_LEVEL == "DEBUG":
            print(f"{CLI_BLUE}[prephase] AGENTS.MD content:\n{agents_md_content}{CLI_CLR}")
        prephase_parts.append(
            f"\n{agents_md_path} CONTENT (source of truth for vault semantics):\n{agents_md_injected}"
        )
    if docs_content_parts:
        prephase_parts.append(
            "\nDOCS/ CONTENT (workflow rules — follow these exactly):\n" + "\n\n".join(docs_content_parts)
        )
    prephase_parts.append(
        "\nNOTE: Use the vault structure and AGENTS.MD above to identify actual folder "
        "paths. Verify paths with list/find before acting. Do not assume paths."
    )

    log.append({"role": "user", "content": "\n".join(prephase_parts)})

    # Step 3: context — task-level metadata from the harness
    print(f"{CLI_BLUE}[prephase] context...{CLI_CLR}", end=" ")
    try:
        ctx_result = vm.context(ContextRequest())
        if ctx_result.content:
            log.append({"role": "user", "content": f"TASK CONTEXT:\n{ctx_result.content}"})
            print(f"{CLI_GREEN}ok{CLI_CLR}")
        else:
            print(f"{CLI_YELLOW}empty{CLI_CLR}")
    except Exception as e:
        print(f"{CLI_YELLOW}not available: {e}{CLI_CLR}")

    # preserve_prefix: always kept during log compaction
    preserve_prefix = list(log)

    print(f"{CLI_BLUE}[prephase] done{CLI_CLR}")

    return PrephaseResult(
        log=log,
        preserve_prefix=preserve_prefix,
        agents_md_content=agents_md_content,
        agents_md_path=agents_md_path,
        inbox_files=sorted(inbox_files, key=lambda x: x[0]),
        vault_tree_text=tree_txt,
    )
