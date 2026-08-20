"""The display layer, tested without a terminal.

`render` is pure formatting and `live` is a pure fold over events, so the two
together cover everything the TUI shows — what's left in tui.py is key
bindings and plumbing.
"""

from __future__ import annotations

from daimon_agent.cli import render
from daimon_agent.cli.live import LiveState
from daimon_agent.cli.render import AskState, TurnStats, visible_len


# --- formatting --------------------------------------------------------------

def test_duration_scales_with_magnitude() -> None:
    assert render.fmt_duration(4.25) == "4.2s"
    assert render.fmt_duration(90) == "1m30s"
    assert render.fmt_duration(3900) == "1h05m"


def test_unpriced_turns_show_no_cost_at_all() -> None:
    """A zero would read as free; nothing reads as unknown, which is true."""
    assert render.fmt_cost(None) == ""
    assert render.fmt_cost(0.0031) == "$0.0031"
    assert render.fmt_cost(1.5) == "$1.50"


def test_truncate_collapses_whitespace_and_marks_the_cut() -> None:
    assert render.truncate("a   b\n c", 40) == "a b c"
    assert render.truncate("x" * 50, 10).endswith("…")
    assert len(render.truncate("x" * 50, 10)) == 10


def test_step_line_shows_the_argument_not_just_the_tool() -> None:
    """Twelve `read_file` lines say nothing; twelve paths say what was read."""
    line = render.step_line("read_file", "done", detail="src/daimon_agent/graph.py")
    assert "read_file" in line
    assert "graph.py" in line


def test_step_line_stays_on_one_line() -> None:
    """A long path must be trimmed to fit, or it wraps and breaks the
    one-line-per-step rhythm the transcript depends on."""
    line = render.step_line(
        "grep_files", "running", detail="x" * 300, elapsed_ms=1500, width=80
    )
    assert visible_len(line) <= 80


def test_step_line_hides_trivial_durations() -> None:
    assert "(" not in render.step_line("read_file", "done", elapsed_ms=12)
    assert "0.4s" in render.step_line("read_file", "done", elapsed_ms=400)


# --- todos -------------------------------------------------------------------

def test_todo_lines_show_progress_and_mark_the_active_item() -> None:
    lines = render.todo_lines(
        [
            {"id": "1", "text": "read the code", "status": "done"},
            {"id": "2", "text": "write the fix", "status": "in_progress"},
            {"id": "3", "text": "run tests", "status": "pending"},
        ]
    )
    assert "1/3" in lines[0]
    assert "read the code" in lines[1]
    assert "write the fix" in lines[2]
    assert len(lines) == 4


def test_empty_todo_list_renders_nothing() -> None:
    assert render.todo_lines([]) == []


# --- the status bar ----------------------------------------------------------

def test_status_line_reports_tokens_cost_and_context() -> None:
    stats = TurnStats(turns=3, input_tokens=34_100, output_tokens=4_200, cost_usd=0.031)
    stats.context_tokens = 43_520
    line = render.status_line("cli", "deepseek-chat", stats, context_window=128_000)
    assert "cli" in line and "deepseek-chat" in line
    assert "3 turns" in line
    assert "34.1k" in line and "4.2k" in line
    assert "$0.03" in line
    assert "ctx 34%" in line


def test_status_line_omits_cost_when_a_model_is_unpriced() -> None:
    stats = TurnStats(input_tokens=100, output_tokens=10)
    stats.cost_unknown = True
    assert "$" not in render.status_line("cli", "custom", stats)


def test_status_line_announces_plan_mode() -> None:
    assert "plan mode" in render.status_line("cli", "m", TurnStats(), mode="plan")


def test_thinking_line_shows_elapsed_and_the_way_out() -> None:
    """A silent agent and a wedged one look identical without a clock."""
    line = render.thinking_line(
        frame_idx=0, verb_idx=0, started=100.0, turn_tokens=3100, now=142.4
    )
    assert "42.4s" in line
    assert "3.1k tokens" in line
    assert "esc to cancel" in line


