"""Resolving paths inside the notes vault.

Its own module because both the HTTP routes and the bus socket need it, and
`server` imports `busws` — putting it in either would be a cycle. The
containment rule is the thing worth having in exactly one place: a
client-supplied note name is attacker-controlled input in the remote case, and
two copies of a path check is how one of them ends up subtly weaker.
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings


def vault_file(settings_obj: Settings, relative: str) -> Path:
    """Resolve a client-supplied path inside the vault, or raise.

    Same discipline as `workspace.Confinement`: resolve first, then check
    containment, so `..` and symlinks can't walk out. Used for folders as
    well as notes — this is pure path resolution; whether the target has to
    be a `.md` file is the caller's rule.
    """
    root = Path(settings_obj.vault_dir).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path is outside the vault")
    return target

def is_internal(relative: Path) -> bool:
    """Paths the vault views must not show.

    `skills` has its own view, and dot-directories are plumbing rather
    than notes — `.daimon/memory` holds this workspace's own databases and
    would otherwise show up in the tree as a folder the user can't
    meaningfully use but can delete.
    """
    return any(part == "skills" or part.startswith(".") for part in relative.parts)
