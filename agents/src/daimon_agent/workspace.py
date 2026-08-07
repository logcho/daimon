"""Workspace confinement — port of `buildCanUseTool` from legacy agent.ts.

Every file-tool path goes through `Confinement.resolve`: expand, absolutize,
`Path.resolve()` to collapse `..` and symlinks, then compare against the
workspace root. A path that lands anywhere outside raises `OutsideWorkspace`
and the tool never touches it (the tools node turns that into a plain error
ToolMessage — the model sees the reason, not a crash).
"""

from __future__ import annotations

from pathlib import Path


class OutsideWorkspace(Exception):
    """Raised when a tool path resolves to somewhere outside the workspace."""


class Confinement:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str) -> Path:
        """Resolve `path` against the workspace and enforce confinement.

        A relative path is taken from the workspace root; an absolute path is
        used as-is. Either way the resolved path (with `..` and symlinks
        collapsed) must live under the root. Encoded dot-dot (`%2e%2e`) is a
        literal filename, not traversal — it stays inside and may simply not
        exist.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise OutsideWorkspace(
                f'path "{path}" resolves to {resolved}, outside the workspace ({self._root})'
            )
        return resolved

    def contains(self, path: Path) -> bool:
        """True if the (already-resolved) path is the root or under it."""
        resolved = path.resolve()
        return resolved == self._root or self._root in resolved.parents
