"""Workspace-confined file tools — the harness's Write/Read/Glob/Grep,
narrowed to the workspace. Every path goes through Confinement (resolve-then-
compare; `notes/../../.ssh/id_rsa` cannot escape), so the model gets a plain
error ToolMessage for anything outside instead of touching it.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from ..workspace import Confinement, OutsideWorkspace

MAX_READ_CHARS = 40_000

#: Lines returned by one read_file when no explicit limit is given. A file
#: longer than this is paged rather than truncated, so the agent knows there is
#: more and how to ask for it.
DEFAULT_READ_LINES = 2_000

#: Line-number gutter. The arrow is deliberately not a character that appears in
#: source, so `_strip_gutter` can undo it without ambiguity when the agent pastes
#: numbered output back into edit_file.
_GUTTER = re.compile(r"^\s*\d+→", re.MULTILINE)


def _number(lines: list[str], first: int) -> str:
    width = max(len(str(first + len(lines) - 1)), 4)
    return "\n".join(f"{first + i:>{width}}→{line}" for i, line in enumerate(lines))


def _strip_gutter(text: str) -> str:
    """Remove read_file's line-number gutter from text the agent quoted back.

    Numbering a read makes a file navigable, but it also means the most natural
    thing for a model to do — copy the lines it just saw into edit_file — would
    never match. Undoing it here costs one regex and removes the whole footgun.
    """
    return _GUTTER.sub("", text)


def _as_int(raw: str, default: int) -> int:
    """Flat string args (the DeepSeek tool-calling mitigation) mean every number
    arrives as text, and sometimes as an empty one."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# --- the read registry -------------------------------------------------------
#
# Editing a file the agent has never read means matching `old_text` it guessed,
# and overwriting one it has never read means discarding content it never saw.
# The system prompt has always said "read first"; this is that rule made
# structural, because by the time you notice a model ignored it, the file is
# already gone.

#: (session_id, resolved path) → (st_mtime_ns, st_size) at the agent's last
#: read. Module-level and process-lived, like repl.py's kernels.
_reads: dict[tuple[str, str], tuple[int, int]] = {}

NEVER_READ = (
    'Refusing to edit "{path}": you have not read it in this session, so the '
    "old_text you are matching against is a guess. Call read_file first and edit "
    "against what is actually there."
)

STALE_READ = (
    '"{path}" changed on disk after you read it — something else wrote to it '
    "(a shell command, the kernel, or the user). Editing against your stale copy "
    "would silently discard that change. Read it again, then redo the edit."
)

BLIND_OVERWRITE = (
    'Refusing to overwrite "{path}": it already exists and you have not read it '
    "in this session, so you don\'t know what you would be discarding. Read it "
    "first — write_file goes through once you have. For a targeted change, "
    "edit_file is the better tool."
)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _remember(session_id: str, path: Path) -> None:
    stamp = _stamp(path)
    if stamp is not None:
        _reads[(session_id, str(path))] = stamp


def _check_seen(session_id: str, path: Path, file_path: str, missing: str) -> str | None:
    """None when the agent may write to `path`, else the reason it may not."""
    seen = _reads.get((session_id, str(path)))
    if seen is None:
        return missing.format(path=file_path)
    if seen != _stamp(path):
        return STALE_READ.format(path=file_path)
    return None


def forget_reads(session_id: str | None = None) -> None:
    """Drop remembered reads — one session's, or every one. Called when a
    session ends; also what tests use to start from a clean slate."""
    if session_id is None:
        _reads.clear()
        return
    for key in [k for k in _reads if k[0] == session_id]:
        del _reads[key]


def read_file(
    conf: Confinement,
    file_path: str,
    offset: str = "",
    limit: str = "",
    session_id: str = "",
) -> str:
    """Read a workspace file, numbered and pageable.

    `offset` is a 1-indexed starting line and `limit` a line count, so a file
    too big for one read can be walked rather than silently cut off at 40k
    characters with no indication of what came after.
    """
    path = conf.resolve(file_path)
    if not path.is_file():
        return f'File "{file_path}" does not exist.'
    content = path.read_text(encoding="utf-8", errors="replace")
    # Record before returning: what the agent is about to see is the version
    # a later edit will be checked against.
    _remember(session_id, path)

    lines = content.splitlines()
    start = max(_as_int(offset, 1), 1)
    count = max(_as_int(limit, DEFAULT_READ_LINES), 1)
    window = lines[start - 1 : start - 1 + count]

    if not window:
        return (
            f'"{file_path}" has {len(lines)} lines — offset {start} is past the end.'
            if lines
            else f'"{file_path}" is empty.'
        )

    body = _number(window, start)
    notes: list[str] = []
    if len(body) > MAX_READ_CHARS:
        # Cut on a line boundary so the last line shown is a whole one.
        body = body[:MAX_READ_CHARS].rsplit("\n", 1)[0]
        shown = body.count("\n") + 1
        notes.append(
            f"truncated at {MAX_READ_CHARS:,} characters after line "
            f"{start + shown - 1}"
        )
    else:
        shown = len(window)

    last = start + shown - 1
    if last < len(lines):
        notes.append(
            f"showing lines {start}-{last} of {len(lines)} — "
            f"read_file with offset={last + 1} for the rest"
        )
    if notes:
        body += "\n\n… (" + "; ".join(notes) + ")"
    return body


