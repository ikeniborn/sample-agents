"""
CC Agent runner — executes pac1 benchmark tasks via Claude Code CLI.

Supports two modes:
  - MULTI_AGENT=1 (default): Classifier → Executor → Verifier pipeline
  - MULTI_AGENT=0: Legacy single-agent (one iclaude call per task)

Workflow (multi-agent):
  1. start_playground / start_trial → harness_url + instruction
  2. Classifier (readonly MCP) → reads vault → classification.json
  3. Executor (draft MCP) → performs task → draft.json
  4. Verifier (readonly MCP, different model) → verdict.json
  5. If reject → retry executor with feedback (up to MAX_RETRIES)
  6. Submit final answer → end_trial → score

Env vars (from cc-agent/.env, .secrets, or shell):
    BITGN_HOST              default: https://api.bitgn.com
    BENCH_ID                default: bitgn/pac1-dev
    TASK_TIMEOUT_S          default: 300
    PARALLEL_TASKS          default: 1
    BITGN_API_KEY           set to enable run mode (vs playground mode)
    BITGN_RUN_NAME          run label shown on the leaderboard
    MULTI_AGENT             default: 1 (0 = legacy single-agent)
    MAX_RETRIES             default: 1 (executor retries on verifier reject)
    CLAUDE_MODEL            executor model (default: CLI default)
    CLAUDE_CLASSIFIER_MODEL default: haiku
    CLAUDE_VERIFIER_MODEL   default: auto (picks model different from executor)
    CLAUDE_EFFORT           executor thinking effort (low/medium/high/max, default: empty = CLI default)
    CLAUDE_CLASSIFIER_EFFORT classifier thinking effort (default: empty)
    CLAUDE_VERIFIER_EFFORT  verifier thinking effort (default: empty)
    CLASSIFIER_TIMEOUT_S    default: 120  (hard cap for classifier subprocess)
    VERIFIER_TIMEOUT_S      default: 180  (hard cap for verifier subprocess; overrides dynamic budget cap)
    USE_ROUTER              default: 0   (1/true = pass --router flag to every iclaude call)
"""

import json
import os
import re as _re
import signal
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

_pac1 = Path(__file__).parent.parent / "pac1-py"
if str(_pac1) not in sys.path:
    sys.path.insert(0, str(_pac1))

# Load cc-agent/.env and .secrets into os.environ (real env vars take priority)
_cc_agent = Path(__file__).parent
_dotenv: dict[str, str] = {}
for _p in (_cc_agent / ".env", _cc_agent / ".secrets"):
    if _p.exists():
        for _line in _p.read_text().splitlines():
            _s = _line.strip()
            if _s and not _s.startswith("#") and "=" in _s:
                _k, _, _v = _s.partition("=")
                _dotenv[_k.strip()] = _v.strip()
for _k, _v in _dotenv.items():
    if _k not in os.environ:
        os.environ[_k] = _v

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    StartPlaygroundRequest,
    StartRunRequest,
    StartTrialRequest,
    StatusRequest,
    SubmitRunRequest,
)
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    AnswerRequest,
    DeleteRequest,
    MkDirRequest,
    MoveRequest,
    Outcome,
    WriteRequest,
)
from connectrpc.errors import ConnectError

from agents import (
    CLASSIFIER_PROMPT,
    VERIFIER_PROMPT,
    apply_verdict,
    build_executor_prompt,
    parse_classifier_output,
    parse_verifier_output,
)
from prompt import classify_task, get_prompt

# ── Configuration ────────────────────────────────────────────────────────────

BITGN_URL = os.getenv("BITGN_HOST", "https://api.bitgn.com")
BENCHMARK_ID = os.getenv("BENCH_ID", "bitgn/pac1-dev")
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT_S", "300"))
PARALLEL_TASKS = int(os.getenv("PARALLEL_TASKS", "1"))
BITGN_API_KEY = os.getenv("BITGN_API_KEY", "")
_run_name_base = os.getenv("BITGN_RUN_NAME", "")
BITGN_RUN_NAME = f"{_run_name_base}-{datetime.now().strftime('%Y%m%d-%H%M%S')}" if _run_name_base else ""
ICLAUDE_CMD = os.getenv("ICLAUDE_CMD", "iclaude")
USE_ROUTER = os.getenv("USE_ROUTER", "0") not in ("0", "", "false", "False")

# When router is active, model selection is handled by the router — ignore env vars
CLAUDE_MODEL = "" if USE_ROUTER else os.getenv("CLAUDE_MODEL", "")
CLAUDE_CLASSIFIER_MODEL = "" if USE_ROUTER else os.getenv("CLAUDE_CLASSIFIER_MODEL", "haiku")
CLAUDE_VERIFIER_MODEL = "" if USE_ROUTER else os.getenv("CLAUDE_VERIFIER_MODEL", "")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "")
CLAUDE_CLASSIFIER_EFFORT = os.getenv("CLAUDE_CLASSIFIER_EFFORT", "")
CLAUDE_VERIFIER_EFFORT = os.getenv("CLAUDE_VERIFIER_EFFORT", "")
MULTI_AGENT = os.getenv("MULTI_AGENT", "1") != "0"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "1"))
CLASSIFIER_TIMEOUT = int(os.getenv("CLASSIFIER_TIMEOUT_S", "120"))
VERIFIER_TIMEOUT = int(os.getenv("VERIFIER_TIMEOUT_S", "180"))
FAST_PATH_TYPES = set(
    t.strip() for t in os.getenv("FAST_PATH_TYPES", "lookup,finance").split(",") if t.strip()
)

