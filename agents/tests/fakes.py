"""Fake models for CI tests — no network, no API keys. A ScriptedModel pops
queued AIMessages per call; a FakeRouter routes pro/flash/judge roles to
independently-scripted instances and records which roles were used.

ScriptedModel implements `_astream` as well as `_generate`, because the agent
node streams: without it BaseChatModel would fall back to wrapping `_generate`
in a single chunk, which works but wouldn't exercise the accumulation path the
real models take. `usage_metadata` can be scripted per message so the usage
accounting is testable without a provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Optional

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool


class ScriptedModel(BaseChatModel):
    """Pops the next queued AIMessage per call; answers "Task complete."
    when the script runs dry. Records every call's message list."""

    model_name: str = "scripted"  # BaseChatModel requires it
    script: list[AIMessage] = []
    calls: list[list[BaseMessage]] = []
    raise_on_call: bool = False
    #: Attempts that yield one chunk and *then* raise a transient-looking
    #: error, before the next attempt succeeds. This is the failure the
    #: provider client's own max_retries cannot see: the request was accepted,
    #: the stream started, and it died partway through.
    raise_mid_stream: int = 0
    #: Seconds to sleep inside _astream, so a test can prove two subagents
    #: overlap in time rather than merely both finishing.
    delay: float = 0.0

    def __init__(self, script: Optional[list[AIMessage]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script = list(script or [])
        self.calls = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        # Scripted models don't need real tool schemas — the graph only asks.
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        if self.raise_on_call:
            raise RuntimeError("model exploded")
        self.calls.append(list(messages))
        if self.script:
            return self.script.pop(0)
        return AIMessage(content="Task complete.")

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        import asyncio

        if self.raise_mid_stream:
            self.raise_mid_stream -= 1
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            raise ConnectionError("the stream died partway through")

        message = self._next(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = message.content if isinstance(message.content, str) else ""
        # Split the text so consumers see more than one delta — a single-chunk
        # stream would hide ordering bugs in the accumulation.
        pieces = [text[i : i + 8] for i in range(0, len(text), 8)] or [""]
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            chunk = AIMessageChunk(
                content=piece,
                # Tool calls and usage ride the final chunk, as they do for a
                # real provider.
                tool_calls=message.tool_calls if last else [],
                # Malformed calls ride the final chunk too. They are how a
                # real provider reports arguments that didn't parse, and the
                # graph has to answer them like any other open call.
                invalid_tool_calls=message.invalid_tool_calls if last else [],
                usage_metadata=getattr(message, "usage_metadata", None) if last else None,
                # A scripted message's own metadata wins, so a test can name
                # the model it wants priced.
                response_metadata=(
                    {"model_name": self.model_name, **(message.response_metadata or {})}
                    if last
                    else {}
                ),
            )
            yield ChatGenerationChunk(message=chunk)

    @property
    def _llm_type(self) -> str:
        return "scripted"


class FakeTool(BaseTool):
    """Scripted tool: records calls, pops canned responses, raises when the
    response script runs dry."""

    name: str
    description: str = "A fake tool for tests."
    responses: list[str] = []
    calls: list[dict] = []
    #: Seconds `_arun` awaits before answering. A test proves the tools node
    #: runs calls concurrently by making each one slow and timing the batch —
    #: sequential execution shows up as the sum, concurrent as the max.
    delay: float = 0.0

    def _run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        raise RuntimeError("fake tool exploded")

    async def _arun(self, **kwargs: Any) -> Any:
        import asyncio

        # Record the call before sleeping: concurrent calls must all be visible
        # as in-flight, not appear one at a time as each finishes.
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.responses:
            return self.responses.pop(0)
        raise RuntimeError("fake tool exploded")


class FakeRouter:
    """Pro/flash/judge roles, each a ScriptedModel with its own script.

    Mirrors ModelRouter's surface, including the `streaming` keyword the graph
    passes — a scripted model streams either way, so it is accepted and
    ignored."""

    def __init__(
        self,
        pro_script: Optional[list[AIMessage]] = None,
        flash_script: Optional[list[AIMessage]] = None,
        judge_script: Optional[list[AIMessage]] = None,
    ) -> None:
        self._pro = ScriptedModel(pro_script, model_name="scripted-pro")
        self._flash = ScriptedModel(flash_script, model_name="scripted-flash")
        self._judge = ScriptedModel(judge_script, model_name="scripted-judge")

    def pro(self, *, streaming: bool = False) -> ScriptedModel:
        return self._pro

    def flash(self, *, streaming: bool = False) -> ScriptedModel:
        return self._flash

    def judge(self) -> ScriptedModel:
        return self._judge

    def for_role(self, role: str, *, streaming: bool = False) -> ScriptedModel:
        return self._pro if role == "pro" else self._flash

    def spec_for_role(self, role: str) -> str:
        return "scripted-pro" if role == "pro" else "scripted-flash"

    def model_name(self, role: str) -> str:
        return self.spec_for_role(role)


def tool_call(name: str, args: dict, id: str | None = None, content: str = "") -> AIMessage:
    """An AIMessage that calls one tool."""
    return AIMessage(
        content=content,
        tool_calls=[{"name": name, "args": args, "id": id or f"call-{name}", "type": "tool_call"}],
    )


def invalid_tool_call(name: str, raw_args: str = "abc", id: str | None = None) -> AIMessage:
    """An AIMessage carrying one *malformed* tool call.

    Built through `tool_call_chunks` rather than by setting the field directly,
    so LangChain's own parser decides it's invalid — the same route a real
    provider's stream takes when the model emits unparseable arguments.
    """
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": name,
                "args": raw_args,
                "id": id or f"call-{name}-bad",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
    )
    return AIMessage(
        content="",
        tool_calls=list(chunk.tool_calls),
        invalid_tool_calls=list(chunk.invalid_tool_calls),
    )
