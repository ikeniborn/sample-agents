import json
import time
from typing import Annotated, List, Literal, Union

from annotated_types import Ge, Le, MaxLen, MinLen
from google.protobuf.json_format import MessageToDict
from openai import OpenAI
from pydantic import BaseModel, Field

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
from connectrpc.errors import ConnectError

client = OpenAI()


class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    completed_steps_laconic: List[str]
    answer: str
    refs: List[str] = Field(default_factory=list)

    code: Literal["completed", "failed"]


class Req_Outline(BaseModel):
    tool: Literal["outline"]
    path: str


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: str
    count: Annotated[int, Ge(1), Le(10)] = 5
    path: str = "/"


class Req_List(BaseModel):
    tool: Literal["list"]
    path: str


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


class Req_Answer(BaseModel):
    tool: Literal["answer"]
    answer: str
    refs: List[str] = Field(default_factory=list)


class NextStep(BaseModel):
    current_state: str
    # we'll use only the first step, discarding all the rest.
    plan_remaining_steps_brief: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ...,
        description="explain your thoughts on how to accomplish - what steps to execute",
    )
    # now let's continue the cascade and check with LLM if the task is done
    task_completed: bool
    # AICODE-NOTE: Keep this union aligned with the MiniRuntime protobuf surface so
    # structured tool calling stays exhaustive as demo VM request types evolve.
    function: Union[
        ReportTaskCompletion,
        Req_Outline,
        Req_Search,
        Req_List,
        Req_Read,
        Req_Write,
        Req_Delete,
    ] = Field(..., description="execute first remaining step")


system_prompt = """
You are a personal business assistant, helfpul and smart.
 
- always start by discovering available information by running root outline.
- always read `AGENTS.md` at the start
- always reference all files that contributed to the answer
- Clearly report when tasks are done
"""


CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"


def dispatch(vm: MiniRuntimeClientSync, cmd: BaseModel):
    if isinstance(cmd, Req_Outline):
        return vm.outline(OutlineRequest(path=cmd.path))
    if isinstance(cmd, Req_Search):
        return vm.search(SearchRequest(path=cmd.path, pattern=cmd.pattern, count=cmd.count))
    if isinstance(cmd, Req_List):
        return vm.list(ListRequest(path=cmd.path))
    if isinstance(cmd, Req_Read):
        return vm.read(ReadRequest(path=cmd.path))
    if isinstance(cmd, Req_Write):
        return vm.write(WriteRequest(path=cmd.path, content=cmd.content))
    if isinstance(cmd, Req_Delete):
        return vm.delete(DeleteRequest(path=cmd.path))
    if isinstance(cmd, ReportTaskCompletion):
        return vm.answer(AnswerRequest(answer=cmd.answer, refs=cmd.refs))



    raise ValueError(f"Unknown command: {cmd}")


def run_agent(model: str, harness_url: str, task_text: str):
    vm = MiniRuntimeClientSync(harness_url)

    # log will contain conversation context for the agent within task
    log = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_text},
    ]

    # let's limit number of reasoning steps by 20, just to be safe
    for i in range(30):
        step = f"step_{i + 1}"
        print(f"Next {step}... ", end="")

        started = time.time()

        resp = client.beta.chat.completions.parse(
            model=model,
            response_format=NextStep,
            messages=log,
            max_completion_tokens=16384,
        )

        job = resp.choices[0].message.parsed

        # print next sep for debugging
        print(job.plan_remaining_steps_brief[0], f"\n  {job.function}")

        # Let's add tool request to conversation history as if OpenAI asked for it.
        # a shorter way would be to just append `job.model_dump_json()` entirely
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

        # now execute the tool by dispatching command to our handler
        try:
            result = dispatch(vm, job.function)
            mappe = MessageToDict(result)
            txt = json.dumps(mappe, indent=2)
            print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt}")
        except ConnectError as e:
            txt = str(e.message)
            # print to console as ascii red
            print(f"{CLI_RED}ERR {e.code}: {e.message}{CLI_CLR}")

        # was this the completion?
        if isinstance(job.function, ReportTaskCompletion):
            print(f"{CLI_GREEN}agent {job.function.code}{CLI_CLR}. Summary:")
            for s in job.function.completed_steps_laconic:
                print(f"- {s}")
            break

        # and now we add results back to the convesation history, so that agent
        # we'll be able to act on the results in the next reasoning step.
        log.append({"role": "tool", "content": txt, "tool_call_id": step})