_MCP_SERVER = Path(__file__).parent / "mcp_pcm.py"
_PAC1_DIR = Path(__file__).parent.parent / "pac1-py"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"

_LOGS_DIR = Path(__file__).parent / "logs"
_STDOUT_LOCK = threading.Lock()

_ANSI = _re.compile(r"\x1B\[[0-9;]*[mA-Za-z]")

_OUTCOME_MAP = {
    "ok": Outcome.OUTCOME_OK,
    "security": Outcome.OUTCOME_DENIED_SECURITY,
    "clarification": Outcome.OUTCOME_NONE_CLARIFICATION,
    "unsupported": Outcome.OUTCOME_NONE_UNSUPPORTED,
}


# ── Utilities ────────────────────────────────────────────────────────────────

def _make_run_dir() -> Path:
    """Create and return logs/<timestamp>/ directory for this run."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _LOGS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _make_task_dir(run_dir: Path, task_id: str) -> Path:
    """Create and return task subdirectory inside run_dir."""
    task_dir = run_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def _open_log(path: Path):
    """Open a log file."""
    return open(path, "w", encoding="utf-8", buffering=1)


def _append_jsonl(path: Path, data: dict) -> None:
    """Append one JSON line to a JSONL file."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, return None on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: dict) -> None:
    """Write a JSON file with indent."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _collect_stdout(src, echo: bool = True) -> list[str]:
    """Drain subprocess stdout; optionally echo to terminal. Return clean lines."""
    lines: list[str] = []
    for raw in src:
        if echo:
            with _STDOUT_LOCK:
                sys.stdout.write(raw)
                sys.stdout.flush()
        lines.extend(_ANSI.sub("", raw).splitlines())
    return lines


def _pick_verifier_model() -> str:
    """Pick a verifier model different from executor."""
    if CLAUDE_MODEL in ("opus", "claude-opus-4-6"):
        return "sonnet"
    return "opus"


_RESOLVED_VERIFIER_MODEL = "" if USE_ROUTER else (CLAUDE_VERIFIER_MODEL or _pick_verifier_model())


def _models_info() -> dict:
    """Return models config dict for logging."""
    if USE_ROUTER:
        return {
            "executor": "router",
            "classifier": "router" if MULTI_AGENT else None,
            "verifier": "router" if MULTI_AGENT else None,
            "effort_executor": CLAUDE_EFFORT or None,
            "effort_classifier": CLAUDE_CLASSIFIER_EFFORT or None,
            "effort_verifier": CLAUDE_VERIFIER_EFFORT or None,
        }
    return {
        "executor": CLAUDE_MODEL or "default",
        "classifier": CLAUDE_CLASSIFIER_MODEL if MULTI_AGENT else None,
        "verifier": _RESOLVED_VERIFIER_MODEL if MULTI_AGENT else None,
        "effort_executor": CLAUDE_EFFORT or None,
        "effort_classifier": CLAUDE_CLASSIFIER_EFFORT or None,
        "effort_verifier": CLAUDE_VERIFIER_EFFORT or None,
    }


def _time_budget(remaining: float, attempt: int, max_attempts: int) -> tuple[int, int]:
    """Return (executor_timeout, verifier_timeout) for this attempt.

    Verifier timeout is computed dynamically after executor finishes via
    _verifier_budget(), so the value returned here is only a planning estimate
    used for logging.  The executor gets the lion's share of the attempt budget.
    """
    remaining_attempts = max_attempts - attempt + 1
    if remaining_attempts <= 0:
        return int(remaining * 0.8), int(remaining * 0.2)
    per_attempt = remaining / remaining_attempts
    # Reserve ~40% for verifier (estimate); actual verifier timeout is dynamic.
    verifier_t = min(VERIFIER_TIMEOUT, per_attempt * 0.4)
    executor_t = per_attempt - verifier_t
    return max(int(executor_t), 10), max(int(verifier_t), 10)


def _verifier_budget(time_remaining: float, elapsed_in_attempt: float,
                     attempt: int, max_attempts: int) -> int:
    """Compute actual verifier timeout from real remaining time after executor.

    Gives the verifier as much time as possible while reserving budget for
    potential retry attempts.
    """
    remaining = time_remaining - elapsed_in_attempt
    future_attempts = max_attempts - attempt  # attempts AFTER this one
    if future_attempts > 0:
        # Reserve budget for future executor+verifier attempts
        reserve = remaining * 0.35
    else:
        # Last attempt — give almost everything to verifier
        reserve = 15  # small buffer for submit overhead
    ver_t = min(VERIFIER_TIMEOUT, remaining - reserve)
    return max(int(ver_t), 10)


# ── MCP config builder ──────────────────────────────────────────────────────

def _build_mcp_config(
    harness_url: str,
    trace_file: Path,
    task_id: str,
    instruction: str,
    mode: str = "full",
    extra_env: dict | None = None,
) -> dict:
    """Build MCP server config for a given mode."""
    env = {
        "HARNESS_URL": harness_url,
        "PYTHONPATH": str(_PAC1_DIR),
        "MCP_TRACE_FILE": str(trace_file),
        "TASK_ID": task_id,
        "TASK_INSTRUCTION": instruction,
        "MCP_MODE": mode,
    }
    if extra_env:
        env.update(extra_env)

    return {
        "mcpServers": {
            "pcm": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(_MCP_SERVER)],
                "env": env,
            }
        }
    }


# ── Spawn iclaude subprocess ────────────────────────────────────────────────

def _spawn_iclaude(
    mcp_cfg: dict,
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: int,
    echo: bool = True,
    bare: bool = False,
    output_format: str = "",
    effort: str = "",
) -> tuple[list[str], int]:
    """Spawn iclaude subprocess. Returns (stdout_lines, exit_code).

    bare=True: runs from /tmp to prevent CLAUDE.md auto-discovery from
    the project directory — Claude walks up from cwd to find CLAUDE.md files.
    Does NOT use --bare CLI flag (that flag disables OAuth/keychain auth).

    output_format: passed as --output-format <value> (e.g. "json").
    With "json" the CLI wraps model output in a JSON envelope; _extract_json
    in agents.py unwraps it transparently.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="mcp_"
    ) as f:
        json.dump(mcp_cfg, f)
        cfg_path = f.name

    cmd = [
        *shlex.split(ICLAUDE_CMD),
        "--no-save",
        "--print",
        "--strict-mcp-config",
        "--mcp-config", cfg_path,
        "--system-prompt", system_prompt,
    ]
    if model:
        cmd.extend(["--model", model])
    if USE_ROUTER:
        cmd.append("--router")
    if effort:
        cmd.extend(["--effort", effort])
    if output_format:
        cmd.extend(["--output-format", output_format])
    cmd.append(user_prompt)

    # bare=True: use neutral cwd to prevent project CLAUDE.md discovery
    cwd = Path("/tmp") if bare else None

    exit_code = -1
    stdout_lines: list[str] = []
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env={**os.environ, "PYTHONPATH": str(_PAC1_DIR)},
            start_new_session=True,  # isolate process group for clean kill
        )
        # Collect stdout in a background thread so the timeout below
        # can kill the process while _collect_stdout is still blocking.
        collected: list[list[str]] = []
        t = threading.Thread(
            target=lambda: collected.append(_collect_stdout(proc.stdout, echo=echo)),
            daemon=True,
        )
        t.start()
        t.join(timeout=timeout)
        if proc.poll() is None:
            # Process still running after timeout — graceful then hard kill.
            # SIGTERM lets iclaude flush output; SIGKILL after grace period
            # ensures the entire process group is reaped.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            # Give processes a few seconds to flush and exit
            t.join(timeout=5)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                t.join(timeout=5)
        else:
            # Process finished normally — wait for output drain
            t.join(timeout=30)
        stdout_lines = collected[0] if collected else []
        exit_code = proc.wait()
    except Exception:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
        exit_code = -1
    finally:
        Path(cfg_path).unlink(missing_ok=True)

    return stdout_lines, exit_code


