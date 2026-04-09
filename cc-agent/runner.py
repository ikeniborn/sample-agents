"""
CC Agent runner — executes pac1 benchmark tasks via Claude Code CLI.

Workflow per task:
  1. start_playground / start_trial → harness_url + instruction
  2. write temp MCP config pointing to mcp_pcm.py with HARNESS_URL set
  3. run: iclaude --print --mcp-config <cfg> -p "<instruction>"
  4. end_trial → score

Usage:
    python runner.py [task_id ...]

Env vars (from pac1-py/.env or shell):
    BITGN_HOST       default: https://api.bitgn.com
    BENCH_ID         default: bitgn/pac1-dev
    TASK_TIMEOUT_S   default: 300
    PARALLEL_TASKS   default: 1
    BITGN_API_KEY    set to enable run mode (vs playground mode)
    BITGN_RUN_NAME   run label shown on the leaderboard
"""

import json
import os
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

# Load pac1-py/.env and .secrets into os.environ (real env vars take priority)
_dotenv: dict[str, str] = {}
for _p in (_pac1 / ".env", _pac1 / ".secrets"):
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
from connectrpc.errors import ConnectError

from prompt import SYSTEM_PROMPT

BITGN_URL = os.getenv("BITGN_HOST", "https://api.bitgn.com")
BENCHMARK_ID = os.getenv("BENCH_ID", "bitgn/pac1-dev")
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT_S", "300"))
PARALLEL_TASKS = int(os.getenv("PARALLEL_TASKS", "1"))
BITGN_API_KEY = os.getenv("BITGN_API_KEY", "")
BITGN_RUN_NAME = os.getenv("BITGN_RUN_NAME", "")

_MCP_SERVER = Path(__file__).parent / "mcp_pcm.py"
_PAC1_DIR = Path(__file__).parent.parent / "pac1-py"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"

_LOGS_DIR = Path(__file__).parent / "logs"
_STDOUT_LOCK = threading.Lock()


