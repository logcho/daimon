"""Fake models for CI tests — no network, no API keys. A ScriptedModel pops
queued AIMessages per call; a FakeRouter routes pro/flash/judge roles to
independently-scripted instances and records which roles were used."""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool


class ScriptedModel(BaseChatModel):
    """Pops the next queued AIMessage per _generate; answers "Task complete."
    when the script runs dry. Records every call's message list."""

    model_name: str = "scripted"  # BaseChatModel requires it
    script: list[AIMessage] = []
    calls: list[list[BaseMessage]] = []
    raise_on_call: bool = False

    def __init__(self, script: Optional[list[AIMessage]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script = list(script or [])
        self.calls = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        # Scripted models don't need real tool schemas — the graph only asks.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.raise_on_call:
            raise RuntimeError("model exploded")
        self.calls.append(list(messages))
        if self.script:
            message = self.script.pop(0)
        else:
            message = AIMessage(content="Task complete.")
        return ChatResult(generations=[ChatGeneration(message=message)])

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

    def _run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        raise RuntimeError("fake tool exploded")


class FakeRouter:
    """Pro/flash/judge roles, each a ScriptedModel with its own script."""

    def __init__(
        self,
        pro_script: Optional[list[AIMessage]] = None,
        flash_script: Optional[list[AIMessage]] = None,
        judge_script: Optional[list[AIMessage]] = None,
    ) -> None:
        self._pro = ScriptedModel(pro_script)
        self._flash = ScriptedModel(flash_script)
        self._judge = ScriptedModel(judge_script)

    def pro(self) -> ScriptedModel:
        return self._pro

    def flash(self) -> ScriptedModel:
        return self._flash

    def judge(self) -> ScriptedModel:
        return self._judge


def tool_call(name: str, args: dict, id: str | None = None, content: str = "") -> AIMessage:
    """An AIMessage that calls one tool."""
    return AIMessage(
        content=content,
        tool_calls=[{"name": name, "args": args, "id": id or f"call-{name}", "type": "tool_call"}],
    )
