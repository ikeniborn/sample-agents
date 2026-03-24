import json
import re
import time
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from connectrpc.errors import ConnectError

from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import AnswerRequest, WriteRequest

from .dispatch import CLI_RED, CLI_GREEN, CLI_CLR, CLI_YELLOW, CLI_BLUE, client, dispatch
from .helpers import (
    POLICY_KEYWORDS,
    _action_hash,
    _clean_ref,
    _compact_log,
    _is_valid_path,
    _truncate,
    _try_parse_microstep,
    _validate_write,
)
from .models import Navigate, Inspect, Modify, Finish, MicroStep
from .prephase import PrephaseResult

# Month name → zero-padded number (for date parsing in task text)
_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def run_loop(vm: MiniRuntimeClientSync, model: str, task_text: str,
             pre: PrephaseResult, cfg: dict) -> None:
    log = pre.log
    preserve_prefix = pre.preserve_prefix
    all_file_contents = pre.all_file_contents
    instruction_file_name = pre.instruction_file_name
    instruction_file_redirect_target = pre.instruction_file_redirect_target
    auto_refs = pre.auto_refs
    all_reads_ever = pre.all_reads_ever
    has_write_task_dirs = pre.has_write_task_dirs

    task_lower = task_text.lower()

    # FIX-9: Track successfully written file paths to prevent duplicate writes
    confirmed_writes: dict[str, int] = {}  # path → step number of first successful write

    # Loop detection state
    last_hashes: list[str] = []
    last_tool_type: str = ""
    consec_tool_count: int = 0
    parse_failures = 0
    total_escalations = 0
    max_steps = 20
    _nav_root_count = 0  # counts nav-root intercepts (FIX-25)

    _f25_redirect_loaded = bool(
        instruction_file_redirect_target
        and all_file_contents.get(instruction_file_redirect_target)
    )
    instr_len = len(all_file_contents.get(instruction_file_name, "")) if instruction_file_name else 0

    for i in range(max_steps):
        step_label = f"step_{i + 1}"
        print(f"\n{CLI_BLUE}--- {step_label} ---{CLI_CLR} ", end="")

        # Compact log to prevent token overflow
        log = _compact_log(log, max_tool_pairs=5, preserve_prefix=preserve_prefix)

        # --- LLM call with retry (FIX-27) ---
        job = None
        raw_content = ""

        max_tokens = cfg.get("max_completion_tokens", 2048)
        _transient_kws = ("503", "502", "NoneType", "overloaded", "unavailable", "server error")
        for _api_attempt in range(4):
            try:
                resp = client.beta.chat.completions.parse(
                    model=model,
                    response_format=MicroStep,
                    messages=log,
                    max_completion_tokens=max_tokens,
                )
                msg = resp.choices[0].message
                job = msg.parsed
                raw_content = msg.content or ""
                break
            except Exception as e:
                _err_str = str(e)
                _is_transient = any(kw.lower() in _err_str.lower() for kw in _transient_kws)
                if _is_transient and _api_attempt < 3:
                    print(f"{CLI_YELLOW}[FIX-27] Transient error (attempt {_api_attempt+1}): {e} — retrying in 4s{CLI_CLR}")
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}LLM call error: {e}{CLI_CLR}")
                raw_content = ""
                break

        # Fallback: try json.loads + model_validate if parsed is None
        if job is None and raw_content:
            print(f"{CLI_YELLOW}parsed=None, trying fallback...{CLI_CLR}")
            job = _try_parse_microstep(raw_content)

        if job is None:
            parse_failures += 1
            print(f"{CLI_RED}Parse failure #{parse_failures}{CLI_CLR}")
            if parse_failures >= 3:
                print(f"{CLI_RED}3 consecutive parse failures, force finishing{CLI_CLR}")
                try:
                    vm.answer(AnswerRequest(
                        answer="Agent failed: unable to parse LLM response",
                        refs=[],
                    ))
                except Exception:
                    pass
                break
            log.append({"role": "assistant", "content": raw_content or "{}"})
            log.append({"role": "user", "content": "Your response was not valid JSON matching the schema. Please try again with a valid MicroStep JSON."})
            continue

        parse_failures = 0

        # --- Print step info ---
        print(f"think: {job.think}")
        if not job.prev_result_ok and job.prev_result_problem:
            print(f"  {CLI_YELLOW}problem: {job.prev_result_problem}{CLI_CLR}")
        print(f"  action: {job.action}")

        # --- Path validation for inspect/navigate ---
        if isinstance(job.action, (Inspect, Navigate)):
            if not _is_valid_path(job.action.path):
                bad_path = job.action.path
                print(f"{CLI_YELLOW}BAD PATH: '{bad_path}' — not a valid path{CLI_CLR}")
                log.append({"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)})
                log.append({"role": "user", "content":
                    f"ERROR: '{bad_path}' is not a valid path. "
                    f"The 'path' field must be a filesystem path like 'ops/retention.md' or 'docs/guide.md'. "
                    f"It must NOT contain spaces, questions, or descriptions. Try again with a correct path."})
                continue

        # --- FIX-25: navigate.tree on "/" when instruction file already loaded → inject reminder ---
        if (isinstance(job.action, Navigate) and job.action.action == "tree"
                and job.action.path.strip("/") == ""
                and i >= 1
                and (instr_len > 50 or _f25_redirect_loaded)
                and not confirmed_writes):
            _nav_root_count += 1
            # After 3 intercepts, force-finish
            if _nav_root_count >= 3:
                _f28_ans = ""
                # Scan recent think fields for a repeated short uppercase keyword
                _f28_word_counts: dict[str, int] = {}
                for _f28_msg in reversed(log[-16:]):
                    if _f28_msg["role"] == "assistant":
                        try:
                            _f28_think = json.loads(_f28_msg["content"]).get("think", "")
                            for _f28_m in re.finditer(r"['\"]([A-Z][A-Z0-9\-]{1,19})['\"]", _f28_think):
                                _f28_w = _f28_m.group(1)
                                if _f28_w not in ("MD", "OUT", "NOTE", "DO", "NOT"):
                                    _f28_word_counts[_f28_w] = _f28_word_counts.get(_f28_w, 0) + 1
                        except Exception:
                            pass
                if _f28_word_counts:
                    _f28_ans = max(_f28_word_counts, key=lambda k: _f28_word_counts[k])
                if not _f28_ans:
                    # Fallback: parse instruction file for 'respond with X' or 'answer with X'
                    _f28_instr = all_file_contents.get(instruction_file_name, "") if instruction_file_name else ""
                    _f28_m2 = re.search(
                        r"(?:respond|answer|reply)\s+with\s+['\"]([A-Za-z0-9\-_]+)['\"]",
                        _f28_instr, re.IGNORECASE
                    )
                    if _f28_m2:
                        _f28_ans = _f28_m2.group(1)
                # Also try redirect target
                if not _f28_ans and instruction_file_redirect_target:
                    _f28_redir_src = all_file_contents.get(instruction_file_redirect_target, "")
                    _f28_m3 = re.search(
                        r"(?:respond|answer|reply)\s+with\s+['\"]([A-Za-z0-9\-_]+)['\"]",
                        _f28_redir_src, re.IGNORECASE
                    )
                    if _f28_m3:
                        _f28_ans = _f28_m3.group(1)
                        print(f"{CLI_GREEN}[FIX-28] extracted keyword '{_f28_ans}' from redirect target{CLI_CLR}")
                if not _f28_ans:
                    _f28_ans = "Unable to complete task"
                print(f"{CLI_GREEN}[FIX-28] nav-root looped {_nav_root_count}x — force-finishing with '{_f28_ans}'{CLI_CLR}")
                _f28_refs = ([instruction_file_redirect_target]
                             if _f25_redirect_loaded and instruction_file_redirect_target
                             else list(auto_refs))
                try:
                    vm.answer(AnswerRequest(answer=_f28_ans, refs=_f28_refs))
                except Exception:
                    pass
                break

            # Build intercept message
            _instr_preview = all_file_contents.get(instruction_file_name, "")[:400] if instruction_file_name else ""
            _f25_kw = ""
            _f25_kw_src = (all_file_contents.get(instruction_file_redirect_target, "")
                           if _f25_redirect_loaded else _instr_preview)
            _f25_m = re.search(
                r"(?:respond|answer|reply)\s+with\s+['\"]([A-Za-z0-9\-_]+)['\"]",
                _f25_kw_src, re.IGNORECASE
            )
            if _f25_m:
                _f25_kw = _f25_m.group(1)
            if _f25_redirect_loaded:
                _redir_preview = all_file_contents.get(instruction_file_redirect_target, "")[:400]
                _f25_kw_hint = (
                    f"\n\nThe required answer keyword is: '{_f25_kw}'. "
                    f"Call finish IMMEDIATELY with answer='{_f25_kw}' and refs=['{instruction_file_redirect_target}']. "
                    f"Do NOT write files. Do NOT navigate. Just call finish NOW."
                ) if _f25_kw else (
                    f"\n\nRead the keyword from {instruction_file_redirect_target} above and call finish IMMEDIATELY. "
                    "Do NOT navigate again."
                )
                _nav_root_msg = (
                    f"NOTE: {instruction_file_name} redirects to {instruction_file_redirect_target}. "
                    f"Re-navigating '/' gives no new information.\n"
                    f"{instruction_file_redirect_target} content (pre-loaded):\n{_redir_preview}\n"
                    f"{_f25_kw_hint}"
                )
                print(f"{CLI_GREEN}[FIX-25] nav-root (redirect) intercepted{CLI_CLR}")
            else:
                _f25_kw_hint = (
                    f"\n\nThe required answer keyword is: '{_f25_kw}'. "
                    f"Call finish IMMEDIATELY with answer='{_f25_kw}' and refs=['{instruction_file_name}']. "
                    f"Do NOT write files. Do NOT navigate. Just call finish NOW."
                ) if _f25_kw else (
                    f"\n\nRead the keyword from {instruction_file_name} above and call finish IMMEDIATELY. "
                    "Do NOT navigate again."
                )
                _nav_root_msg = (
                    f"NOTE: You already have the vault map and all pre-loaded files from the pre-phase. "
                    f"Re-navigating '/' gives no new information.\n"
                    f"{instruction_file_name} content (pre-loaded):\n{_instr_preview}\n"
                    f"{_f25_kw_hint}"
                )
                print(f"{CLI_GREEN}[FIX-25] nav-root intercepted — injecting instruction file reminder{CLI_CLR}")
            log.append({"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)})
            log.append({"role": "user", "content": _nav_root_msg})
            continue

        # --- navigate.tree on a cached file path → serve content directly ---
        if isinstance(job.action, Navigate) and job.action.action == "tree":
            _nav_path = job.action.path.lstrip("/")
            if "." in Path(_nav_path).name:
                _cached_nav = (all_file_contents.get(_nav_path)
                               or all_file_contents.get("/" + _nav_path))
                if _cached_nav:
                    _nav_txt = _truncate(json.dumps({"path": _nav_path, "content": _cached_nav}, indent=2))
                    print(f"{CLI_GREEN}CACHE HIT (nav→file){CLI_CLR}: {_nav_path}")
                    consec_tool_count = max(0, consec_tool_count - 1)
                    # Generic hint when re-navigating instruction file
                    _nav_instr_hint = ""
                    _nav_path_upper = _nav_path.upper()
                    _instr_upper = instruction_file_name.upper() if instruction_file_name else ""
                    if (_nav_path_upper == _instr_upper and not confirmed_writes):
                        if instr_len > 50:
                            _nav_instr_hint = (
                                f"\n\nSTOP NAVIGATING. {instruction_file_name} is already loaded (shown above). "
                                f"Read the keyword it specifies and call finish NOW. "
                                f"Do NOT navigate again. Just call finish with the required keyword and refs=['{instruction_file_name}']."
                            )
                            print(f"{CLI_YELLOW}[FIX-43] instruction file nav→file loop — injecting STOP hint{CLI_CLR}")
                        elif _f25_redirect_loaded:
                            _f48_redir_content = all_file_contents.get(instruction_file_redirect_target, "")[:400]
                            _f48_kw_m = re.search(
                                r"(?:respond|answer|reply)\s+with\s+['\"]([A-Za-z0-9\-_]+)['\"]",
                                _f48_redir_content, re.IGNORECASE
                            )
                            _f48_kw = _f48_kw_m.group(1) if _f48_kw_m else ""
                            _nav_instr_hint = (
                                f"\n\nIMPORTANT: {instruction_file_name} redirects to {instruction_file_redirect_target}. "
                                f"{instruction_file_redirect_target} content:\n{_f48_redir_content}\n"
                                f"The answer keyword is: '{_f48_kw}'. "
                                f"Call finish IMMEDIATELY with answer='{_f48_kw}' and refs=['{instruction_file_redirect_target}']. "
                                f"Do NOT navigate again."
                            ) if _f48_kw else (
                                f"\n\nIMPORTANT: {instruction_file_name} redirects to {instruction_file_redirect_target}. "
                                f"Content:\n{_f48_redir_content}\n"
                                f"Read the keyword and call finish IMMEDIATELY."
                            )
                            print(f"{CLI_YELLOW}[FIX-48] instruction file redirect nav→file — injecting hint{CLI_CLR}")
                    log.append({"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)})
                    log.append({"role": "user", "content": (
                        f"NOTE: '{_nav_path}' is a FILE, not a directory. "
                        f"Its content is pre-loaded and shown below. "
                        f"Use inspect.read for files, not navigate.tree.\n"
                        f"{_nav_txt}\n"
                        f"You now have all information needed. Call finish with your answer and refs."
                        f"{_nav_instr_hint}"
                    )})
                    continue

        # --- Escalation Ladder ---
        tool_type = job.action.tool
        if tool_type == last_tool_type:
            consec_tool_count += 1
        else:
            consec_tool_count = 1
            last_tool_type = tool_type

        remaining = max_steps - i - 1

        escalation_msg = None
        if remaining <= 2 and tool_type != "finish":
            escalation_msg = f"URGENT: {remaining} steps left. Call finish NOW with your best answer. Include ALL files you read in refs."
        elif consec_tool_count >= 3 and tool_type == "navigate":
            # FIX-33: If pre-loaded JSON templates exist, inject the template so model can write immediately.
            _f33_hint = ""
            if not confirmed_writes:
                _f33_jsons = sorted(
                    [(k, v) for k, v in all_file_contents.items()
                     if k.endswith('.json') and v.strip().startswith('{')],
                    key=lambda kv: kv[0]
                )
                if _f33_jsons:
                    _f33_key, _f33_val = _f33_jsons[-1]
                    _f49n_exact = ""
                    try:
                        _f49n_tmpl = json.loads(_f33_val)
                        _f49n_new = dict(_f49n_tmpl)  # shallow copy to avoid mutating cached template
                        for _f49n_id_key in ("id", "ID"):
                            if _f49n_id_key in _f49n_new:
                                _f49n_id_val = str(_f49n_new[_f49n_id_key])
                                _f49n_nums = re.findall(r'\d+', _f49n_id_val)
                                if _f49n_nums:
                                    _f49n_old_num = _f49n_nums[-1]
                                    _f49n_new_num = str(int(_f49n_old_num) + 1).zfill(len(_f49n_old_num))
                                    _f49n_new[_f49n_id_key] = _f49n_id_val[:_f49n_id_val.rfind(_f49n_old_num)] + _f49n_new_num
                        if "title" in _f49n_new:
                            _f49n_task_clean = re.sub(r'^(?:new\s+todo\s+(?:with\s+\w+\s+prio\s*)?:?\s*|remind\s+me\s+to\s+)', '', task_text, flags=re.IGNORECASE).strip()
                            _f49n_new["title"] = _f49n_task_clean[:80] if _f49n_task_clean else task_text[:80]
                        if "priority" in _f49n_new:
                            _f49n_task_lower = task_text.lower()
                            if any(kw in _f49n_task_lower for kw in ("high prio", "high priority", "urgent", "asap", "high-prio")):
                                _f49n_new["priority"] = "pr-high"
                            elif any(kw in _f49n_task_lower for kw in ("low prio", "low priority", "low-prio")):
                                _f49n_new["priority"] = "pr-low"
                        if "due_date" in _f49n_new:
                            _f49n_date_m = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})', task_text, re.IGNORECASE)
                            if _f49n_date_m:
                                _f49n_day = _f49n_date_m.group(1).zfill(2)
                                _f49n_mon = _MONTH_MAP.get(_f49n_date_m.group(2)[:3].lower(), "01")
                                _f49n_yr = _f49n_date_m.group(3)
                                _f49n_new["due_date"] = f"{_f49n_yr}-{_f49n_mon}-{_f49n_day}"
                        _f49n_pnums = re.findall(r'\d+', Path(_f33_key).name)
                        _f49n_new_path = _f33_key
                        if _f49n_pnums:
                            _f49n_old_pnum = _f49n_pnums[-1]
                            _f49n_new_pnum = str(int(_f49n_old_pnum) + 1).zfill(len(_f49n_old_pnum))
                            _f49n_new_path = _f33_key.replace(_f49n_old_pnum, _f49n_new_pnum, 1)
                        _f49n_json_str = json.dumps(_f49n_new, separators=(',', ':'))
                        _f49n_exact = (
                            f"\n\nFIX: Call modify.write with EXACTLY these values (copy verbatim):\n"
                            f"  path: '{_f49n_new_path}'\n"
                            f"  content: {_f49n_json_str}\n"
                            f"NOTE: Priority values are 'pr-high' (high prio) or 'pr-low' (low prio). "
                            f"Do NOT use 'pr-hi', 'high', or other variants."
                        )
                    except Exception:
                        _f49n_exact = "\n\nNOTE: Priority values: use 'pr-high' for high prio, 'pr-low' for low prio."
                    _f33_hint = (
                        f"\n\nIMPORTANT: You have pre-loaded JSON template from '{_f33_key}':\n{_f33_val}\n"
                        f"Copy this STRUCTURE for your new file (increment the ID by 1). "
                        f"IMPORTANT: Replace ALL example values with values from the CURRENT TASK. "
                        f"Call modify.write NOW with the correct path and content."
                        f"{_f49n_exact}"
                    )
            escalation_msg = "You navigated enough. Now: (1) read files you found, or (2) use modify.write to create a file, or (3) call finish." + _f33_hint
        elif consec_tool_count >= 3 and tool_type == "inspect":
            _f33b_hint = ""
            if not confirmed_writes:
                _f33b_non_json = sorted(
                    [(k, v) for k, v in all_file_contents.items()
                     if not k.endswith('.json') and k.endswith('.md')
                     and k not in (instruction_file_name,)
                     and v.strip()],
                    key=lambda kv: kv[0]
                )
                _f33b_jsons = sorted(
                    [(k, v) for k, v in all_file_contents.items()
                     if k.endswith('.json') and v.strip().startswith('{')],
                    key=lambda kv: kv[0]
                )
                if _f33b_jsons:
                    _f33b_key, _f33b_val = _f33b_jsons[-1]
                    _f49_exact = ""
                    try:
                        _f49_tmpl = json.loads(_f33b_val)
                        _f49_new = dict(_f49_tmpl)  # shallow copy to avoid mutating cached template
                        for _f49_id_key in ("id", "ID"):
                            if _f49_id_key in _f49_new:
                                _f49_id_val = str(_f49_new[_f49_id_key])
                                _f49_nums = re.findall(r'\d+', _f49_id_val)
                                if _f49_nums:
                                    _f49_old_num = int(_f49_nums[-1])
                                    _f49_new_num = _f49_old_num + 1
                                    _f49_new[_f49_id_key] = _f49_id_val[:_f49_id_val.rfind(_f49_nums[-1])] + str(_f49_new_num).zfill(len(_f49_nums[-1]))
                        if "title" in _f49_new:
                            _f49_task_clean = re.sub(r'^(?:new\s+todo\s+(?:with\s+\w+\s+prio\s*)?:?\s*|remind\s+me\s+to\s+|create\s+(?:next\s+)?invoice\s+for\s+)', '', task_text, flags=re.IGNORECASE).strip()
                            _f49_new["title"] = _f49_task_clean[:80] if _f49_task_clean else task_text[:80]
                        if "priority" in _f49_new:
                            _task_lower = task_text.lower()
                            if any(kw in _task_lower for kw in ("high prio", "high priority", "urgent", "asap", "high-prio")):
                                _f49_new["priority"] = "pr-high"
                            elif any(kw in _task_lower for kw in ("low prio", "low priority", "low-prio")):
                                _f49_new["priority"] = "pr-low"
                        if "due_date" in _f49_new:
                            _f49_date_m = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})', task_text, re.IGNORECASE)
                            if _f49_date_m:
                                _f49_day = _f49_date_m.group(1).zfill(2)
                                _f49_mon = _MONTH_MAP.get(_f49_date_m.group(2)[:3].lower(), "01")
                                _f49_yr = _f49_date_m.group(3)
                                _f49_new["due_date"] = f"{_f49_yr}-{_f49_mon}-{_f49_day}"
                        _f49_tmpl_path = _f33b_key
                        _f49_new_path = _f49_tmpl_path
                        _f49_pnums = re.findall(r'\d+', Path(_f49_tmpl_path).name)
                        if _f49_pnums:
                            _f49_old_pnum = _f49_pnums[-1]
                            _f49_new_pnum = str(int(_f49_old_pnum) + 1).zfill(len(_f49_old_pnum))
                            _f49_new_path = _f49_tmpl_path.replace(_f49_old_pnum, _f49_new_pnum, 1)
                        _f49_json_str = json.dumps(_f49_new, separators=(',', ':'))
                        _f49_exact = (
                            f"\n\nFIX: Call modify.write with EXACTLY these values (copy verbatim):\n"
                            f"  path: '{_f49_new_path}'\n"
                            f"  content: {_f49_json_str}\n"
                            f"NOTE: Priority values are 'pr-high' (high prio) or 'pr-low' (low prio). "
                            f"Do NOT use 'pr-hi', 'high', or other variants."
                        )
                    except Exception:
                        _f49_exact = "\n\nNOTE: Priority values: use 'pr-high' for high prio, 'pr-low' for low prio. Do NOT use 'pr-hi'."
                    _f33b_hint = (
                        f"\n\nIMPORTANT: You have pre-loaded JSON template from '{_f33b_key}':\n{_f33b_val}\n"
                        f"Copy this STRUCTURE for your new file (increment the ID by 1). "
                        f"IMPORTANT: Replace ALL example values with values from the CURRENT TASK. "
                        f"Call modify.write NOW with the correct path and content."
                        f"{_f49_exact}"
                    )
                elif _f33b_non_json:
                    _f33b_key, _f33b_val = _f33b_non_json[-1]
                    _f33b_hint = (
                        f"\n\nIMPORTANT: You have a pre-loaded template from '{_f33b_key}':\n{repr(_f33b_val[:300])}\n"
                        f"Copy this STRUCTURE EXACTLY but change ONLY: the invoice/todo ID number and the amount/title from the task. "
                        f"Do NOT change any other text. "
                        f"Call modify.write NOW with the correct path and content."
                    )
            escalation_msg = "You inspected enough. Now: (1) use modify.write to create a file if needed, or (2) call finish with your answer and ALL file refs." + _f33b_hint

        if escalation_msg:
            total_escalations += 1
            print(f"{CLI_YELLOW}ESCALATION #{total_escalations}: {escalation_msg}{CLI_CLR}")

            if total_escalations >= 5:
                print(f"{CLI_RED}Too many escalations ({total_escalations}), force finishing{CLI_CLR}")
                force_answer = "Unable to complete task"
                _esc_src = (
                    all_file_contents.get(instruction_file_redirect_target, "")
                    or all_file_contents.get(instruction_file_name, "")
                )
                _esc_kw_m = re.search(
                    r"(?:respond|answer|reply)\s+with\s+['\"]([A-Za-z0-9\-_]+)['\"]",
                    _esc_src, re.IGNORECASE
                )
                if _esc_kw_m:
                    force_answer = _esc_kw_m.group(1)
                if force_answer == "Unable to complete task":
                    _skip_words = {"tree", "list", "read", "search", "write", "finish",
                                   "MD", "NOT", "DONE", "NULL"}
                    for prev_msg in reversed(log):
                        if prev_msg["role"] == "assistant":
                            try:
                                prev_step = json.loads(prev_msg["content"])
                                think_text = prev_step.get("think", "")
                                for qm in re.finditer(r"'([^']{2,25})'", think_text):
                                    candidate = qm.group(1).strip()
                                    if (candidate not in _skip_words
                                            and not candidate.endswith(".md")
                                            and not candidate.endswith(".MD")
                                            and not candidate.endswith(".json")
                                            and "/" not in candidate):
                                        force_answer = candidate
                                        break
                                if force_answer != "Unable to complete task":
                                    break
                            except Exception:
                                pass
                print(f"{CLI_YELLOW}Force answer: '{force_answer}'{CLI_CLR}")
                try:
                    vm.answer(AnswerRequest(answer=force_answer, refs=list(auto_refs)))
                except Exception:
                    pass
                break

            log.append({"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)})
            log.append({"role": "user", "content": escalation_msg})
            continue

        # --- Loop detection ---
        h = _action_hash(job.action)
        last_hashes.append(h)
        if len(last_hashes) > 5:
            last_hashes.pop(0)

        if len(last_hashes) >= 3 and len(set(last_hashes[-3:])) == 1:
            if len(last_hashes) >= 5 and len(set(last_hashes[-5:])) == 1:
                print(f"{CLI_RED}Loop detected (5x same action), force finishing{CLI_CLR}")
                try:
                    vm.answer(AnswerRequest(
                        answer="Agent failed: stuck in loop",
                        refs=[],
                    ))
                except Exception:
                    pass
                break
            else:
                print(f"{CLI_YELLOW}WARNING: Same action repeated 3 times{CLI_CLR}")
                log.append({"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)})
                log.append({"role": "user", "content": "WARNING: You are repeating the same action. Try a different approach or finish the task."})
                continue

        # --- Add assistant message to log ---
        if len(job.think) > 400:
            job = job.model_copy(update={"think": job.think[:400] + "…"})
        log.append({"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)})

        # --- Pre-write validation ---
        if isinstance(job.action, Modify) and job.action.action == "write":
            # Auto-strip leading slash from write path
            if job.action.path.startswith("/"):
                _f45_old = job.action.path
                job.action.path = job.action.path.lstrip("/")
                log[-1] = {"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)}
                print(f"{CLI_YELLOW}[FIX-45] stripped leading slash: '{_f45_old}' → '{job.action.path}'{CLI_CLR}")

            # Block ALL writes when no write-task directories were found in pre-phase
            if not has_write_task_dirs and not confirmed_writes:
                _w41_msg = (
                    f"BLOCKED: Writing files is NOT allowed for this task. "
                    f"This task requires only a factual answer — no file creation. "
                    f"Read the instruction file (already loaded) and call finish IMMEDIATELY with the keyword it specifies. "
                    f"Do NOT write any files."
                )
                print(f"{CLI_YELLOW}[FIX-41] write blocked — no write-task dirs found (factual task){CLI_CLR}")
                log.append({"role": "user", "content": _w41_msg})
                continue

            # Block writes to pre-existing vault files
            _w39_path = job.action.path.lstrip("/")
            _w39_in_cache = (
                _w39_path in all_file_contents
                or ("/" + _w39_path) in all_file_contents
            )
            if _w39_in_cache and _w39_path not in confirmed_writes:
                _w39_nums = re.findall(r'\d+', Path(_w39_path).name)
                if _w39_nums:
                    _w39_next = max(int(x) for x in _w39_nums if int(x) < 1900) + 1
                    _w39_hint = f"Create a NEW file with the next ID (e.g. ID {_w39_next})."
                else:
                    _w39_hint = "Do NOT modify vault files — create a NEW file for this task."
                _w39_msg = (
                    f"ERROR: '{job.action.path}' is a pre-existing vault file — do NOT overwrite it. "
                    f"{_w39_hint} "
                    f"Existing vault file contents must not be changed by this task."
                )
                print(f"{CLI_YELLOW}[FIX-39] BLOCKED overwrite of existing vault file: '{_w39_path}'{CLI_CLR}")
                log.append({"role": "user", "content": _w39_msg})
                continue

            # Block second write to a different path (tasks create exactly ONE file)
            _f44_new_path = job.action.path.lstrip("/")
            _f44_confirmed_paths = {p for p in confirmed_writes.keys() if not p.endswith(":content")}
            if _f44_confirmed_paths and _f44_new_path not in _f44_confirmed_paths:
                _f44_first = next(iter(_f44_confirmed_paths))
                _f44_new_ext = Path(_f44_new_path).suffix.lower()
                _f44_first_ext = Path(_f44_first).suffix.lower()
                _f44_same_dir = str(Path(_f44_new_path).parent) == str(Path(_f44_first).parent)
                _f44_garbage_first = (_f44_first_ext != _f44_new_ext and _f44_same_dir)
                if not _f44_garbage_first:
                    _f44_msg = (
                        f"BLOCKED: '{_f44_new_path}' cannot be written — '{_f44_first}' was already "
                        f"successfully created. This task requires only ONE new file. "
                        f"Call finish IMMEDIATELY with refs to all files you read."
                    )
                    print(f"{CLI_YELLOW}[FIX-44] second-write blocked (already wrote '{_f44_first}'){CLI_CLR}")
                    log.append({"role": "user", "content": _f44_msg})
                    continue
                else:
                    print(f"{CLI_YELLOW}[FIX-44] allowing second write (first '{_f44_first}' was garbage){CLI_CLR}")

            # Prevent duplicate writes
            write_path = job.action.path.lstrip("/")
            if write_path in confirmed_writes:
                dup_msg = (
                    f"ERROR: '{write_path}' was ALREADY successfully written at step {confirmed_writes[write_path]}. "
                    f"Do NOT write to this path again. Call finish immediately with all refs."
                )
                print(f"{CLI_YELLOW}[FIX-9] blocked duplicate write to '{write_path}'{CLI_CLR}")
                log.append({"role": "user", "content": dup_msg})
                continue

            # Unescape literal \\n → real newlines
            if '\\n' in job.action.content and '\n' not in job.action.content:
                job.action.content = job.action.content.replace('\\n', '\n')
                print(f"{CLI_YELLOW}[FIX-20] unescaped \\\\n in write content{CLI_CLR}")
                log[-1] = {"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)}

            # Block markdown content in plain-text files
            _f36_has_markdown = (
                '**' in job.action.content
                or '### ' in job.action.content
                or bool(re.search(r'^# ', job.action.content, re.MULTILINE))
            )
            if not job.action.path.endswith('.json') and _f36_has_markdown:
                _f36_dir = str(Path(job.action.path).parent)
                _f36_templates = [(k, v) for k, v in all_file_contents.items()
                                   if str(Path(k).parent) == _f36_dir
                                   and '**' not in v and '### ' not in v
                                   and not re.search(r'^# ', v, re.MULTILINE)]
                if _f36_templates:
                    _f36_sample_path, _f36_sample_content = _f36_templates[0]
                    _f36_err = (
                        f"ERROR: content for '{job.action.path}' uses markdown formatting "
                        f"(# headings, **bold**, or ### headers) "
                        f"but existing files in '{_f36_dir}/' use PLAIN TEXT (no markdown at all). "
                        f"COPY the EXACT format from '{_f36_sample_path}' below — no # signs, no **, no ###:\n"
                        f"{repr(_f36_sample_content[:400])}\n"
                        f"Replace the example values with the correct ones for this task and retry."
                    )
                    print(f"{CLI_YELLOW}[FIX-36] markdown-in-plaintext blocked for {job.action.path}{CLI_CLR}")
                    log.append({"role": "user", "content": _f36_err})
                    continue

            # Sanitize JSON content for .json files
            if job.action.path.endswith('.json'):
                _j31_content = job.action.content
                try:
                    json.loads(_j31_content)
                except json.JSONDecodeError:
                    _j31_fixed = re.sub(r'^\\+([{\[])', r'\1', _j31_content)
                    _j31_fixed = _j31_fixed.replace('\\"', '"')
                    _j31_end = max(_j31_fixed.rfind('}'), _j31_fixed.rfind(']'))
                    if _j31_end > 0:
                        _j31_fixed = _j31_fixed[:_j31_end + 1]
                    try:
                        json.loads(_j31_fixed)
                        job.action.content = _j31_fixed
                        print(f"{CLI_YELLOW}[FIX-31] JSON content sanitized for {job.action.path}{CLI_CLR}")
                        log[-1] = {"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)}
                    except json.JSONDecodeError:
                        _j31_err = (
                            f"ERROR: content for '{job.action.path}' is not valid JSON. "
                            f"Write ONLY a raw JSON object starting with {{. "
                            f"No backslash prefix, no escaped braces. Example from existing file."
                        )
                        print(f"{CLI_YELLOW}[FIX-31] invalid JSON — blocking write{CLI_CLR}")
                        log.append({"role": "user", "content": _j31_err})
                        continue

            warning = _validate_write(vm, job.action, auto_refs, all_preloaded=all_reads_ever)
            if warning:
                _f34_redirected = False
                if "looks like it belongs in" in warning:
                    _f34_m = re.search(r"Use path '([^']+)' instead", warning)
                    if _f34_m:
                        _f34_correct = _f34_m.group(1)
                        _f34_content_ok = True
                        if job.action.path.endswith('.json'):
                            try:
                                json.loads(job.action.content)
                            except json.JSONDecodeError:
                                _f34_content_ok = False
                        if _f34_content_ok:
                            _old_path = job.action.path
                            job.action.path = _f34_correct
                            log[-1] = {"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)}
                            print(f"{CLI_GREEN}[FIX-34] Cross-dir auto-redirect: '{_old_path}' → '{_f34_correct}'{CLI_CLR}")
                            _f34_redirected = True
                if not _f34_redirected:
                    print(f"{CLI_YELLOW}{warning}{CLI_CLR}")
                    log.append({"role": "user", "content": warning})
                    continue

        # --- Auto-merge refs and clean answer for Finish action ---
        if isinstance(job.action, Finish):
            answer = job.action.answer.strip()

            # Strip [TASK-DONE] prefix
            if answer.startswith("[TASK-DONE]"):
                rest = answer[len("[TASK-DONE]"):].strip()
                if rest:
                    print(f"{CLI_YELLOW}Answer trimmed ([TASK-DONE] prefix removed){CLI_CLR}")
                    answer = rest

            # Strip everything after "}}"
            if "}}" in answer:
                before_braces = answer.split("}}")[0].strip()
                if before_braces and len(before_braces) < 60:
                    print(f"{CLI_YELLOW}Answer trimmed (}} artifact): '{answer[:60]}' → '{before_braces}'{CLI_CLR}")
                    answer = before_braces

            # Extract quoted keyword at end of verbose sentence
            m_quoted = re.search(r'"([A-Z][A-Z0-9\-]{0,29})"\s*\.?\s*$', answer)
            if m_quoted:
                extracted = m_quoted.group(1)
                print(f"{CLI_YELLOW}Answer extracted (quoted keyword): '{answer[:60]}' → '{extracted}'{CLI_CLR}")
                answer = extracted
            elif len(answer) > 2 and answer[0] in ('"', "'") and answer[-1] == answer[0]:
                unquoted = answer[1:-1].strip()
                if unquoted:
                    print(f"{CLI_YELLOW}Answer trimmed (quotes): '{answer}' → '{unquoted}'{CLI_CLR}")
                    answer = unquoted

            # Strip after newlines
            if "\n" in answer:
                first_line = answer.split("\n")[0].strip()
                if first_line:
                    print(f"{CLI_YELLOW}Answer trimmed (newline): '{answer[:60]}' → '{first_line}'{CLI_CLR}")
                    answer = first_line

            # Strip trailing explanation
            if ". " in answer:
                first_sentence = answer.split(". ")[0].strip()
                if first_sentence and len(first_sentence) < 30:
                    print(f"{CLI_YELLOW}Answer trimmed (sentence): '{answer[:60]}' → '{first_sentence}'{CLI_CLR}")
                    answer = first_sentence
            if " - " in answer:
                before_dash = answer.split(" - ")[0].strip()
                if before_dash and len(before_dash) < 30 and before_dash != answer:
                    print(f"{CLI_YELLOW}Answer trimmed (dash): '{answer[:60]}' → '{before_dash}'{CLI_CLR}")
                    answer = before_dash
            if ": " in answer:
                before_colon = answer.split(": ")[0].strip()
                after_colon = answer.split(": ", 1)[1].strip()
                if (before_colon and len(before_colon) < 30 and before_colon != answer
                        and "/" not in after_colon):
                    print(f"{CLI_YELLOW}Answer trimmed (colon): '{answer[:60]}' → '{before_colon}'{CLI_CLR}")
                    answer = before_colon
            if ", " in answer:
                before_comma = answer.split(", ")[0].strip()
                if before_comma and len(before_comma) < 30 and before_comma != answer:
                    print(f"{CLI_YELLOW}Answer trimmed (comma): '{answer[:60]}' → '{before_comma}'{CLI_CLR}")
                    answer = before_comma
            if answer.endswith(".") and len(answer) > 1:
                answer = answer[:-1]
            if answer.endswith(",") and len(answer) > 1:
                answer = answer[:-1]

            # FIX-56: In redirect case, auto-correct answer to redirect keyword
            if (instruction_file_redirect_target and not confirmed_writes):
                _f56_redir_txt = all_file_contents.get(instruction_file_redirect_target, "")
                _f56_kw_m = re.search(
                    r"(?:respond|answer|reply)\s+with\s+['\"]([A-Za-z0-9][A-Za-z0-9 \-_]{0,30})['\"]",
                    _f56_redir_txt, re.IGNORECASE
                )
                if _f56_kw_m:
                    _f56_kw = _f56_kw_m.group(1)
                    if answer != _f56_kw:
                        print(f"{CLI_YELLOW}[FIX-56] redirect: correcting '{answer[:30]}' → '{_f56_kw}'{CLI_CLR}")
                        answer = _f56_kw

            # FIX-32: Extract keyword from think field for verbose answers
            if len(answer) > 40 and "/" not in answer:
                _f32_m = re.search(
                    r"(?:respond|answer|reply)\s+with\s+(?:exactly\s+)?['\"]([A-Za-z0-9\-_]{2,25})['\"]",
                    job.think, re.IGNORECASE
                )
                if _f32_m:
                    _f32_kw = _f32_m.group(1)
                    print(f"{CLI_YELLOW}[FIX-32] verbose answer → extracted keyword from think: '{_f32_kw}'{CLI_CLR}")
                    answer = _f32_kw

            job.action.answer = answer

            # Merge auto-tracked refs with model-provided refs
            model_refs = set(job.action.refs)
            merged_refs = list(model_refs | auto_refs)
            merged_refs = [_clean_ref(r) for r in merged_refs]
            merged_refs = [r for r in merged_refs if r is not None]

            # FIX-8/FIX-58: Force refs to redirect target when redirect mode
            if instruction_file_redirect_target:
                merged_refs = [instruction_file_redirect_target]
                print(f"{CLI_YELLOW}[FIX-8] refs filtered to redirect target: {merged_refs}{CLI_CLR}")

            job.action.refs = merged_refs
            log[-1] = {"role": "assistant", "content": job.model_dump_json(exclude_defaults=True)}

            # FIX-18: Block premature finish claiming file creation when no write has been done
            if not confirmed_writes:
                _ans_has_path = (
                    "/" in answer
                    or bool(re.search(r'\b\w[\w\-]*\.(md|txt|json|csv)\b', answer, re.IGNORECASE))
                )
                _ans_claims_create = bool(re.search(
                    r'\b(creat|added?|wrote|written|new invoice|submitted|filed)\b',
                    answer, re.IGNORECASE
                ))
                if _ans_has_path and _ans_claims_create:
                    _block_msg = (
                        f"ERROR: You claim to have created/written a file ('{answer[:60]}') "
                        f"but no modify.write was called yet. "
                        f"You MUST call modify.write FIRST to actually create the file, then call finish."
                    )
                    print(f"{CLI_YELLOW}BLOCKED: premature finish (no write done){CLI_CLR}")
                    log.append({"role": "user", "content": _block_msg})
                    continue

                # FIX-33b: Block finish with a new file path that was never written
                _ans_ext = Path(answer.replace("\\", "/").strip()).suffix
                _ans_is_new_file = (
                    _ans_has_path and _ans_ext
                    and answer not in all_file_contents
                    and not any(answer in k for k in all_file_contents)
                )
                if _ans_is_new_file:
                    _f33b_hint = (
                        f"ERROR: '{answer}' has not been written yet — no modify.write was called. "
                        f"Call modify.write FIRST to create the file, then call finish."
                    )
                    print(f"{CLI_YELLOW}[FIX-33b] BLOCKED: finish with unwritten path '{answer}'{CLI_CLR}")
                    log.append({"role": "user", "content": _f33b_hint})
                    continue

        # --- Execute action (with pre-phase cache) ---
        txt = ""
        cache_hit = False
        if isinstance(job.action, Inspect) and job.action.action == "read":
            req_path = job.action.path.lstrip("/")
            cached = all_file_contents.get(req_path) or all_file_contents.get("/" + req_path)
            if cached:
                all_reads_ever.add(req_path)
                mapped = {"path": req_path, "content": cached}
                txt = _truncate(json.dumps(mapped, indent=2))
                cache_hit = True
                print(f"{CLI_GREEN}CACHE HIT{CLI_CLR}: {req_path}")
                # FIX-23: When model re-reads instruction file from cache, inject finish hint
                _instr_upper = instruction_file_name.upper() if instruction_file_name else ""
                if (req_path.upper() == _instr_upper and instr_len > 50
                        and not confirmed_writes):
                    txt += (
                        f"\n\nYou have re-read {instruction_file_name}. Its instructions define the required response. "
                        f"Call finish IMMEDIATELY with the required keyword from {instruction_file_name} "
                        f"and refs=['{instruction_file_name}']. "
                        f"Do NOT navigate or read any more files."
                    )
                    print(f"{CLI_GREEN}[FIX-23] finish hint appended to instruction file cache hit{CLI_CLR}")

        if not cache_hit:
            try:
                result = dispatch(vm, job.action)
                mapped = MessageToDict(result)
                txt = _truncate(json.dumps(mapped, indent=2))
                print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt[:500]}{'...' if len(txt) > 500 else ''}")
                # Track live reads for cross-dir validation
                if isinstance(job.action, Inspect) and job.action.action == "read" and not txt.startswith("error"):
                    try:
                        _live_path = json.loads(txt).get("path", "")
                        if _live_path:
                            all_reads_ever.add(_live_path)
                    except Exception:
                        pass
            except ConnectError as e:
                txt = f"error: {e.message}"
                print(f"{CLI_RED}ERR {e.code}: {e.message}{CLI_CLR}")
            except Exception as e:
                txt = f"error: {e}"
                print(f"{CLI_RED}ERR: {e}{CLI_CLR}")

        # --- FIX-38/FIX-50: Inject JSON template after schema validation error ---
        if (isinstance(job.action, Modify)
                and job.action.action == "write"
                and job.action.path.endswith(".json")
                and txt.startswith("error")
                and ("validation" in txt.lower() or "schema" in txt.lower() or "invalid" in txt.lower())):
            _f50_corrected = False
            _f50_content = job.action.content
            _f50_task_lower = task_text.lower()
            _f50_target_prio = None
            if any(kw in _f50_task_lower for kw in ("high prio", "high priority", "urgent", "asap", "high-prio")):
                _f50_target_prio = "pr-high"
            elif any(kw in _f50_task_lower for kw in ("low prio", "low priority", "low-prio")):
                _f50_target_prio = "pr-low"
            _f50_bad_prios = ['"pr-hi"', '"pr-medium"', '"high"', '"low"', '"medium"', '"pr-med-high"', '"pr-high-med"']
            _f50_has_bad_prio = any(bp in _f50_content for bp in _f50_bad_prios)
            if _f50_has_bad_prio and _f50_target_prio:
                _f50_new_content = _f50_content
                for bp in _f50_bad_prios:
                    _f50_new_content = _f50_new_content.replace(bp, f'"{_f50_target_prio}"')
                try:
                    json.loads(_f50_new_content)
                    print(f"{CLI_GREEN}[FIX-50] auto-correcting priority → '{_f50_target_prio}', retrying write{CLI_CLR}")
                    vm.write(WriteRequest(path=job.action.path, content=_f50_new_content))
                    wpath50 = job.action.path.lstrip("/")
                    confirmed_writes[wpath50] = i + 1
                    log.append({"role": "user", "content": (
                        f"[TASK-DONE] '{job.action.path}' has been written successfully (priority corrected to '{_f50_target_prio}'). "
                        f"The task is now COMPLETE. "
                        f"Call finish IMMEDIATELY with refs to ALL files you read."
                    )})
                    _f50_corrected = True
                except Exception as _f50_e:
                    print(f"{CLI_YELLOW}[FIX-50] retry failed: {_f50_e}{CLI_CLR}")
            if not _f50_corrected:
                _f38_dir = str(Path(job.action.path).parent)
                _f38_templates = [
                    (k, v) for k, v in all_file_contents.items()
                    if (str(Path(k).parent) == _f38_dir
                        and k.endswith(".json")
                        and v.strip().startswith("{"))
                ]
                if _f38_templates:
                    _f38_path, _f38_content = _f38_templates[0]
                    try:
                        _f38_parsed = json.loads(_f38_content)
                        _f38_keys = list(_f38_parsed.keys())
                    except Exception:
                        _f38_keys = []
                    _f38_msg = (
                        f"SCHEMA ERROR: your JSON for '{job.action.path}' was rejected. "
                        f"You MUST use the EXACT same JSON structure as existing files in '{_f38_dir}/'. "
                        f"Required fields (from '{_f38_path}'): {_f38_keys}. "
                        f"COPY this exact format, replacing only the values:\n"
                        f"{_f38_content[:600]}\n"
                        f"Keep the SAME path '{job.action.path}', same field names, same structure. "
                        f"Do NOT change the filename. Do NOT add or remove fields. "
                        f"NOTE: Priority values are 'pr-high' (high prio) or 'pr-low' (low prio)."
                    )
                    print(f"{CLI_YELLOW}[FIX-38] schema error — injecting template from {_f38_path}{CLI_CLR}")
                    log.append({"role": "user", "content": _f38_msg})
            continue

        # --- Post-modify auto-finish hint + confirmed write tracking ---
        if isinstance(job.action, Modify) and not txt.startswith("error"):
            op = "deleted" if job.action.action == "delete" else "written"
            if job.action.action == "write":
                wpath = job.action.path.lstrip("/")
                confirmed_writes[wpath] = i + 1
            log.append({"role": "user", "content": (
                f"[TASK-DONE] '{job.action.path}' has been {op} successfully. "
                f"The task is now COMPLETE. "
                f"Call finish IMMEDIATELY with refs to ALL files you read "
                f"(policy files, skill files, source files, etc.). "
                f"Do NOT navigate, list, or read anything else."
            )})

        # --- Track read files for auto-refs ---
        if isinstance(job.action, Inspect) and job.action.action == "read":
            if not txt.startswith("error"):
                try:
                    read_parsed = json.loads(txt)
                    read_path = read_parsed.get("path", "")
                    if read_path:
                        file_stem = Path(read_path).stem.lower()
                        file_name = Path(read_path).name.lower()
                        is_policy_file = any(kw in file_name for kw in POLICY_KEYWORDS)
                        if file_stem in task_lower or file_name in task_lower or is_policy_file:
                            auto_refs.add(read_path)
                            print(f"{CLI_GREEN}[auto-ref] tracked: {read_path}{CLI_CLR}")
                except Exception:
                    pass

        # --- Check if finished ---
        if isinstance(job.action, Finish):
            print(f"\n{CLI_GREEN}Agent {job.action.code}{CLI_CLR}")
            print(f"{CLI_BLUE}ANSWER: {job.action.answer}{CLI_CLR}")
            if job.action.refs:
                for ref in job.action.refs:
                    print(f"  - {CLI_BLUE}{ref}{CLI_CLR}")
            break

        # --- Hints for empty list/search results ---
        if isinstance(job.action, Navigate) and job.action.action == "list":
            mapped_check = json.loads(txt) if not txt.startswith("error") else {}
            if not mapped_check.get("files"):
                txt += "\nNOTE: Empty result. Try 'tree' on this path or list subdirectories."
        elif isinstance(job.action, Inspect) and job.action.action == "search":
            mapped_check = json.loads(txt) if not txt.startswith("error") else {}
            if not mapped_check.get("results") and not mapped_check.get("files"):
                txt += "\nNOTE: No search results. Try: (a) broader pattern, (b) different directory, (c) list instead of search."
        elif isinstance(job.action, Navigate) and job.action.action == "tree":
            nav_path = job.action.path.lstrip("/")
            if "." in Path(nav_path).name and txt.startswith("error"):
                txt += (
                    f"\nNOTE: '{nav_path}' does not exist yet — it has not been created. "
                    f"STOP verifying. CREATE it now using modify.write, then call finish immediately."
                )

        # --- Add tool result to log ---
        log.append({"role": "user", "content": f"Tool result:\n{txt}"})

    else:
        print(f"{CLI_RED}Max steps ({max_steps}) reached, force finishing{CLI_CLR}")
        try:
            vm.answer(AnswerRequest(
                answer="Agent failed: max steps reached",
                refs=[],
            ))
        except Exception:
            pass
