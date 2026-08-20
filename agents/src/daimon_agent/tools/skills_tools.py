"""Skill library tools — skills are real `skills/<name>/SKILL.md` files
(legacy's final design: user-readable/editable, no DB-only rows), in two
libraries: the vault's, which follows the user, and the project's
`.daimon/skills/`, which travels with the repo.

save_skill also indexes the skill into memory so `recall` finds it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..memory import MemoryStore
from ..skills.injector import discover_skills

SKILL_FILENAME = "SKILL.md"

_SAFE_NAME = re.compile(r"[^a-z0-9-]")


def sanitize_skill_name(name: str) -> str:
    return _SAFE_NAME.sub("-", name.strip().lower()).strip("-") or "skill"


def skills_dir(settings: Any) -> Path:
    """The global library — where a skill goes unless asked for otherwise."""
    return settings.resolved_skills_dir


def _library(settings: Any) -> list:
    return discover_skills(settings.resolved_skills_dir, settings.project_skills_dir)


def list_skills(settings: Any) -> str:
    """Both libraries, one line each. Descriptions are included because a bare
    name doesn't tell the agent whether a skill is worth reading."""
    skills = _library(settings)
    if not skills:
        return "(no skills yet)"
    lines = []
    for skill in skills:
        suffix = " (project)" if skill.source == "project" else ""
        description = skill.description or "(no description)"
        lines.append(f"{skill.name}{suffix}: {description}")
    return "\n".join(lines)


def _skill_dir(settings: Any, name: str) -> Path | None:
    safe = sanitize_skill_name(name)
    for skill in _library(settings):
        if skill.name == safe or (skill.path and skill.path.parent.name == safe):
            if skill.path is not None:
                return skill.path.parent
    return None


def read_skill(settings: Any, name: str, file: str = "") -> str:
    """A skill's body, or one of the files bundled with it.

    Installed skills are directories, not single files: Anthropic's pdf skill
    ships reference documents its SKILL.md tells the reader to open, plus
    scripts. Those siblings usually sit in the vault, which is outside the
    workspace, so the workspace-confined file tools can't reach them — hence
    reading them through here.

    Paths are resolved inside the skill's own directory and nowhere else. A
    skill's text can come from a stranger's repo, so `../` in a path it suggests
    must not turn into a read of anything it likes.
    """
    directory = _skill_dir(settings, name)
    if directory is None:
        return f'Skill "{name}" does not exist. Use list_skills to see what is available.'

    if not file:
        body = (directory / SKILL_FILENAME).read_text(encoding="utf-8")
        siblings = sorted(
            str(p.relative_to(directory))
            for p in directory.rglob("*")
            if p.is_file() and p.name != SKILL_FILENAME
        )
        if siblings:
            # Say what else is here and how to reach it, or the agent reads
            # "see REFERENCE.md" and has no way to act on it.
            body += (
                f"\n\n---\nBundled with this skill (read with "
                f'read_skill(name="{sanitize_skill_name(name)}", file="…")): '
                + ", ".join(siblings)
                + f"\nOn disk at: {directory}"
            )
        return body

    target = (directory / file).resolve()
    if target != directory.resolve() and directory.resolve() not in target.parents:
        return f'"{file}" is outside the skill directory.'
    if not target.is_file():
        return f'"{file}" is not a file in skill "{name}".'
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f'Could not read "{file}": {exc}'


def save_skill(
    settings: Any,
    memory: MemoryStore | None,
    name: str,
    description: str,
    content: str,
    scope: str = "vault",
) -> str:
    """Write a skill. `scope` picks the library: "vault" (default — it follows
    the user between projects) or "project" (it lives with the repo and can be
    committed).

    The agent's own `save_skill` tool no longer exposes `scope` and always
    lands in the vault library: a project skill is invisible to any session
    whose workspace isn't that project, including the app. Project scope is
    now only chosen by the user, through `/skills install`."""
    safe = sanitize_skill_name(name)
    root = (
        settings.project_skills_dir
        if str(scope).strip().lower() == "project"
        else skills_dir(settings)
    )
    directory = root / safe
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILENAME).write_text(
        f"---\nname: {safe}\ndescription: {description}\n---\n\n{content}\n",
        encoding="utf-8",
    )
    if memory is not None:
        memory.upsert_skill(safe, description)
    return f'Saved skill "{safe}" ({len(content)} chars) to {directory / SKILL_FILENAME} for future reuse.'