def _extract_model_from_output(lines: list[str]) -> str:
    """Extract the actual model name from iclaude JSON envelope (modelUsage key)."""
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        usage = obj.get("modelUsage")
        if isinstance(usage, dict) and usage:
            return ", ".join(usage.keys())
    return ""


# ── Submit answer directly ──────────────────────────────────────────────────

def _submit_answer(harness_url: str, answer: dict) -> bool:
    """Submit final answer directly via PcmRuntimeClient (bypassing MCP).

    Returns True on success, False if the harness already has an answer
    (e.g. auto-evaluated by the server before our submission).
    """
    vm = PcmRuntimeClientSync(harness_url)
    outcome = _OUTCOME_MAP.get(answer.get("outcome", "ok"), Outcome.OUTCOME_OK)
    try:
        vm.answer(AnswerRequest(
            message=answer.get("message", ""),
            outcome=outcome,
            refs=answer.get("refs", []),
        ))
        return True
    except ConnectError as exc:
        if "already provided" in str(exc).lower():
            with _STDOUT_LOCK:
                print(f"  {CLI_YELLOW}[submit] WARNING: harness already has an answer — ours was not applied{CLI_CLR}")
            return False
        raise


def _commit_vault_ops(harness_url: str, vault_ops: list[dict]) -> None:
    """Replay staged vault operations to the real vault after verifier approval.

    Called only when final outcome=ok (approve or correct verdict).
    vault_ops come from the executor's draft; verifier never mutates the vault.
    """
    if not vault_ops:
        return
    vm = PcmRuntimeClientSync(harness_url)
    for item in vault_ops:
        op, args = item["op"], item["args"]
        if op == "write":
            vm.write(WriteRequest(
                path=args["path"],
                content=args["content"],
                start_line=args.get("start_line", 0),
                end_line=args.get("end_line", 0),
            ))
        elif op == "delete":
            vm.delete(DeleteRequest(path=args["path"]))
        elif op == "mkdir":
            vm.mk_dir(MkDirRequest(path=args["path"]))
        elif op == "move":
            vm.move(MoveRequest(from_name=args["from_name"], to_name=args["to_name"]))


