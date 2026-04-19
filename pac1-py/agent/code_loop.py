"""Code generation loop — replaces the 30-step PCM-tool loop.

Flow per iteration:
  1. LLM → CodeStep (reasoning + code + expected_output)
  2. evaluate_code() — pre-execution review (safety / logic)
  3. execute_code_safe() — run in sandbox with pre-loaded context_vars
  4. Parse ActionPlan JSON from output
  5. evaluate_result() — post-execution result check
  6. Execute writes via PCM + report_completion, or feed error back and retry.
"""
from __future__ import annotations

import os
import re
import time

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import AnswerRequest, DeleteRequest, Outcome, ReadRequest, WriteRequest

from .dispatch import (
    CLI_BLUE, CLI_CLR, CLI_GREEN, CLI_RED, CLI_YELLOW,
    anthropic_client, openrouter_client, ollama_client,
    execute_code_safe,
    get_anthropic_model_id, get_provider,
    TRANSIENT_KWS, _THINK_RE,
    _extract_code_block,
)
from .evaluator import evaluate_code, evaluate_result
from .models import CodeStep, ActionPlan

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_TASK_TIMEOUT_S = int(os.environ.get("TASK_TIMEOUT_S", "180"))
_MAX_ITERATIONS = int(os.environ.get("CODE_LOOP_MAX_ITER", "5"))

_EVALUATOR_ENABLED = os.environ.get("EVALUATOR_ENABLED", "1") == "1"
_EVAL_SKEPTICISM = os.environ.get("EVAL_SKEPTICISM", "mid").lower()
_EVAL_EFFICIENCY = os.environ.get("EVAL_EFFICIENCY", "mid").lower()

_OUTCOME_MAP = {
    "OUTCOME_OK": Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
}

# ---------------------------------------------------------------------------
# Vault file pre-loading
# ---------------------------------------------------------------------------

def _extract_paths_from_tree(tree_text: str) -> list[str]:
    """Parse vault tree text into list of file paths."""
    paths = []
    dir_stack: list[tuple[int, str]] = []  # (depth, dir_name)

    for line in tree_text.splitlines():
        if not line or line.startswith("tree ") or line.strip() == ".":
            continue
        m = re.search(r"[├└]── (.+)$", line)
        if not m:
            continue
        name = m.group(1)
        arrow_pos = line.index(m.group(0))
        depth = arrow_pos // 4

        # Trim stack to ancestors only
        dir_stack = [(d, n) for d, n in dir_stack if d < depth]

        if name.endswith("/"):
            dir_stack.append((depth, name.rstrip("/")))
        else:
            parent = "/" + "/".join(n for _, n in dir_stack) if dir_stack else ""
            full_path = parent + "/" + name if parent else "/" + name
            paths.append(full_path)

    return paths


def _path_to_var(path: str) -> str:
    """Convert vault path to sandbox variable name."""
    return path.lstrip("/").replace("/", "__").replace(".", "_")


def _score_path(path: str, task_text: str) -> float:
    """Relevance score: how likely this file is useful for the task."""
    task_lower = task_text.lower()
    path_lower = path.lower()
    score = 0.0

    # Skip system/template files
    filename = path.rsplit("/", 1)[-1]
    if filename.startswith("_") or filename.upper() in ("README.MD", "AGENTS.MD"):
        return -1.0

    # Directory-based relevance
    _dir_keywords: list[tuple[str, list[str]]] = [
        ("contacts",     ["contact", "person", "who", "email", "phone", "name", "manager"]),
        ("accounts",     ["account", "company", "client", "industry", "region", "legal"]),
        ("reminders",    ["remind", "follow", "reschedule", "reconnect", "due", "next"]),
        ("my-invoices",  ["invoice", "bill", "payment", "revenue", "spend", "overdue", "amount"]),
        ("inbox",        ["inbox", "inbound", "message", "process", "check", "handle"]),
        ("outbox",        ["outbox", "email", "send", "compose", "seq"]),
        ("opportunities", ["opportunity", "deal", "pipeline", "lead"]),
        ("docs/channels", ["blacklist", "channel", "telegram", "otp", "block", "trust", "inbox"]),
    ]
    for dir_name, keywords in _dir_keywords:
        if f"/{dir_name}/" in path_lower or path_lower.startswith(f"/{dir_name}"):
            if any(k in task_lower for k in keywords):
                score += 2.0
            else:
                score += 0.5  # always useful to have

    # Explicit path mention in task
    stem = filename.rsplit(".", 1)[0].lower()
    tokens = re.split(r"[_\-]", stem)
    for tok in tokens:
        if len(tok) > 2 and tok in task_lower:
            score += 3.0

    return score


