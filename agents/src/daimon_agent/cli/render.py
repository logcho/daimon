"""Turning agent events into lines of text.

Everything here is pure: events and state in, ANSI strings out. No terminal, no
prompt_toolkit, no I/O — which is what makes the display testable at all. The
TUI decides *where* a line goes (permanent scrollback vs. the live region);
this module only decides what it says.

Two vocabularies share the file. Live lines are redrawn every frame until the
thing they describe finishes, at which point the TUI promotes a final version
into the transcript. Transcript lines are settled rather than immutable: the
TUI owns that buffer and re-renders it every frame, which is what lets a
collapsed run (`run_line`) expand in place long after it was promoted.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..usage import format_tokens

# --- palette ----------------------------------------------------------------
# Raw ANSI rather than prompt_toolkit styles: these strings pass through
# `ANSI()` for the live region and through `print_formatted_text` for the
# transcript, and raw escapes are the one format both accept unchanged.

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
BLUE = "\x1b[38;2;79;141;255m"  # #4f8dff — the daimon accent

# --- spinner ----------------------------------------------------------------

SPINNER_FRAMES = ["·", "✢", "*", "✶", "✻", "✽"]
SPINNER_INTERVAL = 0.12  # seconds per frame

THINKING_VERBS = [
    "Thinking",
    "Ruminating",
    "Discombobulating",
    "Consulting the oracles",
    "Exploring",
    "Chasing will-o'-wisps",
    "Fiddling with knobs",
    "Flibbertigibbeting",
]
VERB_INTERVAL = 4.0  # seconds per verb


def spinner(frame_idx: int) -> str:
    return SPINNER_FRAMES[frame_idx % len(SPINNER_FRAMES)]


def verb(idx: int) -> str:
    return THINKING_VERBS[idx % len(THINKING_VERBS)]


# --- formatting helpers ------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """Elapsed time at a resolution a human reads at a glance: tenths under a
    minute, then m/s, then h/m."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def fmt_cost(cost: float | None) -> str:
    """Money, or nothing at all.

    Both `None` (unpriced model) and exactly zero (nothing has happened yet)
    render as the empty string. A literal "$0.0000" sitting in the status bar
    before the first turn reads as a measurement rather than the absence of
    one.
    """
    if not cost:
        return ""
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- steps -------------------------------------------------------------------

_MARKS = {
    "running": f"{BLUE}→{RESET}",
    "done": f"{DIM}✓{RESET}",
    "error": f"{RED}✗{RESET}",
    "pending": f"{DIM}·{RESET}",
}


def step_line(
    name: str,
    status: str,
    *,
    detail: str | None = None,
    elapsed_ms: int | None = None,
    indent: int = 2,
    width: int = 80,
) -> str:
    """One tool-call line: `  ✓ read_file  src/graph.py  (0.4s)`.

    Detail is what makes a transcript readable after the fact — twelve
    `read_file` lines say nothing; twelve paths say what the agent looked at.
    """
    mark = _MARKS.get(status, _MARKS["pending"])
    line = f"{' ' * indent}{mark} {MAGENTA}{name}{RESET}"
    tail = ""
    if elapsed_ms is not None and elapsed_ms >= 100:
        tail = f"  {DIM}({fmt_duration(elapsed_ms / 1000)}){RESET}"
    if detail:
        # Budget the detail against the terminal so a long path doesn't wrap
        # into a second line and break the one-line-per-step rhythm.
        budget = max(width - indent - len(name) - len(_strip_ansi(tail)) - 6, 12)
        line += f"  {DIM}{truncate(detail, budget)}{RESET}"
    return line + tail


#: Tool → the category it counts toward in a collapsed run's summary.
#:
#: Kept deliberately coarse. "9 read · 3 edit · 2 shell" tells you the shape of
#: what happened; a breakdown by individual tool name would just be the step
#: list again with the useful part (what each one was called *with*) removed.
#:
#: Mirrors `CATEGORY` in `app/src/components/StepSummary.tsx` — the two clients
#: show the same turn and should describe it the same way. Add a tool to both.
CATEGORY = {
    "read_file": "read",
    "grep_files": "read",
    "glob_files": "read",
    "list_directory": "read",
    "read_note": "read",
    "list_notes": "read",
    "read_skill": "read",
    "list_skills": "read",
    "recall": "read",

    "write_file": "edit",
    "edit_file": "edit",
    "delete_file": "edit",
    "move_file": "edit",
    "mkdir": "edit",
    "create_note": "edit",
    "append_to_note": "edit",
    "move_note": "edit",
    "save_skill": "edit",

    "run_shell": "shell",
    "kernel_execute": "shell",
    "run_tests": "shell",
    "check_code": "shell",
    "debug": "shell",

    "web_search": "web",
    "web_fetch": "web",
    "open_url": "web",
    "read_page": "web",
    "extract_text": "web",
    "find_skills": "web",
}

