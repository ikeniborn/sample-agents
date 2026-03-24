from typing import Literal, Union

from pydantic import BaseModel, Field


class Navigate(BaseModel):
    tool: Literal["navigate"]
    action: Literal["tree", "list"]
    path: str = Field(default="/")


class Inspect(BaseModel):
    tool: Literal["inspect"]
    action: Literal["read", "search"]
    path: str = Field(default="/")
    pattern: str = Field(default="", description="Search pattern, only for search")


class Modify(BaseModel):
    tool: Literal["modify"]
    action: Literal["write", "delete"]
    path: str
    content: str = Field(default="", description="File content, only for write")


class Finish(BaseModel):
    tool: Literal["finish"]
    answer: str
    refs: list[str] = Field(default_factory=list)
    code: Literal["completed", "failed"]


class MicroStep(BaseModel):
    think: str = Field(description="ONE sentence: what I do and why")
    prev_result_ok: bool = Field(description="Was previous step useful? true for first step")
    prev_result_problem: str = Field(default="", description="If false: what went wrong")
    action: Union[Navigate, Inspect, Modify, Finish] = Field(description="Next action")
