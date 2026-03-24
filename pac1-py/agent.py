import json
import os
import time
from pathlib import Path
from typing import Annotated, List, Literal, Union

from annotated_types import Ge, Le, MaxLen, MinLen
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    AnswerRequest,
    DeleteRequest,
    FindRequest,
    ListRequest,
    MkDirRequest,
    MoveRequest,
    Outcome,
    ReadRequest,
    SearchRequest,
    TreeRequest,
    WriteRequest,
)
from google.protobuf.json_format import MessageToDict
from openai import OpenAI
from pydantic import BaseModel, Field

from connectrpc.errors import ConnectError


# ---------------------------------------------------------------------------
# Secrets & OpenAI / OpenRouter client setup
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


def _make_client() -> OpenAI:
    if _OPENROUTER_KEY:
        return OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=_OPENROUTER_KEY,
            default_headers={
                "HTTP-Referer": "http://localhost",
                "X-Title": "bitgn-agent",
            },
        )
    return OpenAI()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    completed_steps_laconic: List[str]
    message: str
    grounding_refs: List[str] = Field(default_factory=list)
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    root: str = Field("", description="tree root, empty means repository root")


class Req_Find(BaseModel):
    tool: Literal["find"]
    name: str
    root: str = "/"
    kind: Literal["all", "files", "dirs"] = "all"
    limit: Annotated[int, Ge(1), Le(20)] = 10


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: str
    limit: Annotated[int, Ge(1), Le(20)] = 10
    root: str = "/"


class Req_List(BaseModel):
    tool: Literal["list"]
    path: str = "/"


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: str


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: str
    content: str


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: str


class Req_MkDir(BaseModel):
    tool: Literal["mkdir"]
    path: str


class Req_Move(BaseModel):
    tool: Literal["move"]
    from_name: str
    to_name: str


class NextStep(BaseModel):
    current_state: str
    plan_remaining_steps_brief: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ...,
        description="briefly explain the next useful steps",
    )
    task_completed: bool
    # AICODE-NOTE: Keep this union aligned with the public PCM runtime surface
    # plus the local stop action. PCM currently lacks a public completion RPC, so
    # `report_completion` ends the sample loop locally and `EndTrial` still grades
    # only the runtime events that the harness persisted.
    function: Union[
        ReportTaskCompletion,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_List,
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_MkDir,
        Req_Move,
    ] = Field(..., description="execute the first remaining step")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

system_prompt = """
You are a pragmatic personal knowledge management assistant.

- Always start by exploring the repository root with `tree`.
- Always read `/AGENTS.md` or `/AGENTS.MD` early when it exists.
- Operate through the PCM runtime file-system tools only.
- Keep edits small and targeted.
- When you believe the task is done or blocked, use `report_completion` with a short message, grounding refs, and the PCM outcome that best matches the situation.
- Do not invent tool results.
"""


# ---------------------------------------------------------------------------
# CLI colors
# ---------------------------------------------------------------------------

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


# ---------------------------------------------------------------------------
# Outcome map
# ---------------------------------------------------------------------------

OUTCOME_BY_NAME = {
    "OUTCOME_OK": Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
}


# ---------------------------------------------------------------------------
# Dispatch: Pydantic models -> PCM runtime methods
# ---------------------------------------------------------------------------