#: The order categories appear in, so two runs are comparable at a glance.
ORDER = ["read", "edit", "shell", "web"]

#: Most rows shown inside an expanded fold. Past a certain length nobody is
#: reading anyway, and every row costs a re-wrap.
MAX_EXPANDED = 30


def category_parts(names: list[str]) -> list[str]:
    """The category tally as display parts — `["9 read", "3 edit"]`, in ORDER."""
    counts: dict[str, int] = {}
    for name in names:
        category = CATEGORY.get(name)
        if category:
            counts[category] = counts.get(category, 0) + 1
    return [f"{counts[c]} {c}" for c in ORDER if c in counts]


@dataclass
class Fold:
    """A promoted block that shows one line until the user opens it.

    Pure data: the two summary variants are pre-rendered, so toggling is a flag
    flip and `lines()` never has to re-derive anything. `tui.Transcript` stores
    these alongside plain strings and rebuilds its wrapped rows when the flag
    changes.
    """

    closed: str
    open: str
    detail: list[str] = field(default_factory=list)
    expanded: bool = False

    def lines(self) -> list[str]:
        return [self.open, *self.detail] if self.expanded else [self.closed]


def run_line(
    count: int,
    *,
    categories: list[str] | None = None,
    failed: int = 0,
    elapsed_ms: int | None = None,
    expanded: bool = False,
    indent: int = 2,
    width: int = 80,
) -> str:
    """The collapsed row standing in for a run of tool calls.

    `  ▸ 12 tools · 9 read · 3 edit  (4.1s)`

    A run is the parent agent's own work between two things worth seeing
    separately — a sub-agent, a line of the answer, the end of the turn. Twelve
    `read_file` rows in permanent scrollback say nothing that this one doesn't;
    the paths are still there, one keypress away.

    A failure is never folded away silently: a run that contains one says so
    here, where the row is visible without expanding it.
    """
    chevron = f"{DIM}{'▾' if expanded else '▸'}{RESET}"
    parts = [f"{count} tool{'s' if count != 1 else ''}", *(categories or [])]
    line = f"{' ' * indent}{chevron} {DIM}{' · '.join(parts)}{RESET}"
    if failed:
        line += f"  {RED}✗ {failed} failed{RESET}"
    if elapsed_ms is not None and elapsed_ms >= 100:
        line += f"  {DIM}({fmt_duration(elapsed_ms / 1000)}){RESET}"
    return line


def run_active_line(
    count: int,
    name: str | None = None,
    detail: str | None = None,
    *,
    failed: int = 0,
    indent: int = 2,
    width: int = 80,
) -> str:
    """The live rolling row: `  → 3 tools · read_file  app/src/App.tsx`.

    Counts the whole run so far, not just what is in flight, and names the most
    recent call. It stands in for finished calls too — they leave the live
    region before the run is promoted, and without this they would briefly be
    nowhere at all.
    """
    head = f"{count} tool{'s' if count != 1 else ''}"
    line = f"{' ' * indent}{_MARKS['running']} {DIM}{head}{RESET}"
    if name:
        line += f" {DIM}·{RESET} {MAGENTA}{name}{RESET}"
        if detail:
            budget = max(width - indent - len(head) - len(name) - 10, 12)
            line += f"  {DIM}{truncate(detail, budget)}{RESET}"
    if failed:
        line += f"  {RED}✗ {failed} failed{RESET}"
    return line


