"""Editing a dotenv file in place.

One implementation, because there are two writers — the server's POST /config
and the CLI's `/setup` — and the interesting behaviour (uncommenting a
placeholder line, preserving everything else) is easy to get subtly different
in two copies.
"""

from __future__ import annotations

from pathlib import Path


def patch_env_file(path: Path, key: str, value: str) -> None:
    """Set `key` in a dotenv file, preserving comments and ordering.

    Replaces an existing assignment or a commented-out placeholder (`# KEY=`),
    which is what `.env.example`-derived files are full of; appends otherwise.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            out.append(f"{key}={value}")
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
