"""Vault-confined note tools — the agent's own way to keep notes.

Distinct from the workspace file tools on purpose. `write_file` writes into
whatever directory the agent is *working* in, which for a CLI session is the
project you happened to run `daimon` from; a note written there is lost to the
vault and to `recall`. These tools always land in the vault, wherever the
session is working, so "make a note of this" means the same thing in the app
and in a checkout of some unrelated repo.

Every write also indexes the note in memory, because a note the agent can't
recall later is barely a note at all — and the app reads the same vault, so
whatever is written here shows up in the notes UI immediately.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..memory import MemoryStore
from ..workspace import Confinement

#: Notes are markdown. The app's listing and the vault endpoints are
#: `*.md`-only, so anything else would be written and then never seen again.
NOTE_SUFFIX = ".md"


def _as_note_name(name: str) -> str:
    """Normalise a model-supplied name: strip slashes at the ends, add `.md`."""
    cleaned = name.strip().strip("/")
    if not cleaned:
        return ""
    return cleaned if cleaned.lower().endswith(NOTE_SUFFIX) else cleaned + NOTE_SUFFIX


def _index(memory: MemoryStore | None, name: str, content: str) -> None:
    """Best-effort reindex. A failed index costs recall accuracy, never the
    note itself, so it must not turn a successful write into an error."""
    if memory is None:
        return
    try:
        memory.index_note(name, content)
    except Exception:  # noqa: BLE001 - see docstring
        pass


def _unindex(memory: MemoryStore | None, name: str) -> None:
    if memory is None:
        return
    try:
        memory.delete_note(name)
    except Exception:  # noqa: BLE001
        pass


def create_note(
    conf: Confinement, memory: MemoryStore | None, name: str, content: str
) -> str:
    """Create a note, or overwrite one that already exists."""
    note = _as_note_name(name)
    if not note:
        return "A note needs a name."
    path = conf.resolve(note)
    if path.is_dir():
        return f'"{note}" is a folder, not a note.'
    existed = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _index(memory, note, content)
    verb = "Updated" if existed else "Created"
    return f'{verb} note "{note}" ({len(content)} characters).'


def append_to_note(
    conf: Confinement, memory: MemoryStore | None, name: str, content: str
) -> str:
    """Add to the end of a note, creating it if it isn't there yet.

    Separate from `create_note` because the common case — adding today's entry
    to a running log — is a read-modify-write the model would otherwise have to
    do by hand, and would get wrong by overwriting the note it meant to extend.
    """
    note = _as_note_name(name)
    if not note:
        return "A note needs a name."
    path = conf.resolve(note)
    if path.is_dir():
        return f'"{note}" is a folder, not a note.'
    existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    updated = existing + separator + content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    _index(memory, note, updated)
    return f'Appended {len(content)} characters to "{note}".'


def read_note(conf: Confinement, name: str) -> str:
    note = _as_note_name(name)
    path = conf.resolve(note)
    if not path.is_file():
        return f'There is no note called "{note}".'
    return path.read_text(encoding="utf-8", errors="replace")


def list_notes(conf: Confinement, folder: str = "") -> str:
    """List notes and folders, newest note first."""
    root = conf.resolve(folder) if folder.strip() else conf.root
    if not root.is_dir():
        return f'There is no folder called "{folder}".'
    notes: list[tuple[float, str]] = []
    folders: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(conf.root)
        # `skills` has its own tools, and dot-directories are plumbing (the
        # workspace's own databases live in `.daimon`).
        if any(part == "skills" or part.startswith(".") for part in rel.parts):
            continue
        if path.is_dir():
            folders.append(str(rel))
        elif path.suffix.lower() == NOTE_SUFFIX:
            notes.append((path.stat().st_mtime, str(rel)))
    if not notes and not folders:
        return "The vault is empty." if not folder else f'"{folder}" is empty.'
    notes.sort(reverse=True)
    lines = []
    if folders:
        lines.append("Folders: " + ", ".join(sorted(folders)))
    if notes:
        lines.append("Notes (newest first):")
        lines += [
            f"  {name}  ({datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M})"
            for mtime, name in notes[:200]
        ]
    return "\n".join(lines)


def create_note_folder(conf: Confinement, path: str) -> str:
    """Make a folder to organise notes into."""
    cleaned = path.strip().strip("/")
    if not cleaned:
        return "A folder needs a name."
    target = conf.resolve(cleaned)
    if target.is_file():
        return f'"{cleaned}" is already a note, so it can\'t be a folder.'
    if target.is_dir():
        return f'Folder "{cleaned}" already exists.'
    target.mkdir(parents=True, exist_ok=True)
    return f'Created folder "{cleaned}".'


def move_note(
    conf: Confinement, memory: MemoryStore | None, source: str, destination: str
) -> str:
    """Move or rename a note, or a whole folder of notes.

    Re-keys the memory index: it is keyed by name, so leaving the old entries
    would have `recall` citing paths that no longer exist.
    """
    src_name = source.strip().strip("/")
    if not src_name:
        return "Nothing to move."
    src = conf.resolve(src_name)
    if not src.exists():
        return f'There is no note or folder called "{src_name}".'

    is_folder = src.is_dir()
    dst_name = destination.strip().strip("/") if is_folder else _as_note_name(destination)
    if not dst_name:
        return "A destination is required."
    dst = conf.resolve(dst_name)
    if dst.exists():
        return f'"{dst_name}" already exists — pick another name.'
    if is_folder and (dst == src or src in dst.parents):
        return f'Cannot move "{src_name}" inside itself.'

    moved = [str(p.relative_to(conf.root)) for p in src.rglob("*" + NOTE_SUFFIX)] if is_folder else [src_name]
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)

    for old in moved:
        new = dst_name if not is_folder else f"{dst_name}/{old[len(src_name) + 1:]}"
        _unindex(memory, old)
        new_path = Path(conf.root) / new
        if new_path.is_file():
            _index(memory, new, new_path.read_text(encoding="utf-8", errors="replace"))

    if is_folder:
        return f'Moved folder "{src_name}" to "{dst_name}" ({len(moved)} note(s)).'
    return f'Moved note "{src_name}" to "{dst_name}".'