def subagent_line(
    label: str,
    query: str,
    status: str,
    *,
    tools: int = 0,
    tokens: int = 0,
    elapsed_ms: int | None = None,
    expanded: bool | None = None,
    width: int = 80,
) -> str:
    """A sub-agent's own line, indented under the spawn.

    `expanded` is None for a line that cannot be opened — the running one in
    the live region — and a bool for the promoted line, which carries a chevron
    alongside its status mark. The mark stays: a sub-agent that failed should
    say so whether or not its calls are showing.
    """
    mark = _MARKS.get(status, _MARKS["pending"])
    chevron = "" if expanded is None else f"{DIM}{'▾' if expanded else '▸'}{RESET} "
    stats: list[str] = []
    if tools:
        stats.append(f"{tools} tool{'s' if tools != 1 else ''}")
    if tokens:
        stats.append(format_tokens(tokens))
    if elapsed_ms:
        stats.append(fmt_duration(elapsed_ms / 1000))
    suffix = f"  {DIM}{' · '.join(stats)}{RESET}" if stats else ""
    budget = max(width - 24 - len(_strip_ansi(suffix)), 16)
    return (
        f"    {chevron}{mark} {CYAN}{label}{RESET} "
        f"{DIM}{truncate(query, budget)}{RESET}{suffix}"
    )


def _strip_ansi(text: str) -> str:
    """Visible width of a styled string — used only for layout budgets, so a
    simple state machine beats pulling in a regex for it."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\x1b":
            while i < len(text) and text[i] not in "m":
                i += 1
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def visible_len(text: str) -> int:
    return len(_strip_ansi(text))


_SGR = re.compile(r"\x1b\[[0-9;]*m")
#: How far back to look for a space when a row has to break.
_WORD_BREAK_WINDOW = 24


def wrap_ansi(line: str, width: int) -> list[str]:
    """Wrap one styled line into rows of at most `width` visible columns.

    The transcript stores pre-wrapped rows so that one stored row is exactly
    one rendered row. That equality is the whole scrolling design: with it,
    `vertical_scroll` is an exact row offset and the arithmetic cannot drift.
    The previous version stored logical lines and let the terminal wrap them,
    so scroll offsets counted one thing while the window measured another —
    which is why scrolling didn't work at all.

    Styling survives the break: the active SGR codes are closed at the end of
    a row and reopened at the start of the next, so a coloured span that spans
    a wrap doesn't bleed or vanish. Continuation rows keep the original line's
    indentation, and breaks prefer a nearby space over splitting a word.
    """
    if width <= 1 or visible_len(line) <= width:
        return [line]

    plain = _strip_ansi(line)
    indent = " " * min(len(plain) - len(plain.lstrip(" ")), max(width - 8, 0))

    rows: list[str] = []
    current: list[str] = []
    active = ""  # SGR codes in effect right now
    column = 0
    first = True
    index = 0

    def flush() -> None:
        nonlocal current, column, first
        rows.append("".join(current) + (RESET if active else ""))
        current = [indent, active] if active else [indent]
        column = len(indent)
        first = False

    while index < len(line):
        match = _SGR.match(line, index)
        if match:
            code = match.group()
            current.append(code)
            active = "" if code in ("\x1b[0m", "\x1b[m") else active + code
            index = match.end()
            continue

        limit = width if first else width
        if column >= limit:
            # Prefer breaking at a space just behind us over splitting a word.
            text_so_far = "".join(current)
            cut = text_so_far.rfind(" ")
            if cut > 0 and len(_strip_ansi(text_so_far[cut:])) <= _WORD_BREAK_WINDOW:
                carry = text_so_far[cut + 1 :]
                current = [text_so_far[:cut]]
                flush()
                current.append(carry)
                column += len(_strip_ansi(carry))
            else:
                flush()

        current.append(line[index])
        column += 1
        index += 1

    rows.append("".join(current))
    return rows


def wrap_all(lines: list[str], width: int) -> list[str]:
    return [row for line in lines for row in wrap_ansi(line, width)]


# --- todo list ---------------------------------------------------------------

_TODO_MARKS = {
    "done": f"{GREEN}✓{RESET}",
    "in_progress": f"{BLUE}▸{RESET}",
    "pending": f"{DIM}○{RESET}",
}


def todo_lines(items: list[dict], *, width: int = 80) -> list[str]:
    """The checklist as the user sees it. The active item is undimmed and the
    finished ones are struck through, so the eye lands on what's happening now."""
    if not items:
        return []
    done = sum(1 for i in items if i.get("status") == "done")
    lines = [f"  {DIM}todos{RESET}  {DIM}{done}/{len(items)}{RESET}"]
    for item in items:
        status = item.get("status", "pending")
        mark = _TODO_MARKS.get(status, _TODO_MARKS["pending"])
        text = truncate(str(item.get("text", "")), max(width - 8, 20))
        if status == "done":
            body = f"{DIM}\x1b[9m{text}\x1b[29m{RESET}"
        elif status == "in_progress":
            body = f"{text}"
        else:
            body = f"{DIM}{text}{RESET}"
        lines.append(f"    {mark} {body}")
    return lines


