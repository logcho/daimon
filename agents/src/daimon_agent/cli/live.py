"""The live region — everything currently in flight, and nothing else.

The split this module enforces: a line is *live* while the thing it describes
is still happening, and *promoted* the moment it isn't. Live lines redraw every
frame; promoted lines go to the transcript and settle.

**A promoted line is settled, not immutable.** An earlier version of this
docstring claimed finalized lines were printed straight into the terminal's own
scrollback and never touched again. They are not — `tui.Transcript` is a buffer
the app owns and re-renders every frame. That is what makes a collapsed run
expandable after the fact, and it is why `_flush_run` can hand the TUI a `Fold`
rather than a string.

The parent agent's own tool calls do not each earn a permanent row. They
accumulate into a *run*, which is flushed as one collapsed line whenever
something worth seeing separately needs promoting — a sub-agent reporting back,
a line of the answer, the end of the turn. Sub-agents already worked this way;
this is the rest of the turn catching up.

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
class FinishedStep:
    """A tool call that has completed — kept so a collapsed run can still show
    what it was called *with* when the user expands it."""

    name: str
    status: str
    detail: str | None = None
    elapsed_ms: int | None = None


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
    #: Its own finished calls, so the line it promotes can be expanded too.
    steps: list[FinishedStep] = field(default_factory=list)


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
        #: The parent agent's own finished calls, waiting to be promoted as one
        #: collapsed row. See `_flush_run` for when that happens.
        self.run: list[FinishedStep] = []
        self.run_started: float | None = None
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

    def consume(self, event: dict) -> list:
        """Fold one event. Returns what to append to the transcript now —
        plain lines, and `render.Fold` blocks for anything expandable."""
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
                    agent.steps.append(
                        FinishedStep(
                            name=name,
                            status=str(status),
                            detail=event.get("detail"),
                            elapsed_ms=event.get("elapsed_ms"),
                        )
                    )
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

            def line(expanded: bool) -> str:
                return render.subagent_line(
                    agent.label, agent.query, str(status),
                    tools=agent.tools,
                    tokens=agent.tokens,
                    elapsed_ms=event.get("elapsed_ms"),
                    expanded=expanded,
                    width=self.width,
                )

            # The parent's own calls happened before this agent reported back,
            # so they have to reach the transcript first.
            promoted = self._flush_run()
            if not agent.steps:
                return promoted + [line(False)]
            return promoted + [
                render.Fold(
                    closed=line(False),
                    open=line(True),
                    detail=self._detail_rows(agent.steps, indent=6),
                )
            ]

        if status == "running":
            self.steps[step_id] = RunningStep(
                name=name, detail=event.get("detail"), agent_id=agent_id
            )
            if self.run_started is None:
                self.run_started = time.monotonic()
            return []

        if status in ("done", "error"):
            self.steps.pop(step_id, None)
            # Not promoted yet: it joins the run and reaches the transcript as
            # part of one collapsed row. It stays on screen meanwhile — the
            # live region's rolling row counts the run, not just what is
            # currently in flight.
            self.run.append(
                FinishedStep(
                    name=name,
                    status=str(status),
                    detail=event.get("detail"),
                    elapsed_ms=event.get("elapsed_ms"),
                )
            )
            return []
        return []

    def _on_assistant_delta(self, event: dict) -> list[str]:
        text = str(event.get("text", ""))
        if event.get("channel") == "reasoning":
            self.reasoning_tail = (self.reasoning_tail + text)[-400:]
            return []
        if event.get("agent_id"):
            return []  # a sub-agent's narration is not the user's transcript
        # Something is about to land in the transcript, so the run that
        # happened before it has to land first. The partial line already on
        # screen goes ahead of both — it is older still.
        promoted = self.flush_text() + self._flush_run() if self.run else []
        if text.strip():
            self.streamed_any = True
        self.pending_text += text
        # Promote whole lines as they complete; the unfinished tail stays live
        # so the text appears to type itself without ever being rewritten.
        if "\n" not in self.pending_text:
            return promoted
        *complete, self.pending_text = self.pending_text.split("\n")
        return promoted + complete

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

    def _on_compaction(self, event: dict) -> list:
        return self._flush_run() + [render.compaction_line(event)]

    def _on_retry(self, event: dict) -> list:
        return self._flush_run() + [render.retry_line(event)]

    def _on_continuation(self, event: dict) -> list[str]:
        # Promoted, not live: it marks a moment rather than describing
        # something still in flight.
        return self.flush_text() + self._flush_run() + [render.continuation_line(event)]

    def _on_ask(self, event: dict) -> list[str]:
        self.ask = AskState(event=event)
        self.thinking_started = None
        promoted = self.flush_text() + self._flush_run()
        if event.get("kind") == "plan" and event.get("plan"):
            promoted += render.plan_block(str(event["plan"]))
        return promoted

    def _on_error(self, event: dict) -> list[str]:
        self.thinking_started = None
        return (
            self.flush_text()
            + self._flush_run()
            + render.error_lines(str(event.get("message", "")))
        )

    # --- runs ---------------------------------------------------------------

    def _detail_rows(self, steps: list[FinishedStep], *, indent: int) -> list[str]:
        """The rows a fold reveals, capped. Past `MAX_EXPANDED` nobody is
        reading, and every row costs a re-wrap on every resize."""
        rows = [
            render.step_line(
                step.name, step.status,
                detail=step.detail,
                elapsed_ms=step.elapsed_ms,
                indent=indent,
                width=self.width,
            )
            for step in steps[: render.MAX_EXPANDED]
        ]
        if len(steps) > render.MAX_EXPANDED:
            extra = len(steps) - render.MAX_EXPANDED
            rows.append(f"{' ' * indent}{render.DIM}…{extra} more{render.RESET}")
        return rows

    def _flush_run(self) -> list:
        """Close the open run and return what to promote.

        Called before anything else reaches the transcript, because the
        transcript is ordered and these calls happened first. A run of one
        promotes a plain step line: hiding a single call behind a chevron costs
        a keypress and saves nothing.
        """
        steps, self.run = self.run, []
        started, self.run_started = self.run_started, None
        if not steps:
            return []
        if len(steps) == 1:
            step = steps[0]
            return [
                render.step_line(
                    step.name, step.status,
                    detail=step.detail,
                    elapsed_ms=step.elapsed_ms,
                    width=self.width,
                )
            ]
        elapsed_ms = int((time.monotonic() - started) * 1000) if started else None
        kwargs = dict(
            categories=render.category_parts([s.name for s in steps]),
            failed=sum(1 for s in steps if s.status == "error"),
            elapsed_ms=elapsed_ms,
            width=self.width,
        )
        return [
            render.Fold(
                closed=render.run_line(len(steps), expanded=False, **kwargs),
                open=render.run_line(len(steps), expanded=True, **kwargs),
                detail=self._detail_rows(steps, indent=4),
            )
        ]

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

    def end_turn(self) -> list:
        """Close out a turn: promote the tail and the open run, then drop
        anything still marked running (nothing is running once the turn is
        over)."""
        promoted = self.flush_text() + self._flush_run()
        self.thinking_started = None
        self.steps.clear()
        self.subagents.clear()
        self.reasoning_tail = ""
        return promoted

    def cancel(self) -> list:
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
        # One row for the parent's own work, however many calls are in flight.
        # It counts the whole run rather than what is running, which is also
        # what keeps a finished call on screen: it has left `self.steps` but
        # will not reach the transcript until the run is flushed.
        running = list(self.steps.values())
        pending = len(self.run) + len(running)
        if pending == 1 and running:
            out.append(
                render.step_line(
                    running[0].name, "running",
                    detail=running[0].detail,
                    width=self.width,
                )
            )
        elif pending:
            # `self.steps` is insertion-ordered, so the last one started is
            # the one worth naming — and when nothing is in flight, the last
            # one to finish, so the row never loses the name mid-run.
            current = running[-1] if running else self.run[-1]
            out.append(
                render.run_active_line(
                    pending,
                    current.name if current else None,
                    current.detail if current else None,
                    failed=sum(1 for s in self.run if s.status == "error"),
                    width=self.width,
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
