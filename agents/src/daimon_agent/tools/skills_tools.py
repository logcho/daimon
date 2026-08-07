"""Skill library tools — skills are real `skills/<name>/SKILL.md` files in the
vault (legacy's final design: user-readable/editable, no DB-only rows).
save_skill also indexes the skill into memory so `recall` finds it."""

from __future__ import annotations

import re
from pathlib import Path

from ..memory import MemoryStore

SKILL_FILENAME = "SKILL.md"

_SAFE_NAME = re.compile(r"[^a-z0-9-]")


def sanitize_skill_name(name: str) -> str:
    return _SAFE_NAME.sub("-", name.strip().lower()).strip("-") or "skill"


def skills_dir(settings: Any) -> Path:
    return settings.resolved_skills_dir


def list_skills(settings: Any) -> str:
    root = skills_dir(settings)
    if not root.exists():
        return "(no skills yet)"
    names = sorted(p.parent.name for p in root.glob(f"*/{SKILL_FILENAME}"))
    return "\n".join(names) if names else "(no skills yet)"


def read_skill(settings: Any, name: str) -> str:
    path = skills_dir(settings) / sanitize_skill_name(name) / SKILL_FILENAME
    if not path.is_file():
        return f'Skill "{name}" does not exist. Use list_skills to see what is available.'
    return path.read_text(encoding="utf-8")


def save_skill(settings: Any, memory: MemoryStore | None, name: str, description: str, content: str) -> str:
    safe = sanitize_skill_name(name)
    directory = skills_dir(settings) / safe
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILENAME).write_text(
        f"---\nname: {safe}\ndescription: {description}\n---\n\n{content}\n",
        encoding="utf-8",
    )
    if memory is not None:
        memory.upsert_skill(safe, description)
    return f'Saved skill "{safe}" ({len(content)} chars) to {directory / SKILL_FILENAME} for future reuse.'