# --- the turn's live header --------------------------------------------------

@dataclass
class TurnStats:
    """What the status line and the thinking header report. Counters are
    cumulative for the session; `turn_*` reset each turn."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = 0.0
    context_tokens: int = 0
    last_elapsed: float | None = None
    turn_tokens: int = 0
    #: Set once any call used a model with no known price — from then on the
    #: running total is incomplete and showing it would be a lie.
    cost_unknown: bool = False

    def add_usage(self, event: dict) -> None:
        self.input_tokens += int(event.get("input_tokens", 0))
        self.output_tokens += int(event.get("output_tokens", 0))
        self.cache_read_tokens += int(event.get("cache_read_tokens", 0))
        self.turn_tokens += int(event.get("input_tokens", 0)) + int(
            event.get("output_tokens", 0)
        )
        if event.get("role") == "pro":
            self.context_tokens = int(event.get("input_tokens", 0))
        if "cost_usd" in event:
            if self.cost_usd is not None:
                self.cost_usd += float(event["cost_usd"])
        else:
            self.cost_unknown = True

    @property
    def cost(self) -> float | None:
        return None if self.cost_unknown else self.cost_usd

    def start_turn(self) -> None:
        self.turn_tokens = 0


def thinking_line(
    *,
    frame_idx: int,
    verb_idx: int,
    started: float,
    turn_tokens: int = 0,
    now: float | None = None,
) -> str:
    """The live 'still working' line: spinner, a cycling verb, elapsed time,
    and this turn's token count.

    The elapsed clock is the point. A silent agent and a wedged one look
    identical without it; with it, "42.1s" is information rather than anxiety.
    """
    elapsed = (now if now is not None else time.monotonic()) - started
    parts = [fmt_duration(elapsed)]
    if turn_tokens:
        parts.append(f"{format_tokens(turn_tokens)} tokens")
    meta = f"{DIM}({' · '.join(parts)}){RESET}"
    return f"  {BLUE}{spinner(frame_idx)}{RESET} {verb(verb_idx)}… {meta} {DIM}esc to cancel{RESET}"


def status_line(
    session: str,
    model: str,
    stats: TurnStats,
    *,
    context_window: int = 128000,
    mode: str = "normal",
) -> str:
    """The persistent bottom bar."""
    parts = [session, model]
    if mode == "plan":
        parts.append(f"{YELLOW}plan mode{RESET}{DIM}")
    if stats.turns:
        parts.append(f"{stats.turns} turn{'s' if stats.turns != 1 else ''}")
    if stats.last_elapsed is not None:
        parts.append(fmt_duration(stats.last_elapsed))
    if stats.input_tokens or stats.output_tokens:
        parts.append(
            f"↑{format_tokens(stats.input_tokens)} ↓{format_tokens(stats.output_tokens)}"
        )
    cost = fmt_cost(stats.cost)
    if cost:
        parts.append(cost)
    if stats.context_tokens and context_window:
        pct = min(int(stats.context_tokens / context_window * 100), 100)
        parts.append(f"ctx {pct}%")
    return f"{DIM}{' · '.join(parts)}{RESET}"


# --- transcript blocks -------------------------------------------------------

def user_line(text: str) -> list[str]:
    """The user's own message, echoed into the transcript so the scrollback
    reads as a conversation rather than a log of replies."""
    lines = text.splitlines() or [""]
    return ["", f"{BLUE}>{RESET} {BOLD}{lines[0]}{RESET}"] + [
        f"  {BOLD}{line}{RESET}" for line in lines[1:]
    ]


def turn_footer(stats: TurnStats, elapsed: float) -> str:
    """The one-line receipt closing a turn."""
    parts = [fmt_duration(elapsed)]
    if stats.turn_tokens:
        parts.append(f"{format_tokens(stats.turn_tokens)} tokens")
    cost = fmt_cost(stats.cost)
    if cost:
        parts.append(cost)
    return f"{DIM}  ─── {' · '.join(parts)}{RESET}"


def error_lines(message: str) -> list[str]:
    return ["", f"  {RED}✖{RESET} {message}"]


def continuation_line(event: dict) -> str:
    """The turn ran past the graph's step cap and is carrying on. Shown so a
    long unattended run reads as progress rather than as a stall."""
    parts = [f"step {int(event.get('steps', 0))}"]
    tokens = int(event.get("tokens", 0))
    if tokens:
        parts.append(f"{format_tokens(tokens)} tokens")
    return f"  {BLUE}↻{RESET} {DIM}continuing · {' · '.join(parts)}{RESET}"


def retry_line(event: dict) -> str:
    """A model call that died mid-stream and is being restarted."""
    attempt = int(event.get("attempt", 2))
    total = int(event.get("max_attempts", 3))
    return (
        f"  {BLUE}↺{RESET} {DIM}connection lost — retrying "
        f"({attempt}/{total}){RESET}"
    )


def compaction_line(event: dict) -> str:
    before = int(event.get("before_tokens", 0))
    after = int(event.get("after_tokens", 0))
    return (
        f"  {DIM}⌘ compacted context "
        f"{format_tokens(before)} → {format_tokens(after)} tokens{RESET}"
    )


# --- the ask / plan prompt ---------------------------------------------------

@dataclass
class AskState:
    """A pending question and where the cursor sits in it."""

    event: dict = field(default_factory=dict)
    cursor: int = 0
    selected: set[int] = field(default_factory=set)
    #: True while the user is typing a free-text answer instead of picking.
    #: Starts true for a question with no options — there is nothing to pick.
    freeform: bool = False

    def __post_init__(self) -> None:
        if not self.options:
            self.freeform = True

    @property
    def options(self) -> list[dict]:
        return list(self.event.get("options") or [])

    @property
    def multi(self) -> bool:
        return bool(self.event.get("multi_select"))

    @property
    def secret(self) -> bool:
        """Whether the answer should be masked as it's typed — an API key
        shouldn't sit on screen in plain text."""
        return bool(self.event.get("secret"))