# --- the ask prompt ----------------------------------------------------------

def _ask(**over) -> AskState:
    event = {
        "type": "ask",
        "id": "a1",
        "kind": "question",
        "question": "Which database?",
        "header": "Storage",
        "options": [
            {"label": "Postgres", "description": "Battle-tested"},
            {"label": "SQLite", "description": "Zero setup"},
        ],
        "multi_select": False,
    }
    event.update(over)
    return AskState(event=event)


def test_ask_lines_show_the_question_and_numbered_options() -> None:
    lines = "\n".join(render.ask_lines(_ask()))
    assert "Storage" in lines
    assert "Which database?" in lines
    assert "1." in lines and "2." in lines
    assert "Postgres" in lines and "Battle-tested" in lines
    assert "e write your own" in lines  # the escape hatch is always offered


def test_ask_cursor_moves_the_marker() -> None:
    state = _ask()
    assert "❯" in render.ask_lines(state)[4]  # first option
    state.cursor = 1
    assert "❯" in render.ask_lines(state)[5]


def test_multi_select_shows_checkboxes_and_a_different_hint() -> None:
    state = _ask(multi_select=True)
    state.selected = {1}
    lines = render.ask_lines(state)
    assert "◉" in lines[5]
    assert "○" in lines[4]
    assert "space toggle" in lines[-1]


def test_plan_block_is_empty_for_an_empty_plan() -> None:
    assert render.plan_block("   ") == []
    assert any("do the thing" in line for line in render.plan_block("do the thing"))


# --- the live fold -----------------------------------------------------------

def _step(**over) -> dict:
    event = {"type": "step", "id": "s1", "label": "read_file", "status": "running",
             "tool": "read_file"}
    event.update(over)
    return event


def test_running_steps_stay_live_and_finished_ones_are_promoted() -> None:
    """The core rule: a line is live while it's happening and permanent once
    it isn't. Nothing in scrollback ever needs revising."""
    state = LiveState()
    assert state.consume(_step()) == []  # nothing promoted yet
    assert any("read_file" in line for line in state.lines())

    promoted = state.consume(_step(status="done", elapsed_ms=420))
    assert len(promoted) == 1 and "read_file" in promoted[0]
    assert not any("read_file" in line for line in state.lines())


def test_thinking_opens_and_closes_the_busy_state() -> None:
    state = LiveState()
    state.consume({"type": "step", "id": "t", "label": "Thinking", "status": "running"})
    assert state.busy
    assert any("esc to cancel" in line for line in state.lines())
    state.consume({"type": "step", "id": "t", "label": "Thinking", "status": "done"})
    assert not state.busy


def test_streamed_text_promotes_whole_lines_and_keeps_the_tail_live() -> None:
    """Text appears to type itself: completed lines go to scrollback, the
    unfinished one redraws in place, and neither is ever rewritten."""
    state = LiveState()
    assert state.consume({"type": "assistant_delta", "text": "Hello "}) == []
    assert state.consume({"type": "assistant_delta", "text": "world\nSecond"}) == [
        "Hello world"
    ]
    assert state.pending_text == "Second"
    assert "Second" in state.lines()
    assert state.flush_text() == ["Second"]


def test_streamed_any_stops_the_answer_printing_twice() -> None:
    """`done` repeats the full answer for clients that ignore deltas. The TUI
    uses this flag to know it already showed it — the earlier version compared
    text and got it wrong once a trailing newline emptied the pending tail."""
    state = LiveState()
    state.start_turn()
    assert state.streamed_any is False
    state.consume({"type": "assistant_delta", "text": "the answer\n"})
    assert state.streamed_any is True
    state.end_turn()
    assert state.streamed_any is True  # survives end_turn; reset by start_turn
    state.start_turn()
    assert state.streamed_any is False


def test_whitespace_only_deltas_do_not_count_as_streaming() -> None:
    """The graph emits a bare newline to close each model call's block; that
    alone must not suppress the markdown fallback."""
    state = LiveState()
    state.start_turn()
    state.consume({"type": "assistant_delta", "text": "\n"})
    assert state.streamed_any is False


