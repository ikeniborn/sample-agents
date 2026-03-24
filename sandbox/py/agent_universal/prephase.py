import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from google.protobuf.json_format import MessageToDict

from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import ListRequest, OutlineRequest, ReadRequest, SearchRequest

from .dispatch import CLI_CLR, CLI_GREEN, CLI_RED, CLI_YELLOW
from .helpers import (
    POLICY_KEYWORDS,
    _ancestors,
    _build_vault_map,
    _extract_dirs_from_text,
    _extract_task_dirs,
    _truncate,
)

# ---------------------------------------------------------------------------
# Instruction file discovery
# ---------------------------------------------------------------------------

INSTRUCTION_FILE_NAMES = [
    "AGENTS.MD", "INSTRUCTIONS.md", "RULES.md", "GUIDE.md", "README.md"
]


def _find_instruction_file(all_file_contents: dict[str, str]) -> tuple[str, str]:
    """Find the primary instruction file from pre-loaded contents.
    Returns (filename, content) or ("", "") if none found."""
    for name in INSTRUCTION_FILE_NAMES:
        if name in all_file_contents and len(all_file_contents[name]) > 0:
            return name, all_file_contents[name]
    return "", ""


# ---------------------------------------------------------------------------
# PrephaseResult
# ---------------------------------------------------------------------------

@dataclass
class PrephaseResult:
    log: list
    preserve_prefix: int
    all_file_contents: dict[str, str]
    all_dirs: set[str]
    instruction_file_name: str          # e.g. "AGENTS.MD" or "RULES.md"
    instruction_file_redirect_target: str  # non-empty when instruction file redirects
    auto_refs: set[str]
    all_reads_ever: set[str]
    pre_phase_policy_refs: set[str]
    has_write_task_dirs: bool = False   # True when probe found content directories


# ---------------------------------------------------------------------------
# Pre-phase runner
# ---------------------------------------------------------------------------