def preload_vault_files(
    vm: PcmRuntimeClientSync,
    vault_tree_text: str,
    task_text: str,
    max_files: int = 15,
    max_bytes: int = 60_000,
) -> dict[str, str]:
    """Read relevant vault files and return as {var_name: content} for sandbox."""
    all_paths = _extract_paths_from_tree(vault_tree_text)
    if not all_paths:
        print(f"{CLI_YELLOW}[code_loop] No paths found in tree text{CLI_CLR}")
        return {}

    # Score and sort
    scored = [(p, _score_path(p, task_text)) for p in all_paths]
    scored = [(p, s) for p, s in scored if s >= 0]
    scored.sort(key=lambda x: x[1], reverse=True)

    context_vars: dict[str, str] = {}
    total_bytes = 0

    for path, score in scored:
        if len(context_vars) >= max_files:
            break
        if total_bytes >= max_bytes:
            break
        try:
            r = vm.read(ReadRequest(path=path))
            content = r.content or ""
            if not content:
                continue
            var_name = _path_to_var(path)
            context_vars[var_name] = content
            total_bytes += len(content)
            print(f"{CLI_BLUE}[code_loop] preload {path} → {var_name} ({len(content)} bytes, score={score:.1f}){CLI_CLR}")
        except Exception as e:
            print(f"{CLI_YELLOW}[code_loop] skip {path}: {e}{CLI_CLR}")

    print(f"{CLI_BLUE}[code_loop] preloaded {len(context_vars)} vars, {total_bytes} bytes total{CLI_CLR}")
    return context_vars


# ---------------------------------------------------------------------------
# LLM call that returns CodeStep
# ---------------------------------------------------------------------------

def _to_anthropic_messages(log: list) -> tuple[str, list]:
    """Split log into (system, messages) for Anthropic API. Merges consecutive same-role."""
    system = ""
    messages: list[dict] = []
    for msg in log:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system = content
            continue
        if role not in ("user", "assistant"):
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + content
        else:
            messages.append({"role": role, "content": content})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(start)"})
    return system, messages


def _parse_codestep(raw: str) -> CodeStep | None:
    """Parse raw LLM text into CodeStep. Tries direct JSON, then extraction."""
    raw = raw.strip()
    # Strip markdown fences if present
    fence_m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence_m:
        raw = fence_m.group(1).strip()
    try:
        return CodeStep.model_validate_json(raw)
    except Exception:
        pass
    # Try to find JSON object in text
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return CodeStep.model_validate_json(m.group(0))
        except Exception:
            pass
    return None