def test_subagent_narration_stays_out_of_the_transcript() -> None:
    """Two agents' prose interleaved is noise; their tool steps are the signal."""
    state = LiveState()
    assert state.consume(
        {"type": "assistant_delta", "text": "chatter", "agent_id": "sub-1"}
    ) == []
    assert state.pending_text == ""


def test_reasoning_is_shown_live_and_never_promoted() -> None:
    state = LiveState()
    state.consume({"type": "step", "id": "t", "label": "Thinking", "status": "running"})
    assert state.consume(
        {"type": "assistant_delta", "text": "let me think", "channel": "reasoning"}
    ) == []
    assert any("let me think" in line for line in state.lines())
    state.end_turn()
    assert state.reasoning_tail == ""


def test_concurrent_subagents_get_their_own_blocks() -> None:
    state = LiveState()
    for i in (1, 2):
        state.consume(_step(
            id=f"sub-{i}", label=f"research: q{i}", status="running",
            tool=None, agent_id=f"sub-{i}", agent_label="research", detail=f"q{i}",
        ))
    assert len(state.subagents) == 2
    lines = "\n".join(state.lines())
    assert "q1" in lines and "q2" in lines

    # A step from inside one sub-agent counts against that agent, not the top
    # level — otherwise concurrent agents interleave into one unreadable list.
    state.consume(_step(id="x", parent_step_id="sub-1", status="done"))
    assert state.subagents["sub-1"].tools == 1
    assert state.subagents["sub-2"].tools == 0
    assert not state.steps

    promoted = state.consume(_step(
        id="sub-1", label="research", status="done", tool="research",
        agent_id="sub-1", agent_label="research", elapsed_ms=18200,
    ))
    assert len(promoted) == 1 and "research" in promoted[0]
    assert "sub-1" not in state.subagents


def test_usage_updates_the_stats_and_the_owning_subagent() -> None:
    state = LiveState()
    state.consume(_step(
        id="sub-1", label="research: q", status="running", tool=None,
        agent_id="sub-1", agent_label="research", detail="q",
    ))
    state.consume({
        "type": "usage", "model": "deepseek-chat", "role": "flash",
        "input_tokens": 900, "output_tokens": 100,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.001,
        "agent_id": "sub-1",
    })
    assert state.subagents["sub-1"].tokens == 1000
    assert state.stats.turn_tokens == 1000
    assert state.stats.cost == 0.001


def test_one_unpriced_call_stops_the_bar_claiming_a_total() -> None:
    state = LiveState()
    state.consume({"type": "usage", "model": "x", "input_tokens": 5, "output_tokens": 1,
                   "cache_read_tokens": 0, "cache_write_tokens": 0})
    assert state.stats.cost is None


def test_todo_events_replace_the_whole_list() -> None:
    state = LiveState()
    state.consume({"type": "todo", "items": [{"id": "1", "text": "a", "status": "pending"}]})
    state.consume({"type": "todo", "items": []})
    assert state.todos == []


def test_ask_flushes_pending_text_and_shows_the_plan() -> None:
    state = LiveState()
    state.consume({"type": "assistant_delta", "text": "Here's my plan:"})
    promoted = state.consume({
        "type": "ask", "id": "a1", "kind": "plan", "question": "Go ahead?",
        "options": [], "multi_select": False, "plan": "1. Do it",
    })
    assert promoted[0] == "Here's my plan:"
    assert any("Do it" in line for line in promoted)
    assert state.ask is not None
    assert not state.busy  # the spinner stops while we wait on the user
    assert any("Go ahead?" in line for line in state.lines())


def test_cancel_clears_everything_in_flight() -> None:
    state = LiveState()
    state.consume({"type": "step", "id": "t", "label": "Thinking", "status": "running"})
    state.consume(_step())
    state.consume({"type": "assistant_delta", "text": "partial"})
    promoted = state.cancel()
    assert "partial" in promoted
    assert any("cancelled" in line for line in promoted)
    assert not state.busy and not state.steps and state.lines() == []