def dispatch(vm: PcmRuntimeClientSync, cmd: BaseModel):
    if isinstance(cmd, Req_Tree):
        return vm.tree(TreeRequest(root=cmd.root))
    if isinstance(cmd, Req_Find):
        return vm.find(
            FindRequest(
                root=cmd.root,
                name=cmd.name,
                type={"all": 0, "files": 1, "dirs": 2}[cmd.kind],
                limit=cmd.limit,
            )
        )
    if isinstance(cmd, Req_Search):
        return vm.search(SearchRequest(root=cmd.root, pattern=cmd.pattern, limit=cmd.limit))
    if isinstance(cmd, Req_List):
        return vm.list(ListRequest(name=cmd.path))
    if isinstance(cmd, Req_Read):
        return vm.read(ReadRequest(path=cmd.path))
    if isinstance(cmd, Req_Write):
        return vm.write(WriteRequest(path=cmd.path, content=cmd.content))
    if isinstance(cmd, Req_Delete):
        return vm.delete(DeleteRequest(path=cmd.path))
    if isinstance(cmd, Req_MkDir):
        return vm.mk_dir(MkDirRequest(path=cmd.path))
    if isinstance(cmd, Req_Move):
        return vm.move(MoveRequest(from_name=cmd.from_name, to_name=cmd.to_name))
    if isinstance(cmd, ReportTaskCompletion):
        # AICODE-NOTE: Keep the report-completion schema aligned with
        # `bitgn.vm.pcm.AnswerRequest`: PAC1 grading consumes the recorded outcome,
        # so the agent must choose one explicitly instead of relying on local-only status.
        return vm.answer(
            AnswerRequest(
                message=cmd.message,
                outcome=OUTCOME_BY_NAME[cmd.outcome],
                refs=cmd.grounding_refs,
            )
        )

    raise ValueError(f"Unknown command: {cmd}")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(model: str, harness_url: str, task_text: str, model_config: dict | None = None) -> None:
    cfg = model_config or {}
    client = _make_client()
    # AICODE-NOTE: PAC1 now imports the PCM SDK eagerly so missing generated
    # packages fail fast at startup instead of hiding behind the first tool call.
    vm = PcmRuntimeClientSync(harness_url)

    log = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_text},
    ]

    max_tokens = cfg.get("max_completion_tokens", 16384)
    _transient_kws = ("503", "502", "NoneType", "overloaded", "unavailable", "server error")

    for i in range(30):
        step = f"step_{i + 1}"
        print(f"Next {step}... ", end="")

        # FIX-27: Retry loop for transient provider errors
        job = None
        elapsed_ms = 0
        for _attempt in range(4):
            try:
                started = time.time()
                resp = client.beta.chat.completions.parse(
                    model=model,
                    response_format=NextStep,
                    messages=log,
                    max_completion_tokens=max_tokens,
                )
                elapsed_ms = int((time.time() - started) * 1000)
                job = resp.choices[0].message.parsed
                break
            except Exception as e:
                _err_str = str(e)
                _is_transient = any(kw.lower() in _err_str.lower() for kw in _transient_kws)
                if _is_transient and _attempt < 3:
                    print(f"{CLI_YELLOW}[FIX-27] Transient error (attempt {_attempt + 1}): {e} — retrying in 4s{CLI_CLR}")
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}LLM call error: {e}{CLI_CLR}")
                break

        if job is None:
            print(f"{CLI_RED}No valid response, stopping{CLI_CLR}")
            break

        print(job.plan_remaining_steps_brief[0], f"({elapsed_ms} ms)\n  {job.function}")

        log.append(
            {
                "role": "assistant",
                "content": job.plan_remaining_steps_brief[0],
                "tool_calls": [
                    {
                        "type": "function",
                        "id": step,
                        "function": {
                            "name": job.function.__class__.__name__,
                            "arguments": job.function.model_dump_json(),
                        },
                    }
                ],
            }
        )

        try:
            result = dispatch(vm, job.function)
            txt = json.dumps(MessageToDict(result), indent=2) if result else "{}"
            print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt}")
        except ConnectError as exc:
            txt = str(exc.message)
            print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")

        if isinstance(job.function, ReportTaskCompletion):
            status = CLI_GREEN if job.function.outcome == "OUTCOME_OK" else CLI_YELLOW
            print(f"{status}agent {job.function.outcome}{CLI_CLR}. Summary:")
            for item in job.function.completed_steps_laconic:
                print(f"- {item}")
            print(f"\n{CLI_BLUE}AGENT SUMMARY: {job.function.message}{CLI_CLR}")
            if job.function.grounding_refs:
                for ref in job.function.grounding_refs:
                    print(f"- {CLI_BLUE}{ref}{CLI_CLR}")
            break

        log.append({"role": "tool", "content": txt, "tool_call_id": step})