def call_llm_codegen(
    log: list,
    model: str,
    cfg: dict,
) -> tuple[CodeStep | None, int, int, int]:
    """Call LLM and parse response as CodeStep. Returns (step, elapsed_ms, in_tok, out_tok)."""
    _provider = get_provider(model, cfg)
    max_tokens = cfg.get("max_completion_tokens", 2000)

    # --- Anthropic SDK ---
    if _provider == "anthropic" and anthropic_client is not None:
        ant_model = get_anthropic_model_id(model)
        for attempt in range(4):
            try:
                started = time.time()
                system, messages = _to_anthropic_messages(log)
                create_kw: dict = dict(
                    model=ant_model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                _ant_temp = cfg.get("temperature")
                if _ant_temp is not None:
                    create_kw["temperature"] = _ant_temp
                resp = anthropic_client.messages.create(**create_kw)
                elapsed_ms = int((time.time() - started) * 1000)
                raw = ""
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        raw = block.text
                        break
                in_tok = getattr(getattr(resp, "usage", None), "input_tokens", 0)
                out_tok = getattr(getattr(resp, "usage", None), "output_tokens", 0)
                print(f"{CLI_YELLOW}[Anthropic/codegen] tokens in={in_tok} out={out_tok}{CLI_CLR}")
                if _LOG_LEVEL == "DEBUG":
                    print(f"{CLI_YELLOW}[Anthropic/codegen] RAW: {raw}{CLI_CLR}")
                step = _parse_codestep(raw)
                if step is not None:
                    return step, elapsed_ms, in_tok, out_tok
                print(f"{CLI_RED}[Anthropic/codegen] JSON parse failed{CLI_CLR}")
                return None, elapsed_ms, in_tok, out_tok
            except Exception as e:
                if any(kw.lower() in str(e).lower() for kw in TRANSIENT_KWS) and attempt < 3:
                    print(f"{CLI_YELLOW}[Anthropic/codegen] Transient (attempt {attempt + 1}): {e} — retrying{CLI_CLR}")
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}[Anthropic/codegen] Error: {e}{CLI_CLR}")
                break

    # --- OpenRouter ---
    if openrouter_client is not None and _provider != "ollama":
        rf = {"type": "json_object"}
        _temp = cfg.get("temperature") or (cfg.get("ollama_options") or {}).get("temperature")
        _seed = (cfg.get("ollama_options") or {}).get("seed")
        for attempt in range(4):
            try:
                started = time.time()
                kw: dict = dict(model=model, messages=log, max_tokens=max_tokens, response_format=rf)
                if _temp is not None:
                    kw["temperature"] = _temp
                if _seed is not None:
                    kw["seed"] = _seed
                resp = openrouter_client.chat.completions.create(**kw)
                elapsed_ms = int((time.time() - started) * 1000)
                _content = resp.choices[0].message.content or ""
                raw = _THINK_RE.sub("", _content).strip()
                in_tok = getattr(getattr(resp, "usage", None), "prompt_tokens", 0)
                out_tok = getattr(getattr(resp, "usage", None), "completion_tokens", 0)
                if not raw:
                    if attempt < 3:
                        continue
                    break
                step = _parse_codestep(raw)
                if step is not None:
                    return step, elapsed_ms, in_tok, out_tok
                print(f"{CLI_RED}[OpenRouter/codegen] JSON parse failed{CLI_CLR}")
                return None, elapsed_ms, in_tok, out_tok
            except Exception as e:
                if any(kw2.lower() in str(e).lower() for kw2 in TRANSIENT_KWS) and attempt < 3:
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}[OpenRouter/codegen] Error: {e}{CLI_CLR}")
                break

    # --- Ollama ---
    ollama_model = cfg.get("ollama_model") or os.environ.get("OLLAMA_MODEL", model)
    extra: dict = {}
    if "ollama_think" in cfg:
        extra["think"] = cfg["ollama_think"]
    _opts = cfg.get("ollama_options")
    if _opts is not None:
        extra["options"] = _opts
    for attempt in range(4):
        try:
            started = time.time()
            kw = dict(model=ollama_model, messages=log, response_format={"type": "json_object"})
            if extra:
                kw["extra_body"] = extra
            resp = ollama_client.chat.completions.create(**kw)
            elapsed_ms = int((time.time() - started) * 1000)
            _content = resp.choices[0].message.content or ""
            raw = _THINK_RE.sub("", _content).strip()
            in_tok = getattr(getattr(resp, "usage", None), "prompt_tokens", 0)
            out_tok = getattr(getattr(resp, "usage", None), "completion_tokens", 0)
            if not raw:
                if attempt < 3:
                    continue
                break
            step = _parse_codestep(raw)
            if step is not None:
                return step, elapsed_ms, in_tok, out_tok
            return None, elapsed_ms, in_tok, out_tok
        except Exception as e:
            if any(kw2.lower() in str(e).lower() for kw2 in TRANSIENT_KWS) and attempt < 3:
                time.sleep(4)
                continue
            print(f"{CLI_RED}[Ollama/codegen] Error: {e}{CLI_CLR}")
            break

    return None, 0, 0, 0