def run_prephase(vm: MiniRuntimeClientSync, task_text: str, system_prompt: str) -> PrephaseResult:
    log = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_text},
    ]

    # --- Step 1: outline "/" to get all files ---
    tree_data = {}
    try:
        tree_result = vm.outline(OutlineRequest(path="/"))
        tree_data = MessageToDict(tree_result)
        print(f"{CLI_GREEN}[pre] tree /{CLI_CLR}: {len(tree_data.get('files', []))} files")
    except Exception as e:
        print(f"{CLI_RED}[pre] tree / failed: {e}{CLI_CLR}")

    vault_map = _build_vault_map(tree_data)
    print(f"{CLI_GREEN}[pre] vault map{CLI_CLR}:\n{vault_map[:500]}...")

    # Extract all known dirs for targeted listing
    all_dirs: set[str] = set()
    for f in tree_data.get("files", []):
        all_dirs.update(_ancestors(f.get("path", "")))

    # Auto-list ALL top-level subdirectories from tree (max 5)
    targeted_details = ""
    top_dirs = sorted([d for d in all_dirs if d.count("/") == 1])[:5]
    for d in top_dirs:
        try:
            lr = vm.list(ListRequest(path=d))
            lt = _truncate(json.dumps(MessageToDict(lr), indent=2), 1500)
            if lt.strip() != "{}":
                targeted_details += f"\n--- {d} ---\n{lt}"
                print(f"{CLI_GREEN}[pre] list {d}{CLI_CLR}: {lt[:200]}...")
        except Exception as e:
            print(f"{CLI_YELLOW}[pre] list {d} failed: {e}{CLI_CLR}")

    # Also list task-relevant dirs not already covered
    task_dirs = _extract_task_dirs(task_text, all_dirs)
    for d in task_dirs:
        if d not in top_dirs:
            try:
                lr = vm.list(ListRequest(path=d))
                lt = _truncate(json.dumps(MessageToDict(lr), indent=2), 1500)
                if lt.strip() != "{}":
                    targeted_details += f"\n--- {d} ---\n{lt}"
                    print(f"{CLI_GREEN}[pre] list {d}{CLI_CLR}: {lt[:200]}...")
            except Exception as e:
                print(f"{CLI_YELLOW}[pre] list {d} failed: {e}{CLI_CLR}")

    pre_result = f"Vault map:\n{vault_map}"
    if targeted_details:
        pre_result += f"\n\nDetailed listings:{targeted_details}"

    log.append({"role": "assistant", "content": json.dumps({
        "think": "See vault structure.",
        "prev_result_ok": True, "action": {"tool": "navigate", "action": "tree", "path": "/"}
    })})
    log.append({"role": "user", "content": pre_result})

    # --- Step 2: read ALL files visible in tree ---
    all_file_contents: dict[str, str] = {}

    for f in tree_data.get("files", []):
        fpath = f.get("path", "")
        if not fpath:
            continue
        try:
            read_r = vm.read(ReadRequest(path=fpath))
            read_d = MessageToDict(read_r)
            content = read_d.get("content", "")
            if content:
                all_file_contents[fpath] = content
                print(f"{CLI_GREEN}[pre] read {fpath}{CLI_CLR}: {len(content)} chars")
        except Exception as e:
            print(f"{CLI_YELLOW}[pre] read {fpath} failed: {e}{CLI_CLR}")

    # Find instruction file
    instruction_file_name, instruction_content = _find_instruction_file(all_file_contents)
    if instruction_file_name:
        print(f"{CLI_GREEN}[pre] instruction file: {instruction_file_name}{CLI_CLR}")
    else:
        print(f"{CLI_YELLOW}[pre] no instruction file found{CLI_CLR}")

    # Build combined file contents message
    files_summary = ""

    # Redirect detection: if instruction file is a short redirect, add prominent notice
    instruction_file_redirect_target: str = ""
    instr_raw = all_file_contents.get(instruction_file_name, "") if instruction_file_name else ""
    if instruction_file_name and 0 < len(instr_raw) < 50:
        redirect_target = None
        for rpat in [r"[Ss]ee\s+'([^']+\.MD)'", r"[Ss]ee\s+\"([^\"]+\.MD)\"",
                     r"[Ss]ee\s+([A-Z][A-Z0-9_-]*\.MD)\b", r"[Rr]ead\s+([A-Z][A-Z0-9_-]*\.MD)\b"]:
            rm = re.search(rpat, instr_raw)
            if rm:
                redirect_target = rm.group(1)
                instruction_file_redirect_target = redirect_target
                break
        if redirect_target:
            _redir_content = all_file_contents.get(redirect_target, "")
            files_summary += (
                f"⚠ CRITICAL OVERRIDE: {instruction_file_name} is ONLY a redirect stub ({len(instr_raw)} chars). "
                f"The ONLY file with task rules is '{redirect_target}'. "
                f"IGNORE your own knowledge, IGNORE all other vault files. "
                f"Even if you know the factual answer to the task question, you MUST follow '{redirect_target}' EXACTLY. "
                f"'{redirect_target}' content: {_redir_content[:300]}\n"
                f"Read ONLY '{redirect_target}' above and call finish IMMEDIATELY with the keyword it specifies.\n"
            )
            print(f"{CLI_YELLOW}[pre] redirect notice: {instruction_file_name} → {redirect_target}{CLI_CLR}")

    for fpath, content in all_file_contents.items():
        files_summary += f"\n--- {fpath} ---\n{_truncate(content, 2000)}\n"

    log.append({"role": "assistant", "content": json.dumps({
        "think": "Read all vault files for context and rules.",
        "prev_result_ok": True,
        "action": {"tool": "inspect", "action": "read",
                   "path": instruction_file_name or "AGENTS.MD"}
    })})
    # FORMAT NOTE: Match the EXACT format of pre-loaded examples
    files_summary += (
        "\n\nFORMAT NOTE: Match the EXACT format of pre-loaded examples (same field names, "
        "same structure, no added/removed markdown headers like '# Title')."
    )
    log.append({"role": "user", "content": f"PRE-LOADED file contents (use these directly — do NOT re-read them):{files_summary}"})

    # --- Step 2b: auto-follow references in instruction file ---
    _auto_followed: set[str] = set()
    if instruction_content:
        ref_patterns = [
            r"[Ss]ee\s+'([^']+\.MD)'",
            r"[Ss]ee\s+\"([^\"]+\.MD)\"",
            r"[Rr]efer\s+to\s+'?([^'\"]+\.MD)'?",
            r"[Ss]ee\s+([A-Z][A-Z0-9_-]*\.MD)\b",
            r"[Rr]ead\s+([A-Z][A-Z0-9_-]*\.MD)\b",
            r"check\s+([A-Z][A-Z0-9_-]*\.MD)\b",
        ]
        for pat in ref_patterns:
            for m in re.finditer(pat, instruction_content):
                ref_file = m.group(1)
                if ref_file not in all_file_contents:
                    try:
                        ref_r = vm.read(ReadRequest(path=ref_file))
                        ref_d = MessageToDict(ref_r)
                        ref_content = ref_d.get("content", "")
                        if ref_content:
                            all_file_contents[ref_file] = ref_content
                            _auto_followed.add(ref_file)
                            files_summary += f"\n--- {ref_file} (referenced by {instruction_file_name}) ---\n{_truncate(ref_content, 2000)}\n"
                            log[-1]["content"] = f"PRE-LOADED file contents (use these directly — do NOT re-read them):{files_summary}"
                            print(f"{CLI_GREEN}[pre] auto-follow {ref_file}{CLI_CLR}: {len(ref_content)} chars")
                    except Exception as e:
                        print(f"{CLI_YELLOW}[pre] auto-follow {ref_file} failed: {e}{CLI_CLR}")

    # --- Step 2c: extract directory paths from ALL file contents ---
    content_mentioned_dirs: set[str] = set()
    for fpath, content in all_file_contents.items():
        for m in re.finditer(r'\b([a-z][\w-]*/[\w-]+(?:/[\w-]+)*)/?\b', content):
            candidate = m.group(1)
            if len(candidate) > 2 and candidate not in all_dirs:
                content_mentioned_dirs.add(candidate)
        for d in _extract_dirs_from_text(content):
            if d.lower() not in {ad.rstrip("/").lower() for ad in all_dirs}:
                content_mentioned_dirs.add(d)

    pre_phase_policy_refs: set[str] = set()

    # Probe content-mentioned directories
    for cd in sorted(content_mentioned_dirs)[:10]:
        if any(cd + "/" == d or cd == d.rstrip("/") for d in all_dirs):
            continue
        try:
            probe_r = vm.outline(OutlineRequest(path=cd))
            probe_d = MessageToDict(probe_r)
            probe_files = probe_d.get("files", [])
            if probe_files:
                print(f"{CLI_GREEN}[pre] content-probe {cd}/{CLI_CLR}: {len(probe_files)} files")
                all_dirs.add(cd + "/")
                to_read = [pf for pf in probe_files
                           if any(kw in pf.get("path", "").lower() for kw in POLICY_KEYWORDS)]
                if not to_read:
                    to_read = probe_files[:1]
                for pf in to_read[:3]:
                    pfp = pf.get("path", "")
                    if pfp:
                        if "/" not in pfp:
                            pfp = cd.rstrip("/") + "/" + pfp
                    if pfp and pfp not in all_file_contents:
                        try:
                            pr = vm.read(ReadRequest(path=pfp))
                            prd = MessageToDict(pr)
                            prc = prd.get("content", "")
                            if prc:
                                all_file_contents[pfp] = prc
                                files_summary += f"\n--- {pfp} (discovered) ---\n{_truncate(prc, 1500)}\n"
                                log[-1]["content"] = f"PRE-LOADED file contents (use these directly — do NOT re-read them):{files_summary}"
                                print(f"{CLI_GREEN}[pre] read {pfp}{CLI_CLR}: {len(prc)} chars")
                                _fname2 = Path(pfp).name.lower()
                                if any(kw in _fname2 for kw in POLICY_KEYWORDS):
                                    pre_phase_policy_refs.add(pfp)
                                for m2 in re.finditer(r'\b([a-z][\w-]*/[\w-]+(?:/[\w-]+)*)/?\b', prc):
                                    cand2 = m2.group(1)
                                    if len(cand2) > 2 and cand2 not in all_dirs:
                                        content_mentioned_dirs.add(cand2)
                        except Exception:
                            pass
        except Exception:
            pass

    # --- Step 3: auto-explore directories mentioned in instruction file ---
    explored_dirs_info = ""
    if instruction_content:
        mentioned_dirs = _extract_dirs_from_text(instruction_content)
        for dname in mentioned_dirs[:3]:
            try:
                tree_r = vm.outline(OutlineRequest(path=dname))
                tree_d = MessageToDict(tree_r)
                dir_files = tree_d.get("files", [])
                if dir_files:
                    file_list = ", ".join(f.get("path", "") for f in dir_files[:10])
                    explored_dirs_info += f"\n{dname}/ contains: {file_list}"
                    print(f"{CLI_GREEN}[pre] tree {dname}/{CLI_CLR}: {len(dir_files)} files")
                    for df in dir_files[:2]:
                        dfp = df.get("path", "")
                        if dfp and any(kw in dfp.lower() for kw in ["policy", "retention", "skill", "rule", "config"]):
                            try:
                                read_r = vm.read(ReadRequest(path=dfp))
                                read_d = MessageToDict(read_r)
                                read_content = read_d.get("content", "")
                                if read_content:
                                    explored_dirs_info += f"\n\n--- {dfp} ---\n{_truncate(read_content, 1500)}"
                                    print(f"{CLI_GREEN}[pre] read {dfp}{CLI_CLR}: {len(read_content)} chars")
                            except Exception:
                                pass
            except Exception:
                pass

    if explored_dirs_info:
        log.append({"role": "assistant", "content": json.dumps({
            "think": "Explore directories mentioned in instruction file.",
            "prev_result_ok": True, "action": {"tool": "navigate", "action": "tree", "path": "/"}
        })})
        log.append({"role": "user", "content": f"Pre-explored directories:{explored_dirs_info}"})
        preserve_prefix = 8
    else:
        preserve_prefix = 6

    # --- Step 4: aggressive directory probing ---
    probe_dirs = [
        "docs", "inbox", "archive", "staging", "notes", "templates",
        "workspace", "projects", "ops", "admin", "data", "files",
        "my", "work", "tasks", "todo", "todos", "drafts", "billing", "invoices",
        "skills", "agent-hints", "hints", "records", "biz",
        # two-level common
        "docs/archive", "workspace/archive", "notes/archive",
        "docs/invoices", "docs/todos", "docs/tasks",
        "workspace/todos", "workspace/tasks", "workspace/notes",
        "my/invoices", "my/todos", "my/tasks",
        "work/invoices", "work/todos", "work/notes",
        "data/invoices", "data/bills", "data/todos",
        "biz/data", "biz/invoices", "biz/records",
    ]
    # Add task-relevant dirs dynamically
    dynamic_dirs = _extract_task_dirs(task_text, all_dirs)
    for d in dynamic_dirs:
        dclean = d.rstrip("/")
        if dclean not in probe_dirs:
            probe_dirs.append(dclean)

    probed_info = ""
    has_write_task_dirs = False
    for pd in probe_dirs:
        if any(pd + "/" == d or pd == d.rstrip("/") for d in all_dirs):
            continue
        try:
            probe_r = vm.outline(OutlineRequest(path=pd))
            probe_d = MessageToDict(probe_r)
            probe_files = probe_d.get("files", [])
            if probe_files:
                has_write_task_dirs = True
                file_list = ", ".join(f.get("path", "") for f in probe_files[:10])
                probed_info += f"\n{pd}/ contains: {file_list}"
                print(f"{CLI_GREEN}[pre] probe {pd}/{CLI_CLR}: {len(probe_files)} files")
                # FIX-35: Compute true numeric max-ID from all filenames
                _f35_nums: list[tuple[int, str]] = []
                for _f35_pf in probe_files:
                    _f35_name = Path(_f35_pf.get("path", "")).name
                    _f35_matches = re.findall(r'\d+', _f35_name)
                    if _f35_matches:
                        _f35_candidates = [int(x) for x in _f35_matches if int(x) < 1900]
                        if not _f35_candidates:
                            _f35_candidates = [int(_f35_matches[-1])]
                        _f35_nums.append((_f35_candidates[-1], _f35_pf.get("path", "")))
                if _f35_nums:
                    _f35_max_val, _f35_max_path = max(_f35_nums, key=lambda x: x[0])
                    _f35_next = _f35_max_val + 1
                    probed_info += (
                        f"\n[IMPORTANT: The highest existing sequence ID in {pd}/ is {_f35_max_val}"
                        f" (file: '{_f35_max_path}'). Your new file must use ID {_f35_next},"
                        f" NOT {len(probe_files) + 1} (do NOT count files).]"
                    )
                    print(f"{CLI_GREEN}[FIX-35] max-ID hint: {_f35_max_val} → next: {_f35_next}{CLI_CLR}")
                # Track discovered subdirs for recursive probing (deduplicate before calling)
                _seen_subdirs: set[str] = set()
                for pf in probe_files:
                    pfp = pf.get("path", "")
                    if "/" in pfp:
                        sub_dir = pfp.rsplit("/", 1)[0]
                        if sub_dir and sub_dir != pd and sub_dir not in _seen_subdirs:
                            _seen_subdirs.add(sub_dir)
                            try:
                                sub_r = vm.outline(OutlineRequest(path=sub_dir))
                                sub_d = MessageToDict(sub_r)
                                sub_files = sub_d.get("files", [])
                                if sub_files:
                                    sub_list = ", ".join(sf.get("path", "") for sf in sub_files[:10])
                                    probed_info += f"\n{sub_dir}/ contains: {sub_list}"
                                    print(f"{CLI_GREEN}[pre] probe {sub_dir}/{CLI_CLR}: {len(sub_files)} files")
                            except Exception:
                                pass
                _to_read_probe = [pf for pf in probe_files
                                   if any(kw in pf.get("path", "").lower() for kw in POLICY_KEYWORDS)]
                if not _to_read_probe:
                    _to_read_probe = probe_files[:1]
                    # FIX-17: Also read the highest-numeric-ID file
                    if len(probe_files) > 1:
                        _f17_nums: list[tuple[int, dict]] = []
                        for _f17_pf in probe_files:
                            _f17_name = Path(_f17_pf.get("path", "")).name
                            _f17_matches = [int(x) for x in re.findall(r'\d+', _f17_name) if int(x) < 1900]
                            if not _f17_matches:
                                _f17_matches = [int(x) for x in re.findall(r'\d+', _f17_name)]
                            if _f17_matches:
                                _f17_nums.append((_f17_matches[-1], _f17_pf))
                        if _f17_nums:
                            _f17_best = max(_f17_nums, key=lambda x: x[0])[1]
                            if _f17_best not in _to_read_probe:
                                _to_read_probe = _to_read_probe + [_f17_best]
                for pf in _to_read_probe[:4]:
                    pfp = pf.get("path", "")
                    if pfp:
                        if "/" not in pfp:
                            pfp = pd.rstrip("/") + "/" + pfp
                        if pfp in all_file_contents:
                            continue
                        try:
                            pr = vm.read(ReadRequest(path=pfp))
                            prd = MessageToDict(pr)
                            prc = prd.get("content", "")
                            if prc:
                                probed_info += f"\n\n--- {pfp} ---\n{_truncate(prc, 1000)}"
                                print(f"{CLI_GREEN}[pre] read {pfp}{CLI_CLR}: {len(prc)} chars")
                                all_file_contents[pfp] = prc
                                _fname = Path(pfp).name.lower()
                                if any(kw in _fname for kw in POLICY_KEYWORDS):
                                    pre_phase_policy_refs.add(pfp)
                        except Exception:
                            pass
        except Exception:
            pass

    if probed_info:
        if explored_dirs_info:
            log[-1]["content"] += f"\n\nAdditional directories found:{probed_info}"
        else:
            log.append({"role": "assistant", "content": json.dumps({
                "think": "Probe common directories for hidden content.",
                "prev_result_ok": True, "action": {"tool": "navigate", "action": "tree", "path": "/"}
            })})
            log.append({"role": "user", "content": f"Discovered directories:{probed_info}"})
            preserve_prefix = max(preserve_prefix, len(log))

    # --- Step 5b: extract explicit path templates from all pre-loaded files ---
    path_template_hints: list[str] = []
    path_template_re = re.compile(r'\b([a-zA-Z][\w-]*/[a-zA-Z][\w/.-]{3,})\b')
    for fpath, content in all_file_contents.items():
        for m in path_template_re.finditer(content):
            candidate = m.group(1)
            if (candidate.count("/") >= 1
                    and not candidate.startswith("http")
                    and len(candidate) < 80
                    and any(c.isalpha() for c in candidate.split("/")[-1])):
                path_template_hints.append(candidate)

    if path_template_hints:
        seen_hints: set[str] = set()
        unique_hints = []
        for h in path_template_hints:
            if h not in seen_hints:
                seen_hints.add(h)
                unique_hints.append(h)
        hint_text = (
            "PATH PATTERNS found in vault instructions:\n"
            + "\n".join(f"  - {h}" for h in unique_hints[:15])
            + "\nWhen creating files, match these patterns EXACTLY (folder, prefix, numbering, extension)."
        )
        if explored_dirs_info or probed_info:
            log[-1]["content"] += f"\n\n{hint_text}"
        else:
            log.append({"role": "assistant", "content": json.dumps({
                "think": "Extract path patterns from vault instructions.",
                "prev_result_ok": True, "action": {"tool": "navigate", "action": "tree", "path": "/"}
            })})
            log.append({"role": "user", "content": hint_text})
            preserve_prefix = max(preserve_prefix, len(log))
        print(f"{CLI_GREEN}[pre] path hints: {len(unique_hints)} patterns{CLI_CLR}")

    # --- Delete task detection: inject hint (but do NOT execute delete) ---
    task_lower = task_text.lower()
    if any(w in task_lower for w in ["delete", "remove", "discard", "clean up", "cleanup"]):
        delete_candidates: list[str] = []
        for fpath, content in all_file_contents.items():
            if fpath in pre_phase_policy_refs:
                continue
            clower = content.lower()
            if "status: done" in clower or "status: completed" in clower or "status:done" in clower:
                delete_candidates.append(fpath)
        if not delete_candidates:
            for pattern in ("Status: done", "Status: completed", "status:done",
                            "status: archived", "status: finished", "completed: true",
                            "- [x]", "DONE", "done"):
                try:
                    sr = vm.search(SearchRequest(path="/", pattern=pattern, count=5))
                    sd = MessageToDict(sr)
                    for r in (sd.get("results") or sd.get("files") or []):
                        fpath_r = r.get("path", "")
                        if fpath_r and fpath_r not in delete_candidates:
                            delete_candidates.append(fpath_r)
                            print(f"{CLI_GREEN}[pre] delete-search found: {fpath_r}{CLI_CLR}")
                except Exception:
                    pass
                if delete_candidates:
                    break
        if delete_candidates:
            target = delete_candidates[0]
            delete_hint = (
                f"DELETION TASK DETECTED. File '{target}' has Status: done and is the deletion target.\n"
                f"REQUIRED ACTION: {{'tool':'modify','action':'delete','path':'{target}'}}\n"
                f"Do NOT navigate or read further. Execute modify.delete NOW on '{target}', then call finish."
            )
            log.append({"role": "assistant", "content": json.dumps({
                "think": "Identify file to delete.",
                "prev_result_ok": True, "action": {"tool": "navigate", "action": "tree", "path": "/"}
            })})
            log.append({"role": "user", "content": delete_hint})
            preserve_prefix = max(preserve_prefix, len(log))
            print(f"{CLI_GREEN}[pre] delete hint injected for: {target}{CLI_CLR}")

    # --- Auto-ref tracking ---
    auto_refs: set[str] = set()
    if instruction_file_name:
        instr_len = len(all_file_contents.get(instruction_file_name, ""))
        if instr_len > 50:
            auto_refs.add(instruction_file_name)
    auto_refs.update(_auto_followed)
    auto_refs.update(pre_phase_policy_refs)

    all_reads_ever: set[str] = set(all_file_contents.keys())

    return PrephaseResult(
        log=log,
        preserve_prefix=preserve_prefix,
        all_file_contents=all_file_contents,
        all_dirs=all_dirs,
        instruction_file_name=instruction_file_name,
        instruction_file_redirect_target=instruction_file_redirect_target,
        auto_refs=auto_refs,
        all_reads_ever=all_reads_ever,
        pre_phase_policy_refs=pre_phase_policy_refs,
        has_write_task_dirs=has_write_task_dirs,
    )