def ask_lines(state: AskState, *, width: int = 80) -> list[str]:
    """Render the question and its options.

    The plan body goes into the transcript separately (it is long, and it
    shouldn't redraw on every keystroke); what's here is only the part that
    changes as the user moves the cursor.
    """
    event = state.event
    lines: list[str] = [""]
    header = event.get("header")
    if header:
        lines.append(f"  {DIM}{header}{RESET}")
    question = str(event.get("question", "")).strip()
    if question:
        lines.append(f"  {BOLD}{question}{RESET}")
    lines.append("")

    for i, option in enumerate(state.options):
        active = i == state.cursor
        if state.multi:
            box = "◉" if i in state.selected else "○"
            marker = f"{BLUE}{box}{RESET}" if active else f"{DIM}{box}{RESET}"
        else:
            marker = f"{BLUE}❯{RESET}" if active else " "
        label = str(option.get("label", ""))
        label_text = f"{BOLD}{label}{RESET}" if active else label
        line = f"  {marker} {DIM}{i + 1}.{RESET} {label_text}"
        description = str(option.get("description", "")).strip()
        if description:
            line += f"  {DIM}{truncate(description, max(width - len(label) - 16, 16))}{RESET}"
        lines.append(line)

    lines.append("")
    if not state.options:
        # Nothing to pick — the wizard's key step, for instance. Offering
        # "1-9 pick" against an empty list is just noise.
        lines.append(f"  {DIM}type your answer · enter confirm · esc skip{RESET}")
        return lines
    hint = "1-9 pick · ↑↓ move · space toggle · enter confirm" if state.multi else (
        "1-9 pick · ↑↓ move · enter confirm"
    )
    lines.append(f"  {DIM}{hint} · e write your own · esc skip{RESET}")
    return lines


def plan_block(plan: str) -> list[str]:
    """The plan body, for the transcript. Rendered as-is with a rule above and
    below so it reads as a document rather than more agent chatter."""
    body = plan.strip()
    if not body:
        return []
    return ["", f"  {DIM}── plan{RESET}", "", *(f"  {line}" for line in body.splitlines()), ""]