# ── Multi-agent pipeline ────────────────────────────────────────────────────

def _run_pipeline(
    harness_url: str,
    task_id: str,
    instruction: str,
    task_dir: Path,
) -> None:
    """Run Classifier → Executor → Verifier pipeline."""
    pipeline_start = time.monotonic()

    with _STDOUT_LOCK:
        print(f"  {CLI_YELLOW}[pipeline] classifier → executor → verifier{CLI_CLR}")

    # ── Fast-path: skip classifier for simple task types ──
    if FAST_PATH_TYPES:
        task_type = classify_task(instruction)
        if task_type in FAST_PATH_TYPES:
            with _STDOUT_LOCK:
                print(f"  {CLI_GREEN}[pipeline] fast-path: type={task_type}, skipping classifier{CLI_CLR}")
            _append_jsonl(task_dir / "pipeline.events.jsonl", {
                "type": "classifier_fast_path", "task_type": task_type,
            })
            executor_prompt = get_prompt(instruction)
            time_remaining = TASK_TIMEOUT - (time.monotonic() - pipeline_start)
            return _executor_verify_loop(
                harness_url, task_id, instruction, task_dir,
                executor_prompt, attempt=1, time_remaining=time_remaining,
            )

    # ── Phase 1: Classifier ──
    vault_reads_file = task_dir / "vault_reads.json"
    classifier_trace = task_dir / "classifier.events.jsonl"
    classifier_cfg = _build_mcp_config(
        harness_url, classifier_trace, task_id, instruction, mode="readonly",
        extra_env={"VAULT_READS_FILE": str(vault_reads_file)},
    )

    cls_model_label = "router" if USE_ROUTER else (CLAUDE_CLASSIFIER_MODEL or "default")
    with _STDOUT_LOCK:
        print(f"  {CLI_YELLOW}[classifier] model={cls_model_label}{CLI_CLR}")

    cls_lines, cls_exit = _spawn_iclaude(
        mcp_cfg=classifier_cfg,
        system_prompt=CLASSIFIER_PROMPT,
        user_prompt=instruction,
        model=CLAUDE_CLASSIFIER_MODEL,
        timeout=CLASSIFIER_TIMEOUT,
        echo=False,
        bare=True,
        output_format="json",
        effort=CLAUDE_CLASSIFIER_EFFORT,
    )
    cls_actual = _extract_model_from_output(cls_lines)
    if cls_actual:
        with _STDOUT_LOCK:
            print(f"  {CLI_YELLOW}[classifier] actual_model={cls_actual}{CLI_CLR}")
    classification = parse_classifier_output(cls_lines)

    # Retry classifier once on parse failure (e.g. transient ECONNREFUSED).
    # Brief sleep before retry lets the proxy recover from burst concurrency.
    # A second attempt is cheap relative to executor cost and avoids static-prompt fallback.
    if not classification:
        with _STDOUT_LOCK:
            print(f"  {CLI_YELLOW}[classifier] parse_failed — retrying once{CLI_CLR}")
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "classifier_retry", "reason": "parse_failed", "exit_code": cls_exit,
        })
        time.sleep(5)
        cls_lines, cls_exit = _spawn_iclaude(
            mcp_cfg=classifier_cfg,
            system_prompt=CLASSIFIER_PROMPT,
            user_prompt=instruction,
            model=CLAUDE_CLASSIFIER_MODEL,
            timeout=CLASSIFIER_TIMEOUT,
            echo=False,
            bare=True,
            output_format="json",
            effort=CLAUDE_CLASSIFIER_EFFORT,
        )
        classification = parse_classifier_output(cls_lines)

    if classification:
        _write_json(task_dir / "classification.json", classification)
        executor_prompt = build_executor_prompt(classification)
        with _STDOUT_LOCK:
            print(f"  {CLI_GREEN}[classifier] type={classification.get('task_type', '?')}{CLI_CLR}")
    else:
        executor_prompt = get_prompt(instruction)
        # Save raw output for post-mortem debugging
        raw_out = "\n".join(cls_lines)
        (task_dir / "classifier_raw.txt").write_text(raw_out, encoding="utf-8")
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "classifier_fallback", "reason": "parse_failed", "exit_code": cls_exit,
            "raw_lines": len(cls_lines),
        })
        with _STDOUT_LOCK:
            print(f"  {CLI_RED}[classifier] fallback to static prompt{CLI_CLR}")

    # ── Phase 2+3: Executor → Verifier loop ──
    time_remaining = TASK_TIMEOUT - (time.monotonic() - pipeline_start)
    _executor_verify_loop(
        harness_url, task_id, instruction, task_dir,
        executor_prompt, attempt=1, time_remaining=time_remaining,
    )


