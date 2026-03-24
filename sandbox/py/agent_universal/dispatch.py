import os
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel

from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import (
    AnswerRequest,
    DeleteRequest,
    ListRequest,
    OutlineRequest,
    ReadRequest,
    SearchRequest,
    WriteRequest,
)

from .models import Navigate, Inspect, Modify, Finish


# ---------------------------------------------------------------------------
# Secrets & OpenAI client setup
# ---------------------------------------------------------------------------

def _load_secrets(path: str = ".secrets") -> None:
    secrets_file = Path(path)
    if not secrets_file.exists():
        return
    for line in secrets_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_secrets()

_OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if _OPENROUTER_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=_OPENROUTER_KEY,
        default_headers={
            "HTTP-Referer": "http://localhost",
            "X-Title": "bitgn-agent",
        },
    )
else:
    client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")


# ---------------------------------------------------------------------------
# CLI colors
# ---------------------------------------------------------------------------

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


# ---------------------------------------------------------------------------
# Dispatch: 4 tool types -> 7 VM methods
# ---------------------------------------------------------------------------

def dispatch(vm: MiniRuntimeClientSync, action: BaseModel):
    if isinstance(action, Navigate):
        if action.action == "tree":
            return vm.outline(OutlineRequest(path=action.path))
        return vm.list(ListRequest(path=action.path))

    if isinstance(action, Inspect):
        if action.action == "read":
            return vm.read(ReadRequest(path=action.path))
        return vm.search(SearchRequest(path=action.path, pattern=action.pattern, count=10))

    if isinstance(action, Modify):
        if action.action == "write":
            content = action.content.rstrip()
            return vm.write(WriteRequest(path=action.path, content=content))
        return vm.delete(DeleteRequest(path=action.path))

    if isinstance(action, Finish):
        return vm.answer(AnswerRequest(answer=action.answer, refs=action.refs))

    raise ValueError(f"Unknown action: {action}")
