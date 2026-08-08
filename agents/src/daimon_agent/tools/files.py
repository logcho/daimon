"""Workspace-confined file tools — the harness's Write/Read/Glob/Grep,
narrowed to the workspace. Every path goes through Confinement (resolve-then-
compare; `notes/../../.ssh/id_rsa` cannot escape), so the model gets a plain
error ToolMessage for anything outside instead of touching it.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from ..workspace import Confinement, OutsideWorkspace

MAX_READ_CHARS = 40_000


def read_file(conf: Confinement, file_path: str) -> str:
    path = conf.resolve(file_path)
    if not path.is_file():
        return f'File "{file_path}" does not exist.'
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) > MAX_READ_CHARS:
        content = content[:MAX_READ_CHARS] + "\n…(truncated)"
    return content


def write_file(conf: Confinement, file_path: str, content: str) -> str:
    path = conf.resolve(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f'Wrote {len(content)} characters to "{file_path}".'


def edit_file(conf: Confinement, file_path: str, old_text: str, new_text: str) -> str:
    path = conf.resolve(file_path)
    if not path.is_file():
        return f'File "{file_path}" does not exist.'
    content = path.read_text(encoding="utf-8")
    if old_text not in content:
        return f'Could not find the given text in "{file_path}". Use read_file first to get the exact content.'
    updated = content.replace(old_text, new_text, 1)
    path.write_text(updated, encoding="utf-8")
    return f'Edited "{file_path}": replaced one occurrence of the given text.'


def glob_files(conf: Confinement, pattern: str) -> str:
    root = conf.root
    # Walk the whole workspace and fnmatch the relative path. Python's fnmatch
    # lets `*` span "/", so "*.md" matches at any depth and "notes/*.txt"
    # matches by directory — no shell glob semantics needed.
    matches: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if fnmatch.fnmatch(str(rel), pattern):
            matches.append(str(rel))
        if len(matches) >= 100:
            break
    if not matches:
        return f'No files matching "{pattern}" in the workspace.'
    return "\n".join(sorted(matches))


def grep_files(conf: Confinement, query: str, file_path: str = "") -> str:
    root = conf.root
    search_root = conf.resolve(file_path) if file_path else root
    if not search_root.exists():
        return f'Path "{file_path}" does not exist.'
    try:
        regex = re.compile(query)
    except re.error as exc:
        return f'Invalid regex "{query}": {exc}'
    lines: list[str] = []
    for path in search_root.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = path.relative_to(root)
                lines.append(f"{rel}:{lineno}: {line[:200]}")
        if len(lines) >= 100:
            break
    if not lines:
        return f'No matches for "{query}" in {search_root.relative_to(root) if conf.contains(search_root) else search_root}.'
    return "\n".join(lines)