def _executor_verify_loop(
    harness_url: str,
    task_id: str,
    instruction: str,
    task_dir: Path,
    executor_prompt: str,
    attempt: int,
    time_remaining: float,
) -> None:
    """Run executor + verifier with retry on reject."""
    exec_t, ver_t = _time_budget(time_remaining, attempt, MAX_RETRIES + 1)
    attempt_start = time.monotonic()

    verifier_model = _RESOLVED_VERIFIER_MODEL

    exec_model_label = "router" if USE_ROUTER else (CLAUDE_MODEL or "default")
    with _STDOUT_LOCK:
        print(f"  {CLI_YELLOW}[executor] attempt={attempt} model={exec_model_label} timeout={exec_t}s{CLI_CLR}")

    # ── Executor ──
    draft_file = task_dir / f"draft_{attempt}.json"
    exec_trace = task_dir / f"executor_{attempt}.events.jsonl"
    exec_extra_env: dict[str, str] = {"DRAFT_FILE": str(draft_file)}
    # Pass classifier's vault reads cache to executor to avoid redundant RPCs
    vault_reads_file = task_dir / "vault_reads.json"
    if vault_reads_file.exists():
        exec_extra_env["VAULT_READS_FILE"] = str(vault_reads_file)
    # Pass vault_today from classifier if available
    vault_today_file = task_dir / "classification.json"
    if vault_today_file.exists():
        try:
            cls_data = json.loads(vault_today_file.read_text(encoding="utf-8"))
            vt = cls_data.get("vault_today", "")
            if vt:
                exec_extra_env["VAULT_TODAY"] = vt
        except (json.JSONDecodeError, OSError):
            pass
    executor_cfg = _build_mcp_config(
        harness_url, exec_trace, task_id, instruction,
        mode="draft",
        extra_env=exec_extra_env,
    )
    exec_lines, _ = _spawn_iclaude(
        mcp_cfg=executor_cfg,
        system_prompt=executor_prompt,
        user_prompt=instruction,
        model=CLAUDE_MODEL,
        timeout=exec_t,
        bare=True,
        output_format="json",
        effort=CLAUDE_EFFORT,
    )
    exec_actual = _extract_model_from_output(exec_lines)
    if exec_actual:
        with _STDOUT_LOCK:
            print(f"  {CLI_YELLOW}[executor] actual_model={exec_actual}{CLI_CLR}")

    draft = _read_json(draft_file)
    exec_elapsed = time.monotonic() - attempt_start
    executor_timed_out = not draft and exec_elapsed >= exec_t * 0.9
    if not draft:
        draft = {"schema_version": 1, "outcome": "clarification", "message": "Executor did not produce a result", "refs": []}
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "executor_timeout" if executor_timed_out else "executor_no_draft",
            "attempt": attempt, "exec_elapsed_s": round(exec_elapsed, 1),
        })

    # ── Time check: skip verifier if budget is too tight ──
    # The harness may auto-submit a default answer on inactivity timeout.
    # If we don't have enough time for a full verifier pass, submit the
    # executor draft directly to avoid losing the answer.
    elapsed = time.monotonic() - attempt_start
    remaining_after_exec = time_remaining - elapsed
    if remaining_after_exec < 45:
        with _STDOUT_LOCK:
            print(f"  {CLI_YELLOW}[pipeline] {remaining_after_exec:.0f}s left — skipping verifier, submitting draft{CLI_CLR}")
        _write_json(task_dir / "final_answer.json", draft)
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "verifier_skipped", "reason": "time_budget",
            "remaining_s": round(remaining_after_exec, 1), "attempt": attempt,
        })
        if draft.get("outcome") == "ok":
            vault_ops = draft.get("vault_ops", [])
            if vault_ops:
                _commit_vault_ops(harness_url, vault_ops)
        _submit_answer(harness_url, draft)
        with _STDOUT_LOCK:
            print(f"  {CLI_GREEN}[submitted] outcome={draft.get('outcome', '?')} (no verifier){CLI_CLR}")
        return

    # Compute actual verifier timeout from real remaining time after executor.
    ver_t = _verifier_budget(time_remaining, time.monotonic() - attempt_start,
                             attempt, MAX_RETRIES + 1)

    ver_model_label = "router" if USE_ROUTER else (verifier_model or "default")
    with _STDOUT_LOCK:
        print(f"  {CLI_YELLOW}[verifier] model={ver_model_label} timeout={ver_t}s{CLI_CLR}")

    # ── Verifier ──
    ver_trace = task_dir / f"verifier_{attempt}.events.jsonl"
    ver_extra_env: dict[str, str] = {}
    if exec_extra_env.get("VAULT_TODAY"):
        ver_extra_env["VAULT_TODAY"] = exec_extra_env["VAULT_TODAY"]
    verifier_cfg = _build_mcp_config(
        harness_url, ver_trace, task_id, instruction, mode="readonly",
        extra_env=ver_extra_env or None,
    )
    verifier_input = json.dumps({
        "instruction": instruction,
        "draft_answer": draft,
    }, ensure_ascii=False)

    ver_lines, ver_exit = _spawn_iclaude(
        mcp_cfg=verifier_cfg,
        system_prompt=VERIFIER_PROMPT,
        user_prompt=verifier_input,
        model=verifier_model,
        timeout=ver_t,
        bare=True,
        echo=False,
        output_format="json",
        effort=CLAUDE_VERIFIER_EFFORT,
    )
    ver_actual = _extract_model_from_output(ver_lines)
    if ver_actual:
        with _STDOUT_LOCK:
            print(f"  {CLI_YELLOW}[verifier] actual_model={ver_actual}{CLI_CLR}")
    verdict = parse_verifier_output(ver_lines)

    # Retry verifier once on parse failure (e.g. transient ECONNREFUSED).
    if not verdict:
        with _STDOUT_LOCK:
            print(f"  {CLI_YELLOW}[verifier] parse_failed — retrying once{CLI_CLR}")
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "verifier_retry", "reason": "parse_failed",
            "exit_code": ver_exit, "attempt": attempt,
        })
        time.sleep(5)
        # Recompute budget for retry with remaining time
        ver_t_retry = _verifier_budget(time_remaining, time.monotonic() - attempt_start,
                                       attempt, MAX_RETRIES + 1)
        ver_lines, ver_exit = _spawn_iclaude(
            mcp_cfg=verifier_cfg,
            system_prompt=VERIFIER_PROMPT,
            user_prompt=verifier_input,
            model=verifier_model,
            timeout=ver_t_retry,
            bare=True,
            echo=False,
            output_format="json",
            effort=CLAUDE_VERIFIER_EFFORT,
        )
        verdict = parse_verifier_output(ver_lines)

    if verdict:
        _write_json(task_dir / f"verdict_{attempt}.json", verdict)
        # Warn if verifier approved without reading any vault files (grounding empty)
        if verdict.get("verdict") == "approve" and not verdict.get("grounding"):
            _append_jsonl(task_dir / "pipeline.events.jsonl", {
                "type": "verifier_no_grounding", "attempt": attempt,
            })
            with _STDOUT_LOCK:
                print(f"  {CLI_YELLOW}[verifier] WARNING: approved with empty grounding{CLI_CLR}")
        with _STDOUT_LOCK:
            print(f"  {CLI_GREEN}[verifier] verdict={verdict.get('verdict', '?')}{CLI_CLR}")
    else:
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "verifier_fallback", "reason": "parse_failed",
            "exit_code": ver_exit, "attempt": attempt,
        })
        with _STDOUT_LOCK:
            print(f"  {CLI_RED}[verifier] fallback — submitting draft as-is{CLI_CLR}")

    # ── Retry on reject ──
    if (verdict and verdict.get("verdict") == "reject"
            and attempt <= MAX_RETRIES):
        elapsed = time.monotonic() - attempt_start
        new_remaining = time_remaining - elapsed
        # Skip futile retry: if executor timed out, retrying with similar or less
        # time budget will produce the same timeout. Require 1.5x the executor
        # budget to give the retry a realistic chance.
        if executor_timed_out and new_remaining < exec_t * 1.5:
            with _STDOUT_LOCK:
                print(f"  {CLI_YELLOW}[retry] skipping — executor timed out and {new_remaining:.0f}s < {exec_t * 1.5:.0f}s needed{CLI_CLR}")
            _append_jsonl(task_dir / "pipeline.events.jsonl", {
                "type": "retry_skipped_timeout", "attempt": attempt,
                "remaining_s": round(new_remaining, 1),
            })
        elif new_remaining > 30:  # enough time for another attempt
            with _STDOUT_LOCK:
                print(f"  {CLI_YELLOW}[retry] attempt {attempt + 1}, reason: {verdict.get('reason', '?')[:80]}{CLI_CLR}")
            feedback_prompt = (
                f"{executor_prompt}\n\n"
                f"## Feedback from verifier (attempt {attempt})\n"
                f"{verdict.get('reason', 'Unknown issue')}\n"
                f"Fix the issues above and try again."
            )
            return _executor_verify_loop(
                harness_url, task_id, instruction, task_dir,
                feedback_prompt, attempt + 1, new_remaining,
            )

    # ── Submit final answer ──
    final = apply_verdict(draft, verdict)
    _write_json(task_dir / "final_answer.json", final)

    # Commit deferred vault ops only when final outcome=ok.
    # vault_ops always originate from the executor draft; verifier never writes to vault.
    # verdict=correct may patch message/refs — vault_ops from executor are still applied.
    if final.get("outcome") == "ok":
        vault_ops = draft.get("vault_ops", [])
        if vault_ops:
            _commit_vault_ops(harness_url, vault_ops)
            with _STDOUT_LOCK:
                print(f"  {CLI_GREEN}[vault] committed {len(vault_ops)} op(s){CLI_CLR}")

    submitted = _submit_answer(harness_url, final)

    if submitted:
        with _STDOUT_LOCK:
            print(f"  {CLI_GREEN}[submitted] outcome={final.get('outcome', '?')}{CLI_CLR}")
    else:
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "submit_already_provided",
            "intended_outcome": final.get("outcome", "?"),
        })