def test_unknown_event_types_are_ignored() -> None:
    """live_frame, ui_action, host_action and anything added later must not
    break the display."""
    state = LiveState()
    assert state.consume({"type": "live_frame", "data": "..."}) == []
    assert state.consume({"type": "something_new"}) == []


# --- wrapping: the invariant scrolling depends on ----------------------------

def test_wrap_produces_rows_that_fit() -> None:
    """One stored row must be exactly one rendered row. Everything about
    scrolling follows from that — when it failed, `vertical_scroll` counted
    logical lines while the window counted rendered rows."""
    line = "  " + " ".join(f"word{i}" for i in range(60))
    rows = render.wrap_ansi(line, 40)
    assert len(rows) > 1
    for row in rows:
        assert visible_len(row) <= 40, repr(row)


def test_wrap_preserves_the_visible_text() -> None:
    line = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    rows = render.wrap_ansi(line, 20)
    joined = " ".join(" ".join(r.split()) for r in rows)
    assert joined.split() == line.split()


def test_wrap_carries_styling_across_the_break() -> None:
    """A coloured span that spans a wrap must not bleed past its end or lose
    its colour on the continuation row."""
    line = f"{render.DIM}{'x' * 60}{render.RESET}"
    rows = render.wrap_ansi(line, 20)
    assert len(rows) == 3
    for row in rows:
        assert render.DIM in row
        assert row.endswith(render.RESET)


def test_wrap_keeps_the_indent_on_continuation_rows() -> None:
    rows = render.wrap_ansi("    " + "y " * 60, 30)
    assert all(visible_len(r) <= 30 for r in rows)
    assert all(r.startswith("    ") for r in rows[1:])


def test_wrap_breaks_at_a_space_when_one_is_near() -> None:
    rows = render.wrap_ansi("hello world " * 10, 24)
    # A word should not be split when a space was available just behind.
    assert not any(r.rstrip().endswith(("hel", "wor", "worl")) for r in rows)


def test_wrap_splits_an_unbreakable_run() -> None:
    """No space to break at — a long path or hash still has to fit."""
    rows = render.wrap_ansi("z" * 100, 20)
    assert len(rows) == 5
    assert all(visible_len(r) == 20 for r in rows)


def test_short_lines_pass_through_untouched() -> None:
    assert render.wrap_ansi("short", 40) == ["short"]
    assert render.wrap_ansi("", 40) == [""]


def test_wrap_all_flattens() -> None:
    rows = render.wrap_all(["a" * 30, "b"], 10)
    assert rows[-1] == "b"
    assert len(rows) == 4


def test_skill_steps_show_which_skill() -> None:
    """`✓ read_skill` on its own says nothing — every other tool line carries
    its argument, and a transcript of six identical lines is unreadable."""
    from daimon_agent.graph import _call_detail

    assert _call_detail({"name": "pdf", "file": "forms.md"}) == "pdf"
    assert _call_detail({"name": "changelog", "description": "d", "content": "c"}) == "changelog"
    # A path still wins over a name where both exist.
    assert _call_detail({"path": "a.txt", "name": "x"}) == "a.txt"


def test_a_secret_ask_is_flagged() -> None:
    """The TUI masks both the typed input and the transcript echo off this."""
    plain = AskState(event={"question": "Which?", "options": [{"label": "A"}]})
    assert plain.secret is False
    assert plain.freeform is False

    key = AskState(event={"question": "Paste your key", "options": [], "secret": True})
    assert key.secret is True
    # No options means nothing to pick, so typing starts immediately.
    assert key.freeform is True


def test_an_ask_with_no_options_says_to_type() -> None:
    lines = render.ask_lines(
        AskState(event={"question": "Paste your key", "options": []})
    )
    assert "type your answer" in lines[-1]
    assert "1-9 pick" not in lines[-1]
