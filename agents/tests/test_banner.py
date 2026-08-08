"""Banner rendering tests.

The simplified banner (default) is a 3-line wordmark + CWD + hint.
The full ASCII art (BANNER_LINES, BANNER) is preserved but opt-in via
``full_banner_lines()``.

The ansi.py contract under test: every styling decision is made per-call
against the target stream's isatty(). pytest's capsys captures through a
CaptureIO whose isatty() is always False, so ``print_banner`` output comes
out plain with no \\x1b escapes.  ``banner_lines()`` always emits ANSI
(built for the TUI raw-ANSI pipeline), so it's tested against raw escape
codes.
"""

from __future__ import annotations

import io

from daimon_agent.banner import (
    BANNER,
    BANNER_LINES,
    banner_lines,
    full_banner_lines,
    print_banner,
)


# --- Full ASCII art (preserved) -------------------------------------------

def test_banner_is_non_empty():
    """The ASCII art must be a non-empty string with 15 lines."""
    assert BANNER
    lines = BANNER.splitlines()
    assert len(lines) == 15


def test_banner_contains_block_chars():
    """The art uses block-drawing characters (█ and ▓)."""
    assert "█" in BANNER
    assert "▓" in BANNER


def test_banner_lines_structure():
    """All 15 entries are dim; eyes (▓▓) are embedded in line 4's text."""
    assert len(BANNER_LINES) == 15
    for i, (style, text) in enumerate(BANNER_LINES):
        assert style == "dim", f"line {i} expected dim, got {style}"
    assert "▓▓" in BANNER_LINES[4][1]


def test_full_banner_lines_emits_blue_for_eyes():
    """full_banner_lines() wraps ▓ characters in the blue ANSI code."""
    lines = full_banner_lines()
    eye_line = lines[4]
    assert "\x1b[38;2;79;141;255m" in eye_line  # BLUE constant
    # Blue should appear exactly twice (one per ▓)
    assert eye_line.count("\x1b[38;2;79;141;255m") == 2


# --- Simplified banner (default) ------------------------------------------

def test_banner_lines_is_simplified():
    """Default banner_lines() returns 3 lines (wordmark, hint, blank)."""
    lines = banner_lines()
    assert len(lines) == 3
    assert lines[2] == ""  # trailing blank
    assert any("daimon" in line for line in lines)


def test_banner_lines_has_hint():
    lines = banner_lines()
    hint_line = lines[1]
    assert "Alt+Enter" in hint_line
    assert "/help" in hint_line


def test_print_banner_plain_when_not_tty():
    """Under capture (non-tty), print_banner outputs plain ASCII — no
    escape codes, and the wordmark contains 'daimon'."""
    buf = io.StringIO()
    # StringIO is never a tty
    print_banner(buf)
    out = buf.getvalue()
    assert "\x1b[" not in out
    assert "daimon" in out
    assert "Alt+Enter" in out


def test_print_banner_with_session_name():
    """The session name appears in the hint line when provided."""
    buf = io.StringIO()
    print_banner(buf, session_name="work")
    out = buf.getvalue()
    assert "[work]" in out
