"""The live region — everything currently in flight, and nothing else.

The split this module exists to enforce: a line is *live* while the thing it
describes is still happening, and *finalized* the moment it isn't. Finalized
lines get printed once into the terminal's own scrollback and are never touched
again; live lines redraw every frame.

That is the whole reason scrolling works. The alternative — one big buffer the
app owns and re-renders — means the app also owns scrolling, which means
reimplementing the wheel, the scrollbar, text selection, and find-in-page,
badly. Here the terminal keeps all of that, because from its point of view the
transcript is just output.

`LiveState.consume` folds one agent event and returns the lines to promote into
the transcript. Pure apart from the clock: no printing, no prompt_toolkit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import render
from .render import AskState, TurnStats


@dataclass
class RunningStep:
    name: str
    detail: str | None = None
    started: float = field(default_factory=time.monotonic)
    agent_id: str | None = None


@dataclass
class SubAgent:
    """One concurrently-running sub-agent's own progress."""

    agent_id: str
    label: str
    query: str
    started: float = field(default_factory=time.monotonic)
    tools: int = 0
    tokens: int = 0
    status: str = "running"
    current: str | None = None


class LiveState:
    """What the bottom of the screen shows while a turn runs."""

    def __init__(self, *, width: int = 80) -> None:
        self.width = width
        self.stats = TurnStats()
        self.todos: list[dict] = []
        self.ask: AskState | None = None

        self.thinking_started: float | None = None
        self.steps: dict[str, RunningStep] = {}
        self.subagents: dict[str, SubAgent] = {}
        #: Streamed answer text not yet flushed to the transcript — always a
        #: partial final line, since whole lines are promoted as they complete.
        self.pending_text: str = ""
        #: Whether any answer text streamed this turn. The `done` event still
        #: carries the full result for clients that don't render deltas, so
        #: this is how the TUI knows not to print the same answer twice.
        self.streamed_any: bool = False
        #: Reasoning is shown while it streams and then dropped; keeping a
        #: model's scratch work in permanent scrollback buries the answer.
        self.reasoning_tail: str = ""

        self.frame_idx = 0
        self.verb_idx = 0
        self._verb_elapsed = 0.0

    # --- animation ----------------------------------------------------------

    def tick(self, dt: float) -> None:
        self.frame_idx += 1
        self._verb_elapsed += dt
        if self._verb_elapsed >= render.VERB_INTERVAL:
            self._verb_elapsed = 0.0
            self.verb_idx += 1

    @property
    def busy(self) -> bool:
        return self.thinking_started is not None

    # --- event folding ------------------------------------------------------

    def consume(self, event: dict) -> list[str]:
        """Fold one event. Returns lines to append to the transcript now."""
        etype = event.get("type")
        handler = getattr(self, f"_on_{etype}", None)
        return handler(event) if handler is not None else []

    def _on_step(self, event: dict) -> list[str]:
        status = event.get("status")
        label = event.get("label", "")
        tool = event.get("tool")
        step_id = event.get("id", "")
        name = tool or label
        agent_id = event.get("agent_id")
        parent = event.get("parent_step_id")

        if name == "Thinking":
            if status == "running":
                # A resumed turn re-opens Thinking; keep the original clock so
                # the elapsed time covers the whole turn, pause included.
                if self.thinking_started is None:
                    self.thinking_started = time.monotonic()
            elif status == "done":
                self.thinking_started = None
            return []

        # A step belonging to a sub-agent updates that agent's block rather
        # than the top-level list — this is what keeps concurrent agents
        # legible instead of interleaved.
        if parent is not None:
            agent = self.subagents.get(parent)
            if agent is not None:
                if status == "running":
                    agent.current = name
                else:
                    agent.tools += 1
                    agent.current = None
            return []

        # A spawn: the step carries an agent_label and its own id.
        if event.get("agent_label") and status == "running":
            self.subagents[step_id] = SubAgent(
                agent_id=step_id,
                label=str(event["agent_label"]),
                query=str(event.get("detail") or label),
            )
            return []

        if event.get("agent_label") and status in ("done", "error"):
            agent = self.subagents.pop(step_id, None)
            if agent is None:
                return []
            return [
                render.subagent_line(
                    agent.label, agent.query, status,
                    tools=agent.tools,
                    tokens=agent.tokens,
                    elapsed_ms=event.get("elapsed_ms"),
                    width=self.width,
                )
            ]

        if status == "running":
            self.steps[step_id] = RunningStep(
                name=name, detail=event.get("detail"), agent_id=agent_id
            )
            return []

        if status in ("done", "error"):
            self.steps.pop(step_id, None)
            # Finished: it will never change again, so it belongs in scrollback.
            return [
                render.step_line(
                    name, status,
                    detail=event.get("detail"),
                    elapsed_ms=event.get("elapsed_ms"),
                    width=self.width,
                )
            ]
        return []

    def _on_assistant_delta(self, event: dict) -> list[str]:
        text = str(event.get("text", ""))
        if event.get("channel") == "reasoning":
            self.reasoning_tail = (self.reasoning_tail + text)[-400:]
            return []
        if event.get("agent_id"):
            return []  # a sub-agent's narration is not the user's transcript
        if text.strip():
            self.streamed_any = True
        self.pending_text += text
        # Promote whole lines as they complete; the unfinished tail stays live
        # so the text appears to type itself without ever being rewritten.
        if "\n" not in self.pending_text:
            return []
        *complete, self.pending_text = self.pending_text.split("\n")
        return complete

    def _on_usage(self, event: dict) -> list[str]:
        self.stats.add_usage(event)
        agent_id = event.get("agent_id")
        if agent_id and agent_id in self.subagents:
            self.subagents[agent_id].tokens += int(event.get("input_tokens", 0)) + int(
                event.get("output_tokens", 0)
            )
        return []

    def _on_todo(self, event: dict) -> list[str]:
        self.todos = list(event.get("items") or [])
        return []

    def _on_compaction(self, event: dict) -> list[str]:
        return [render.compaction_line(event)]

    def _on_ask(self, event: dict) -> list[str]:
        self.ask = AskState(event=event)
        self.thinking_started = None
        promoted = self.flush_text()
        if event.get("kind") == "plan" and event.get("plan"):
            promoted += render.plan_block(str(event["plan"]))
        return promoted

    def _on_error(self, event: dict) -> list[str]:
        self.thinking_started = None
        return self.flush_text() + render.error_lines(str(event.get("message", "")))

    # --- lifecycle ----------------------------------------------------------

    def flush_text(self) -> list[str]:
        """Promote whatever partial line is still live. Called at the end of a
        turn, when there is no more text coming to complete it."""
        if not self.pending_text:
            return []
        text, self.pending_text = self.pending_text, ""
        return [text]

    def start_turn(self) -> None:
        self.streamed_any = False
        self.stats.start_turn()

    def end_turn(self) -> list[str]:
        """Close out a turn: promote the tail, drop anything still marked
        running (nothing is running once the turn is over)."""
        promoted = self.flush_text()
        self.thinking_started = None
        self.steps.clear()
        self.subagents.clear()
        self.reasoning_tail = ""
        return promoted

    def cancel(self) -> list[str]:
        promoted = self.end_turn()
        self.ask = None
        return promoted + ["", f"  {render.DIM}⊘ cancelled{render.RESET}"]

    # --- rendering ----------------------------------------------------------

    def lines(self) -> list[str]:
        """The live region, top to bottom."""
        if self.ask is not None:
            return render.ask_lines(self.ask, width=self.width)

        out: list[str] = []
        if self.todos:
            out += render.todo_lines(self.todos, width=self.width)
        for agent in self.subagents.values():
            out.append(
                render.subagent_line(
                    agent.label,
                    agent.current or agent.query,
                    "running",
                    tools=agent.tools,
                    tokens=agent.tokens,
                    width=self.width,
                )
            )
        for step in self.steps.values():
            out.append(
                render.step_line(
                    step.name, "running", detail=step.detail, width=self.width
                )
            )
        if self.pending_text:
            out.append(self.pending_text)
        if self.thinking_started is not None:
            if self.reasoning_tail and not self.steps and not self.subagents:
                tail = render.truncate(
                    self.reasoning_tail.replace("\n", " "), max(self.width - 6, 20)
                )
                out.append(f"  {render.DIM}{tail}{render.RESET}")
            out.append(
                render.thinking_line(
                    frame_idx=self.frame_idx,
                    verb_idx=self.verb_idx,
                    started=self.thinking_started,
                    turn_tokens=self.stats.turn_tokens,
                )
            )
        return out