def _make_run_dir() -> Path:
    """Create and return logs/<timestamp>/ directory for this run."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _LOGS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _open_log(run_dir: Path, name: str):
    """Open a log file inside run_dir."""
    return open(run_dir / f"{name}.log", "w", encoding="utf-8", buffering=1)


import re as _re
_ANSI = _re.compile(r"\x1B\[[0-9;]*[mA-Za-z]")


def _collect_stdout(src) -> list[str]:
    """Drain subprocess stdout to terminal; return clean lines (ANSI stripped)."""
    lines: list[str] = []
    for raw in src:
        with _STDOUT_LOCK:
            sys.stdout.write(raw)
            sys.stdout.flush()
        lines.extend(_ANSI.sub("", raw).splitlines())
    return lines


def _agent_response(lines: list[str]) -> list[str]:
    """Drop the iclaude proxy preamble (everything up to and including 'PII proxy:')."""
    for i, line in enumerate(lines):
        if "PII proxy:" in line:
            # skip blank lines immediately after the preamble
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            return lines[j:]
    return lines  # no preamble found — return as-is


def _build_mcp_config(harness_url: str, trace_file: Path) -> dict:
    """Build MCP server config for this trial."""
    env = {
        "HARNESS_URL": harness_url,
        "PYTHONPATH": str(_PAC1_DIR),
        "MCP_TRACE_FILE": str(trace_file),
    }
    for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        val = os.getenv(key)
        if val:
            env[key] = val

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


def _execute_iclaude(
    client: HarnessServiceClientSync,
    task_id: str,
    trial_id: str,
    harness_url: str,
    instruction: str,
    run_dir: Path,
) -> dict:
    """Run iclaude subprocess for one trial. Returns score dict."""
    with _STDOUT_LOCK:
        print(f"\n{'=' * 30} {task_id} {'=' * 30}")
        print(f"{CLI_BLUE}{instruction}{CLI_CLR}\n{'-' * 80}")

    trace_file = run_dir / f"{task_id}.trace"
    mcp_cfg = _build_mcp_config(harness_url, trace_file)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix=f"mcp_{task_id}_"
    ) as f:
        json.dump(mcp_cfg, f)
        cfg_path = f.name

    start = time.time()
    exit_code = -1
    stdout_lines: list[str] = []
    proc = None
    try:
        proc = subprocess.Popen(
            [
                "iclaude",
                "--no-save",
                "--print",
                "--strict-mcp-config",
                "--mcp-config", cfg_path,
                "--system-prompt", SYSTEM_PROMPT,
                instruction,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_PAC1_DIR)},
        )
        stdout_lines = _collect_stdout(proc.stdout)
        proc.wait(timeout=TASK_TIMEOUT)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        with _STDOUT_LOCK:
            print(f"{CLI_RED}[TIMEOUT] task {task_id}{CLI_CLR}")
        exit_code = -1
    finally:
        Path(cfg_path).unlink(missing_ok=True)

    elapsed = time.time() - start

    # Assemble log after process ends — no concurrent writes
    with _open_log(run_dir, task_id) as log:
        log.write(f"task:        {task_id}\n")
        log.write(f"instruction: {instruction}\n")
        log.write(f"pid:         {proc.pid if proc else '?'}  started: {datetime.fromtimestamp(start).isoformat()}\n\n")

        # Tool trace (written by mcp_pcm.py to separate file)
        if trace_file.exists():
            log.write("── tool trace ───────────────────────────────────────────\n")
            log.write(trace_file.read_text(encoding="utf-8"))
            log.write("─────────────────────────────────────────────────────────\n\n")
            trace_file.unlink()

        # Agent response (proxy preamble filtered out)
        response_lines = _agent_response(stdout_lines)
        if response_lines:
            log.write("── agent response ───────────────────────────────────────\n")
            log.write("\n".join(response_lines) + "\n")
            log.write("─────────────────────────────────────────────────────────\n\n")

        log.write(f"[DONE] exit={exit_code}  elapsed={elapsed:.1f}s\n\n")

        trial_result = client.end_trial(EndTrialRequest(trial_id=trial_id))
        score = trial_result.score
        detail = list(trial_result.score_detail)
        style = CLI_GREEN if score == 1 else CLI_RED
        explain = textwrap.indent("\n".join(detail), "  ")
        with _STDOUT_LOCK:
            print(f"\n{style}[{task_id}] Score: {score:.2f}\n{explain}{CLI_CLR}")
        log.write(f"score: {score:.2f}\n{textwrap.indent(chr(10).join(detail), '  ')}\n")

    return {
        "task_id": task_id,
        "score": score,
        "detail": detail,
        "elapsed": elapsed,
        "exit_code": exit_code,
    }


def run_task(client: HarnessServiceClientSync, task_id: str, run_dir: Path) -> dict:
    """Playground mode: start_playground → _execute_iclaude."""
    trial = client.start_playground(
        StartPlaygroundRequest(benchmark_id=BENCHMARK_ID, task_id=task_id)
    )
    return _execute_iclaude(client, task_id, trial.trial_id, trial.harness_url, trial.instruction, run_dir)


def run_trial(
    client: HarnessServiceClientSync,
    trial_id: str,
    task_filter: list[str],
    run_dir: Path,
) -> dict | None:
    """Run mode: start_trial → filter → _execute_iclaude. Returns None if filtered out."""
    trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
    if task_filter and trial.task_id not in task_filter:
        return None
    return _execute_iclaude(client, trial.task_id, trial.trial_id, trial.harness_url, trial.instruction, run_dir)


def main() -> None:
    task_filter = sys.argv[1:]

    client = HarnessServiceClientSync(BITGN_URL)
    print("Connecting to BitGN:", client.status(StatusRequest()))

    bench = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
    print(f"Benchmark: {bench.benchmark_id} — {len(bench.tasks)} tasks  (parallel={PARALLEL_TASKS})")
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

        with _open_log(run_dir, "summary") as log:
            run_id = "_".join(task_filter) if task_filter else "all"
            mode = f"run:{BITGN_RUN_NAME}" if BITGN_API_KEY else "playground"
            log.write(f"benchmark: {bench.benchmark_id}  tasks: {run_id}  mode: {mode}  parallel: {PARALLEL_TASKS}\n\n")
            print("\n" + "=" * 60)
            for r in scores:
                style = CLI_GREEN if r["score"] == 1 else CLI_RED
                print(f"{r['task_id']}: {style}{r['score']:.2f}{CLI_CLR}  ({r['elapsed']:.1f}s)")
                log.write(f"{r['task_id']}: {r['score']:.2f}  ({r['elapsed']:.1f}s)\n")
            final = f"\nFINAL: {total:.2f}%  total: {total_elapsed:.1f}s"
            print(final)
            log.write(final + "\n")


if __name__ == "__main__":
    main()
