"""
CC Agent runner — executes pac1 benchmark tasks via Claude Code CLI.

Workflow per task:
  1. start_playground → harness_url + instruction
  2. write temp MCP config pointing to mcp_pcm.py with HARNESS_URL set
  3. run: claude -p "<instruction>" --system-prompt "<system>" --mcp-config <cfg>
  4. end_trial → score

Usage:
    python runner.py [task_id ...]

Env vars (same as pac1-py):
    BENCHMARK_HOST   default: https://api.bitgn.com
    BENCHMARK_ID     default: bitgn/pac1-dev
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

_pac1 = Path(__file__).parent.parent / "pac1-py"
if str(_pac1) not in sys.path:
    sys.path.insert(0, str(_pac1))

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    StartPlaygroundRequest,
    StatusRequest,
)
from connectrpc.errors import ConnectError

from prompt import SYSTEM_PROMPT

BITGN_URL = os.getenv("BENCHMARK_HOST", "https://api.bitgn.com")
BENCHMARK_ID = os.getenv("BENCHMARK_ID", "bitgn/pac1-dev")
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT_S", "300"))

_MCP_SERVER = Path(__file__).parent / "mcp_pcm.py"
_PAC1_DIR = Path(__file__).parent.parent / "pac1-py"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def _build_mcp_config(harness_url: str) -> dict:
    """Build MCP server config for this trial."""
    # Load secrets for API key passthrough
    secrets: dict[str, str] = {}
    secrets_path = _PAC1_DIR / ".secrets"
    if secrets_path.exists():
        for line in secrets_path.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _, v = s.partition("=")
                secrets[k.strip()] = v.strip()

    env = {
        "HARNESS_URL": harness_url,
        "PYTHONPATH": str(_PAC1_DIR),
    }
    # Forward API keys so pac1-py bitgn client can authenticate if needed
    for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        val = os.getenv(key) or secrets.get(key)
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


def run_task(client: HarnessServiceClientSync, task_id: str) -> dict:
    """Run a single task via Claude Code CLI. Returns score info dict."""
    trial = client.start_playground(
        StartPlaygroundRequest(benchmark_id=BENCHMARK_ID, task_id=task_id)
    )
    harness_url = trial.harness_url
    print(f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-' * 80}")

    mcp_cfg = _build_mcp_config(harness_url)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix=f"mcp_{task_id}_"
    ) as f:
        json.dump(mcp_cfg, f)
        cfg_path = f.name

    start = time.time()
    try:
        result = subprocess.run(
            [
                "iclaude",
                "--no-save",
                "--print",
                "--strict-mcp-config",
                "--mcp-config", cfg_path,
                "--system-prompt", SYSTEM_PROMPT,
                trial.instruction,
            ],
            timeout=TASK_TIMEOUT,
            capture_output=False,  # let stdout flow to terminal
            text=True,
            env={**os.environ, "PYTHONPATH": str(_PAC1_DIR)},
        )
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        print(f"{CLI_RED}[TIMEOUT] task {task_id}{CLI_CLR}")
        exit_code = -1
    finally:
        Path(cfg_path).unlink(missing_ok=True)

    elapsed = time.time() - start
    trial_result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))

    score = trial_result.score
    detail = list(trial_result.score_detail)
    style = CLI_GREEN if score == 1 else CLI_RED
    explain = textwrap.indent("\n".join(detail), "  ")
    print(f"\n{style}Score: {score:.2f}\n{explain}\n{CLI_CLR}")

    return {
        "task_id": task_id,
        "score": score,
        "detail": detail,
        "elapsed": elapsed,
        "exit_code": exit_code,
    }


def main() -> None:
    task_filter = sys.argv[1:]

    client = HarnessServiceClientSync(BITGN_URL)
    print("Connecting to BitGN:", client.status(StatusRequest()))

    bench = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
    print(f"Benchmark: {bench.benchmark_id} — {len(bench.tasks)} tasks")
    print(f"{CLI_GREEN}{bench.description}{CLI_CLR}\n")

    scores = []
    run_start = time.time()

    try:
        for task in bench.tasks:
            if task_filter and task.task_id not in task_filter:
                continue
            print(f"{'=' * 30} {task.task_id} {'=' * 30}")
            try:
                info = run_task(client, task.task_id)
                scores.append(info)
            except Exception as exc:
                print(f"{CLI_RED}Error on {task.task_id}: {exc}{CLI_CLR}")

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    if scores:
        total = sum(r["score"] for r in scores) / len(scores) * 100
        total_elapsed = time.time() - run_start
        print("\n" + "=" * 60)
        for r in scores:
            style = CLI_GREEN if r["score"] == 1 else CLI_RED
            print(f"{r['task_id']}: {style}{r['score']:.2f}{CLI_CLR}  ({r['elapsed']:.1f}s)")
        print(f"\nFINAL: {total:.2f}%  total: {total_elapsed:.1f}s")


if __name__ == "__main__":
    main()
