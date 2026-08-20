"""Tiny ANSI helpers — no deps, no state.

The decision to emit escape codes is made *per call* against the target
stream's ``isatty()``, never at import time: pytest's fd-level capture means
``sys.stdout`` during import is the real terminal, so a module-level check
would leak ANSI into captured output, and piped invocations (``echo hi |
daimon-chat``) must come out plain. Passing ``stream=None`` targets
``sys.stdout`` at call time — every helper re-checks, so redirecting stdout
between calls is always honored.
"""

from __future__ import annotations

import sys

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
BLUE = "\x1b[38;2;79;141;255m"  # #4f8dff — Daimon accent


def _is_tty(stream: object | None) -> bool:
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty and isatty())


def paint(text: str, code: str, stream: object | None = None) -> str:
    return f"{code}{text}{RESET}" if _is_tty(stream) else text


def bold(text: str, stream: object | None = None) -> str:
    return paint(text, BOLD, stream)


def dim(text: str, stream: object | None = None) -> str:
    return paint(text, DIM, stream)


def red(text: str, stream: object | None = None) -> str:
    return paint(text, RED, stream)


def green(text: str, stream: object | None = None) -> str:
    return paint(text, GREEN, stream)


def magenta(text: str, stream: object | None = None) -> str:
    return paint(text, MAGENTA, stream)


def cyan(text: str, stream: object | None = None) -> str:
    return paint(text, CYAN, stream)


def blue(text: str, stream: object | None = None) -> str:
    return paint(text, BLUE, stream)


def erase_line(stream: object | None = None) -> str:
    """Carriage return + clear-to-end-of-line — the prefix for every stderr
    line that must replace the spinner in place. Empty when the target
    stream isn't a tty."""
    return "\r\x1b[2K" if _is_tty(stream) else ""