# ---------------------------------------------------------------------------
# Action plan parsing and execution
# ---------------------------------------------------------------------------

def _parse_action_plan(output: str) -> ActionPlan | None:
    """Parse code output string as ActionPlan JSON.

    Code may print debug lines before the final JSON — search from the end.
    """
    output = output.strip()
    # Try full output first (ideal case: code prints only the JSON)
    try:
        return ActionPlan.model_validate_json(output)
    except Exception:
        pass
    # Try from each { position, right-to-left (last JSON wins)
    positions = [m.start() for m in re.finditer(r"\{", output)]
    for pos in reversed(positions):
        try:
            return ActionPlan.model_validate_json(output[pos:])
        except Exception:
            continue
    return None


def _execute_deletes(vm: PcmRuntimeClientSync, paths: list[str]) -> list[str]:
    """Execute delete operations via PCM. Returns list of error messages (empty = all ok)."""
    errors = []
    for path in paths:
        if not path:
            errors.append("delete missing path")
            continue
        try:
            vm.delete(DeleteRequest(path=path))
            print(f"{CLI_GREEN}[code_loop] DELETE {path}{CLI_CLR}")
        except Exception as e:
            err = f"delete {path} failed: {e}"
            print(f"{CLI_RED}[code_loop] {err}{CLI_CLR}")
            errors.append(err)
    return errors


def _execute_writes(vm: PcmRuntimeClientSync, writes: list[dict]) -> list[str]:
    """Execute write operations via PCM. Returns list of error messages (empty = all ok)."""
    errors = []
    for w in writes:
        path = w.get("path", "")
        content = w.get("content", "")
        if not path:
            errors.append("write missing path")
            continue
        try:
            vm.write(WriteRequest(path=path, content=content))
            print(f"{CLI_GREEN}[code_loop] WRITE {path} ({len(content)} bytes){CLI_CLR}")
        except Exception as e:
            err = f"write {path} failed: {e}"
            print(f"{CLI_RED}[code_loop] {err}{CLI_CLR}")
            errors.append(err)
    return errors


def _do_report(vm: PcmRuntimeClientSync, plan: ActionPlan) -> None:
    """Submit task completion to PCM harness."""
    outcome_proto = _OUTCOME_MAP.get(plan.outcome, Outcome.OUTCOME_OK)
    try:
        vm.answer(AnswerRequest(
            message=plan.message,
            outcome=outcome_proto,
            refs=plan.grounding_refs,
        ))
        print(f"{CLI_GREEN}[code_loop] report_completion {plan.outcome}: {plan.message[:80]}{CLI_CLR}")
    except Exception as e:
        print(f"{CLI_RED}[code_loop] report_completion failed: {e}{CLI_CLR}")


# ---------------------------------------------------------------------------
# Few-shot replacement for codegen format
# ---------------------------------------------------------------------------

_CODEGEN_FEW_SHOT_USER = "Example: what is John's email address?"
_CODEGEN_FEW_SHOT_ASSISTANT = (
    '{"reasoning":"iterate contacts vars to find John and return email",'
    '"code":"for var in [contacts__cont_001_json]:\\n  d=json.loads(var)\\n  if \'john\' in d.get(\'name\',\'\').lower():\\n    print(json.dumps({\'outcome\':\'OUTCOME_OK\',\'message\':d[\'email\'],\'writes\':[],\'grounding_refs\':[\'/contacts/cont_001.json\']}))\\n    break",'
    '"expected_output":"{\\"outcome\\":\\"OUTCOME_OK\\",\\"message\\":\\"john@example.com\\",\\"writes\\":[],\\"grounding_refs\\":[\\"...\\"]}"}'
)