def write_file(
    conf: Confinement, file_path: str, content: str, session_id: str = ""
) -> str:
    path = conf.resolve(file_path)
    if path.exists():
        refusal = _check_seen(session_id, path, file_path, BLIND_OVERWRITE)
        if refusal is not None:
            return refusal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _remember(session_id, path)
    return f'Wrote {len(content)} characters to "{file_path}".'


def edit_file(
    conf: Confinement, file_path: str, old_text: str, new_text: str, session_id: str = ""
) -> str:
    path = conf.resolve(file_path)
    if not path.is_file():
        return f'File "{file_path}" does not exist.'
    refusal = _check_seen(session_id, path, file_path, NEVER_READ)
    if refusal is not None:
        return refusal

    content = path.read_text(encoding="utf-8")
    old, new = _strip_gutter(old_text), _strip_gutter(new_text)
    if old not in content:
        return (
            f'Could not find the given text in "{file_path}". Use read_file first to '
            f"get the exact content."
        )
    if content.count(old) > 1:
        # Replacing the first of several is a coin flip about which one the
        # agent meant. Ask for more surrounding context instead of guessing.
        return (
            f'The given text appears {content.count(old)} times in "{file_path}", so '
            f"it does not identify one place. Include enough surrounding lines to "
            f"make old_text unique."
        )
    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    _remember(session_id, path)
    return f'Edited "{file_path}": replaced one occurrence of the given text.'


#: Directories never worth walking: VCS metadata, virtualenvs, caches, build
#: output. In a Python or Node workspace these hold far more files than the
#: project does, and a search that reads them costs seconds and finds nothing
#: the user meant.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv",
    "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".next", "target",
    ".daimon",
})

#: Most results a single search returns. Past this the answer costs more
#: context than it informs, and the right move is a narrower query.
MAX_RESULTS = 100

#: Bytes sampled when deciding whether a file is binary.
_BINARY_SNIFF_BYTES = 1024


def _walk(root: Path):
    """Every file under `root`, with IGNORED_DIRS pruned.

    `os.walk` rather than `Path.rglob` because only os.walk can prune: assigning
    to `dirnames[:]` stops it descending at all, where rglob would already have
    enumerated every file in `.venv` before anything could filter them out.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        base = Path(dirpath)
        for name in sorted(filenames):
            yield base / name


def _is_binary(path: Path) -> bool:
    """A null byte in the first KB — the heuristic git uses. Cheaper and more
    accurate than decoding the whole file with errors="replace" and grepping
    the mojibake that comes out."""
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True  # unreadable is as good as binary here


def glob_files(conf: Confinement, pattern: str) -> str:
    root = conf.root
    # Python's fnmatch lets `*` span "/", so "*.md" matches at any depth and
    # "notes/*.txt" matches by directory — no shell glob semantics needed.
    matches: list[str] = []
    truncated = False
    for path in _walk(root):
        rel = path.relative_to(root)
        if not fnmatch.fnmatch(str(rel), pattern):
            continue
        if len(matches) >= MAX_RESULTS:
            truncated = True
            break
        matches.append(str(rel))
    if not matches:
        return f'No files matching "{pattern}" in the workspace.'
    body = "\n".join(sorted(matches))
    if truncated:
        body += (
            f"\n…({MAX_RESULTS} matches shown, more remain — narrow the pattern "
            f"to see the rest.)"
        )
    return body


def grep_files(conf: Confinement, query: str, file_path: str = "") -> str:
    root = conf.root
    search_root = conf.resolve(file_path) if file_path else root
    if not search_root.exists():
        return f'Path "{file_path}" does not exist.'
    try:
        regex = re.compile(query)
    except re.error as exc:
        return f'Invalid regex "{query}": {exc}'

    # A file_path naming a single file used to walk it as a directory and find
    # nothing at all — searching one file is the narrowing move the truncation
    # message recommends, so it had better work.
    paths = [search_root] if search_root.is_file() else _walk(search_root)

    lines: list[str] = []
    truncated = False
    for path in paths:
        if _is_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root) if conf.contains(path) else path
        for lineno, line in enumerate(text.splitlines(), 1):
            if not regex.search(line):
                continue
            # The cap counts *results*, not files. It used to sit in the outer
            # loop, so a single file with thousands of matches returned every
            # one of them.
            if len(lines) >= MAX_RESULTS:
                truncated = True
                break
            lines.append(f"{rel}:{lineno}: {line[:200]}")
        if truncated:
            break
    if not lines:
        where = search_root.relative_to(root) if conf.contains(search_root) else search_root
        return f'No matches for "{query}" in {where}.'
    body = "\n".join(lines)
    if truncated:
        body += (
            f"\n…({MAX_RESULTS} matches shown, more remain — narrow the query "
            f"or pass file_path to search one directory.)"
        )
    return body