# ── Legacy single-agent execution ───────────────────────────────────────────

def _execute_single(
    harness_url: str,
    task_id: str,
    instruction: str,
    task_dir: Path,
) -> None:
    """Legacy single-agent: one iclaude call with full MCP mode."""
    trace_file = task_dir / "executor.events.jsonl"
    mcp_cfg = _build_mcp_config(harness_url, trace_file, task_id, instruction, mode="full")

    _spawn_iclaude(
        mcp_cfg=mcp_cfg,
        system_prompt=get_prompt(instruction),
        user_prompt=instruction,
        model=CLAUDE_MODEL,
        timeout=TASK_TIMEOUT,
        bare=True,
        effort=CLAUDE_EFFORT,
    )


# ── Task execution (unified entry point) ────────────────────────────────────

def _execute_task(
    client: HarnessServiceClientSync,
    task_id: str,
    trial_id: str,
    harness_url: str,
    instruction: str,
    run_dir: Path,
) -> dict:
    """Execute one task (multi-agent or single). Returns score dict."""
    with _STDOUT_LOCK:
        print(f"\n{'=' * 30} {task_id} {'=' * 30}")
        print(f"{CLI_BLUE}{instruction}{CLI_CLR}")
        print(f"mode={'multi-agent' if MULTI_AGENT else 'single'}  timeout={TASK_TIMEOUT}s")
        print("-" * 70)

    task_dir = _make_task_dir(run_dir, task_id)
    start = time.time()

    try:
        if MULTI_AGENT:
            _run_pipeline(harness_url, task_id, instruction, task_dir)
        else:
            _execute_single(harness_url, task_id, instruction, task_dir)
    except Exception as exc:
        with _STDOUT_LOCK:
            print(f"{CLI_RED}[ERROR] {task_id}: {exc}{CLI_CLR}")
        _append_jsonl(task_dir / "pipeline.events.jsonl", {
            "type": "pipeline_error", "error": str(exc),
        })

    elapsed = time.time() - start

    # ── Score ──
    trial_result = client.end_trial(EndTrialRequest(trial_id=trial_id))
    score = trial_result.score
    detail = list(trial_result.score_detail)

    style = CLI_GREEN if score == 1 else CLI_RED
    explain = textwrap.indent("\n".join(detail), "  ")
    with _STDOUT_LOCK:
        print(f"\n{style}[{task_id}] Score: {score:.2f}\n{explain}{CLI_CLR}")

    # ── Write text log ──
    with _open_log(task_dir / f"{task_id}.log") as log:
        log.write(f"task:        {task_id}\n")
        log.write(f"instruction: {instruction}\n")
        log.write(f"mode:        {'multi-agent' if MULTI_AGENT else 'single'}\n")
        log.write(f"started:     {datetime.fromtimestamp(start).isoformat()}\n\n")

        # Include all JSONL event files
        for events_file in sorted(task_dir.glob("*.events.jsonl")):
            log.write(f"── {events_file.name} ─────────────────────────────────\n")
            log.write(events_file.read_text(encoding="utf-8"))
            log.write("─────────────────────────────────────────────────────────\n\n")

        # Include JSON exchange files
        for json_file in sorted(task_dir.glob("*.json")):
            log.write(f"── {json_file.name} ─────────────────────────────────\n")
            log.write(json_file.read_text(encoding="utf-8"))
            log.write("\n─────────────────────────────────────────────────────────\n\n")

        log.write(f"[DONE] elapsed={elapsed:.1f}s\n")
        log.write(f"score: {score:.2f}\n{textwrap.indent(chr(10).join(detail), '  ')}\n")

    # ── JSONL run event ──
    _append_jsonl(run_dir / "run.jsonl", {
        "type": "task_result",
        "task_id": task_id,
        "score": score,
        "elapsed_s": round(elapsed, 1),
        "outcome": _read_json(task_dir / "final_answer.json") if MULTI_AGENT else None,
        "agent_mode": "multi-agent" if MULTI_AGENT else "single",
        "models": _models_info(),
        "timestamp": datetime.now().isoformat(),
    })

    return {
        "task_id": task_id,
        "score": score,
        "detail": detail,
        "elapsed": elapsed,
    }


