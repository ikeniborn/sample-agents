"""DSPy LM adapter backed by dispatch.call_llm_raw().

DispatchLM subclasses dspy.BaseLM (required by DSPy 3.x) and delegates all
LLM calls to the existing 3-tier routing infrastructure (Anthropic → OpenRouter → Ollama).

DSPy 3.x uses JSONAdapter: it formats prompts asking the model to respond with a JSON
object containing the output fields. For Ollama backends, response_format=json_object
is enabled to enforce valid JSON output.

Usage:
    from agent.dspy_lm import DispatchLM
    import dspy

    lm = DispatchLM(model, cfg, max_tokens=300)
    with dspy.context(lm=lm):
        result = predictor(field=...)
"""
from __future__ import annotations

import json as _json
import re as _re

import dspy

from .dispatch import call_llm_raw

# JSONAdapter single-field trailer: "Respond with a JSON object in the following order of fields: `fieldname`."
_JSONADAPTER_SINGLE_RE = _re.compile(
    r'Respond with a JSON object in the following order of fields: `(\w+)`\.\s*$',
    _re.MULTILINE,
)


def _coerce_to_json(raw: str, user_msg: str) -> str:
    """Wrap plain-text response as JSON when JSONAdapter expects a single output field.

    kimi-k2.5:cloud ignores response_format=json_object and returns plain text.
    For single-field signatures (e.g. PromptAddendum → addendum), wrap the content
    so DSPy JSONAdapter can parse it. Multi-field signatures are left unchanged.
    """
    if not raw or raw.strip().startswith("{"):
        return raw
    m = _JSONADAPTER_SINGLE_RE.search(user_msg)
    if m:
        return _json.dumps({m.group(1): raw})
    return raw


# ---------------------------------------------------------------------------
# Minimal OpenAI-compatible response objects expected by dspy.BaseLM
# ---------------------------------------------------------------------------

class _Usage:
    """Minimal usage object supporting dict() conversion."""

    def __init__(self, in_tok: int = 0, out_tok: int = 0) -> None:
        self.prompt_tokens = in_tok
        self.completion_tokens = out_tok
        self.total_tokens = in_tok + out_tok

    # Support dict(usage) as called by dspy.BaseLM._process_lm_response
    def keys(self):
        return ("prompt_tokens", "completion_tokens", "total_tokens")

    def __getitem__(self, key: str):
        return getattr(self, key)


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _Response:
    """Minimal OpenAI ChatCompletion-like response for dspy.BaseLM._process_completion."""

    def __init__(self, content: str, in_tok: int = 0, out_tok: int = 0) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage(in_tok, out_tok)
        self.model = "custom"


# ---------------------------------------------------------------------------
# DispatchLM
# ---------------------------------------------------------------------------

class DispatchLM(dspy.BaseLM):
    """dspy.BaseLM subclass delegating calls to dispatch.call_llm_raw().

    DSPy 3.x requires BaseLM subclassing and calls forward() which must return
    an OpenAI-compatible response. Token counts are stored in _last_tokens after
    each forward call for the caller to retrieve.
    """

    def __init__(self, model: str, cfg: dict, max_tokens: int = 512, json_mode: bool = True) -> None:
        super().__init__(
            model=model,
            cache=False,        # disable DSPy cache — agent handles retries itself
            max_tokens=max_tokens,
        )
        self._dispatch_cfg = cfg
        self._last_tokens: dict = {"input": 0, "output": 0}
        self._json_mode = json_mode

    def forward(
        self,
        prompt: str | None = None,
        messages: list[dict] | None = None,
        **kwargs,
    ) -> _Response:
        """Extract system + user content from DSPy messages and call call_llm_raw().

        DSPy 3.x passes messages in OpenAI format:
          [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]

        Returns an OpenAI-compatible _Response object that dspy.BaseLM._process_completion
        can extract text from.
        """
        system = ""
        user_parts: list[str] = []

        for m in messages or []:
            role = m.get("role", "")
            content = m.get("content", "") or ""
            if role == "system":
                system = content
            elif role in ("user", "human"):
                user_parts.append(content)

        user_msg = prompt or "\n\n".join(user_parts)

        tok: dict = {}
        raw = call_llm_raw(
            system=system,
            user_msg=user_msg,
            model=self.model,
            cfg=self._dispatch_cfg,
            max_tokens=self.kwargs.get("max_tokens", 512),
            think=False,
            token_out=tok,
            plain_text=not self._json_mode,
        )
        raw = _coerce_to_json(raw or "", user_msg)
        self._last_tokens = tok
        return _Response(
            content=raw or "",
            in_tok=tok.get("input", 0),
            out_tok=tok.get("output", 0),
        )