def _replace_fewshot(log: list) -> None:
    """Replace prephase few-shot pair (NextStep format) with CodeStep example."""
    if len(log) >= 3 and log[1].get("role") == "user" and log[2].get("role") == "assistant":
        log[1] = {"role": "user", "content": _CODEGEN_FEW_SHOT_USER}
        log[2] = {"role": "assistant", "content": _CODEGEN_FEW_SHOT_ASSISTANT}
    else:
        print(f"{CLI_YELLOW}[code_loop] _replace_fewshot: log has {len(log)} entries, no few-shot pair to replace{CLI_CLR}")


# ---------------------------------------------------------------------------
# Main code loop
# ---------------------------------------------------------------------------

def run_code_loop(
    vm: PcmRuntimeClientSync,
    model: str,
    cfg: dict,
    task_text: str,
    task_type: str,
    context_vars: dict[str, str],
    evaluator_model: str,
    evaluator_cfg: dict,
    log: list,
) -> dict:
    """Execute code generation loop. Returns token usage dict.

    Up to _MAX_ITERATIONS iterations:
      LLM → CodeStep → evaluate_code → execute_code_safe → parse ActionPlan
      → evaluate_result → execute writes → report_completion

    On any failure: add feedback to log and retry.
    After all iterations exhausted: report OUTCOME_NONE_CLARIFICATION.
    """
    _replace_fewshot(log)

    var_names = sorted(context_vars.keys())
    var_list_str = "\n".join(f"  {k}" for k in var_names) if var_names else "  (none)"
    task_msg = f"TASK: {task_text}\n\nAVAILABLE VARS (pre-loaded sandbox variables):\n{var_list_str}"
    log.append({"role": "user", "content": task_msg})

    stats: dict = {
        "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
        "llm_elapsed_ms": 0, "step_count": 0, "llm_call_count": 0,
        "evaluator_calls": 0, "evaluator_rejections": 0, "evaluator_ms": 0,
    }

    task_start = time.time()
    final_plan: ActionPlan | None = None

    for iteration in range(1, _MAX_ITERATIONS + 1):
        # Timeout check
        if time.time() - task_start > _TASK_TIMEOUT_S:
            print(f"{CLI_YELLOW}[code_loop] Timeout after {iteration - 1} iterations{CLI_CLR}")
            break

        print(f"\n{CLI_BLUE}[code_loop] ── ITERATION {iteration}/{_MAX_ITERATIONS} ──{CLI_CLR}")

        # a) LLM → CodeStep
        step, elapsed_ms, in_tok, out_tok = call_llm_codegen(log, model, cfg)
        stats["input_tokens"] += in_tok
        stats["output_tokens"] += out_tok
        stats["llm_elapsed_ms"] += elapsed_ms
        stats["llm_call_count"] += 1
        stats["step_count"] += 1

        if step is None:
            print(f"{CLI_RED}[code_loop] LLM returned no CodeStep — retrying{CLI_CLR}")
            log.append({"role": "user", "content": (
                f"[ITERATION {iteration} ERROR]\n"
                "Response was not valid CodeStep JSON. "
                "Output ONLY: {\"reasoning\":\"...\",\"code\":\"...\",\"expected_output\":\"...\"}"
            )})
            continue

        log.append({"role": "assistant", "content": step.model_dump_json()})
        code = _extract_code_block(step.code)

        # Always log generated code to task log (stdout is tee'd to {task_id}.log)
        print(
            f"{CLI_BLUE}[code_loop] GENERATED CODE (iteration {iteration}):{CLI_CLR}\n"
            f"# reasoning: {step.reasoning}\n"
            "# " + "-" * 60 + "\n"
            + code +
            "\n" + CLI_BLUE + "# " + "-" * 60 + CLI_CLR
        )

        # b) Pre-execution: evaluate_code
        if _EVALUATOR_ENABLED:
            t0 = time.time()
            code_verdict = evaluate_code(
                code=code,
                task_text=task_text,
                task_type=task_type,
                context_keys=var_names,
                model=evaluator_model,
                cfg=evaluator_cfg,
                skepticism="low",
            )
            eval_ms = int((time.time() - t0) * 1000)
            stats["evaluator_calls"] += 1
            stats["evaluator_ms"] += eval_ms

            if not code_verdict.approved:
                stats["evaluator_rejections"] += 1
                issues = ", ".join(code_verdict.issues)
                print(f"{CLI_YELLOW}[code_loop] Code rejected: {issues}{CLI_CLR}")
                log.append({"role": "user", "content": (
                    f"[ITERATION {iteration} FEEDBACK]\n"
                    f"Evaluator rejected your code: {issues}\n"
                    "Fix the code and try again."
                )})
                continue

        # c) Execute code in sandbox
        output = execute_code_safe(code, context_vars)
        print(f"{CLI_BLUE}[code_loop] output: {output[:300]!r}{CLI_CLR}")

        # d) Check for runtime errors
        if output.startswith("[error]"):
            print(f"{CLI_RED}[code_loop] Runtime error: {output}{CLI_CLR}")
            log.append({"role": "user", "content": (
                f"[ITERATION {iteration} FEEDBACK]\n"
                f"Runtime error: {output}\n"
                "Fix the code to eliminate the error."
            )})
            continue

        if not output or output == "(ok, no output)":
            log.append({"role": "user", "content": (
                f"[ITERATION {iteration} FEEDBACK]\n"
                "Code produced no output. Always end with print(json.dumps({...}))."
            )})
            continue

        # e) Parse ActionPlan from output
        plan = _parse_action_plan(output)
        if plan is None:
            log.append({"role": "user", "content": (
                f"[ITERATION {iteration} FEEDBACK]\n"
                f"Output is not valid ActionPlan JSON: {output[:200]}\n"
                'Code must print: {"outcome":"OUTCOME_OK","message":"...","writes":[...],"grounding_refs":[]}'
            )})
            continue

        # f) Post-execution: evaluate_result
        if _EVALUATOR_ENABLED:
            t0 = time.time()
            result_verdict = evaluate_result(
                output=output,
                task_text=task_text,
                task_type=task_type,
                model=evaluator_model,
                cfg=evaluator_cfg,
                skepticism=_EVAL_SKEPTICISM,
                efficiency=_EVAL_EFFICIENCY,
            )
            eval_ms = int((time.time() - t0) * 1000)
            stats["evaluator_calls"] += 1
            stats["evaluator_ms"] += eval_ms

            if not result_verdict.approved:
                stats["evaluator_rejections"] += 1
                issues = ", ".join(result_verdict.issues)
                print(f"{CLI_YELLOW}[code_loop] Result rejected: {issues}{CLI_CLR}")
                log.append({"role": "user", "content": (
                    f"[ITERATION {iteration} FEEDBACK]\n"
                    f"Output: {output[:200]}\n"
                    f"Evaluator: {issues}\n"
                    "Fix the code to produce the correct result."
                )})
                continue

        # g) Execute writes + deletes + report completion
        if plan.writes:
            write_errors = _execute_writes(vm, plan.writes)
            if write_errors:
                log.append({"role": "user", "content": (
                    f"[ITERATION {iteration} FEEDBACK]\n"
                    f"Write errors: {'; '.join(write_errors)}\n"
                    "Fix the file paths or content."
                )})
                continue

        if plan.deletes:
            delete_errors = _execute_deletes(vm, plan.deletes)
            if delete_errors:
                log.append({"role": "user", "content": (
                    f"[ITERATION {iteration} FEEDBACK]\n"
                    f"Delete errors: {'; '.join(delete_errors)}\n"
                    "Fix the file paths in deletes[]."
                )})
                continue

        final_plan = plan
        break

    # Report completion
    if final_plan is None:
        print(f"{CLI_YELLOW}[code_loop] Exhausted iterations — reporting clarification{CLI_CLR}")
        final_plan = ActionPlan(
            outcome="OUTCOME_NONE_CLARIFICATION",
            message="Could not compute a valid result after all iterations.",
            writes=[],
            grounding_refs=[],
        )

    _do_report(vm, final_plan)
    return stats