# ── Entry points ─────────────────────────────────────────────────────────────

def run_task(client: HarnessServiceClientSync, task_id: str, run_dir: Path) -> dict:
    """Playground mode: start_playground → _execute_task."""
    trial = client.start_playground(
        StartPlaygroundRequest(benchmark_id=BENCHMARK_ID, task_id=task_id)
    )
    return _execute_task(client, task_id, trial.trial_id, trial.harness_url, trial.instruction, run_dir)


def run_trial(
    client: HarnessServiceClientSync,
    trial_id: str,
    task_filter: list[str],
    run_dir: Path,
) -> dict | None:
    """Run mode: start_trial → filter → _execute_task. Returns None if filtered out."""
    trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
    if task_filter and trial.task_id not in task_filter:
        return None
    return _execute_task(client, trial.task_id, trial.trial_id, trial.harness_url, trial.instruction, run_dir)


def main() -> None:
    task_filter = sys.argv[1:]

    client = HarnessServiceClientSync(BITGN_URL)
    print("Connecting to BitGN:", client.status(StatusRequest()))

    bench = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
    mode_label = "multi-agent" if MULTI_AGENT else "single"
    print(f"Benchmark: {bench.benchmark_id} — {len(bench.tasks)} tasks  (parallel={PARALLEL_TASKS}, mode={mode_label})")
    if MULTI_AGENT:
        mi = _models_info()
        print(f"Models: classifier={mi['classifier']}  executor={mi['executor']}  verifier={mi['verifier']}  retries={MAX_RETRIES}")
        print(f"Timeouts: task={TASK_TIMEOUT}s  classifier={CLASSIFIER_TIMEOUT}s  verifier={VERIFIER_TIMEOUT}s")
    print(f"{CLI_GREEN}{bench.description}{CLI_CLR}\n")

    scores: list[dict] = []
    run_start = time.time()
    run_dir = _make_run_dir()
    print(f"Logs: {run_dir}")

    try:
        if BITGN_API_KEY:
            # ── Run mode ──────────────────────────────────────────────────
            run = client.start_run(StartRunRequest(
                name=BITGN_RUN_NAME,
                benchmark_id=BENCHMARK_ID,
                api_key=BITGN_API_KEY,
            ))
            print(f"Run started: {run.run_id}  ({len(run.trial_ids)} trials)")
            try:
                with ThreadPoolExecutor(max_workers=PARALLEL_TASKS) as pool:
                    futures = {
                        pool.submit(run_trial, client, tid, task_filter, run_dir): tid
                        for tid in run.trial_ids
                    }
                    for fut in as_completed(futures):
                        try:
                            result = fut.result()
                            if result is not None:
                                scores.append(result)
                        except Exception as exc:
                            with _STDOUT_LOCK:
                                print(f"{CLI_RED}Error on {futures[fut]}: {exc}{CLI_CLR}")
            finally:
                client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
                print(f"Run submitted: {run.run_id}")
        else:
            # ── Playground mode ───────────────────────────────────────────
            tasks_to_run = [t for t in bench.tasks if not task_filter or t.task_id in task_filter]
            print(f"Playground mode — {len(tasks_to_run)} tasks")
            with ThreadPoolExecutor(max_workers=PARALLEL_TASKS) as pool:
                futures = {
                    pool.submit(run_task, client, t.task_id, run_dir): t.task_id
                    for t in tasks_to_run
                }
                for fut in as_completed(futures):
                    try:
                        scores.append(fut.result())
                    except Exception as exc:
                        with _STDOUT_LOCK:
                            print(f"{CLI_RED}Error on {futures[fut]}: {exc}{CLI_CLR}")

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    if scores:
        scores.sort(key=lambda r: r["task_id"])
        total = sum(r["score"] for r in scores) / len(scores) * 100
        total_elapsed = time.time() - run_start

        # ── Summary ──
        print("\n" + "=" * 60)
        for r in scores:
            style = CLI_GREEN if r["score"] == 1 else CLI_RED
            print(f"{r['task_id']}: {style}{r['score']:.2f}{CLI_CLR}  ({r['elapsed']:.1f}s)")
        final_line = f"\nFINAL: {total:.2f}%  total: {total_elapsed:.1f}s"
        print(final_line)

        # Legacy summary.log
        with _open_log(run_dir / "summary.log") as log:
            run_id = "_".join(task_filter) if task_filter else "all"
            mode = f"run:{BITGN_RUN_NAME}" if BITGN_API_KEY else "playground"
            log.write(f"benchmark: {bench.benchmark_id}  tasks: {run_id}  mode: {mode}  parallel: {PARALLEL_TASKS}\n\n")
            for r in scores:
                log.write(f"{r['task_id']}: {r['score']:.2f}  ({r['elapsed']:.1f}s)\n")
            log.write(f"\nFINAL: {total:.2f}%  total: {total_elapsed:.1f}s\n")

        # Run summary JSONL event
        _append_jsonl(run_dir / "run.jsonl", {
            "type": "run_summary",
            "benchmark": bench.benchmark_id,
            "tasks_total": len(scores),
            "tasks_passed": sum(1 for r in scores if r["score"] == 1.0),
            "score_avg": round(total / 100, 4),
            "elapsed_total_s": round(total_elapsed, 1),
            "agent_mode": "multi-agent" if MULTI_AGENT else "single",
            "models": _models_info(),
            "parallel": PARALLEL_TASKS,
            "max_retries": MAX_RETRIES,
            "timestamp": datetime.now().isoformat(),
        })


if __name__ == "__main__":
    main()
