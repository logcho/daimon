"""Runtime skill library — SkillOpt's core idea in its simplest form.

Skills are plain `skills/<name>/SKILL.md` files: frontmatter (name,
description) plus a markdown body the agent should follow. They come from two
places — the vault library, which follows the user everywhere, and the current
project's `.daimon/skills/`, which travels with the repo and can be committed
for a team. A project skill shadows a vault skill of the same name.

**Loading is progressive.** Every turn the prompt carries only a one-line index
— each skill's name and description — and the agent calls `read_skill` when one
actually applies. The alternative (pasting whole bodies for whatever the
keyword match hit) spends context on skills that turn out to be irrelevant and
stops scaling once the library is more than a handful. `format_skills_block`,
which does paste bodies, survives for the optimizer: its rollouts vary one
skill's *body* between candidate and baseline, so the body has to be in the
prompt for the comparison to mean anything.

The optimizer (daimon_agent/optimizer) treats these files as the evolution
target: propose edits, validate on held-out tasks, gate by strict
improvement. The files are the source of truth; the FTS `skills` table in
memory.py only indexes them for recall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SKILL_FILE = "SKILL.md"

#: Skills listed in full before ranking kicks in. Past this the index would
#: cost more prompt than it saves, so `select_skills` narrows it.
MAX_INDEXED = 40


@dataclass
class Skill:
    name: str
    description: str
    content: str = ""
    path: Path | None = None
    #: Which library it came from — "project" shadows "vault".
    source: str = "vault"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split `---\nkey: value\n---\n\nbody`. Malformed files degrade to an
    empty frontmatter with the whole text as the body — a skill that doesn't
    parse must never crash the turn."""
    meta: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
            return meta, text[end + 4 :].lstrip("\n")
    return meta, text


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _load_dir(skills_dir: Path, source: str) -> list[Skill]:
    """Every `skills_dir/*/SKILL.md`, in name order. A missing directory is
    an empty library, not an error."""
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        return []
    skills: list[Skill] = []
    for path in sorted(skills_dir.glob(f"*/{_SKILL_FILE}")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # one unreadable skill must not hide the rest
        meta, body = _parse_frontmatter(text)
        skills.append(
            Skill(
                name=meta.get("name") or path.parent.name,
                description=meta.get("description") or "",
                content=body.strip(),
                path=path,
                source=source,
            )
        )
    return skills


def discover_skills(
    vault_dir: Path | None = None, project_dir: Path | None = None
) -> list[Skill]:
    """The merged library, sorted by name.

    A project skill shadows a vault skill of the same name — the repo's own
    version of a procedure should win over the user's general one, the same way
    a project config beats a global one.
    """
    merged: dict[str, Skill] = {}
    if vault_dir is not None:
        for skill in _load_dir(vault_dir, "vault"):
            merged[skill.name] = skill
    if project_dir is not None:
        for skill in _load_dir(project_dir, "project"):
            merged[skill.name] = skill
    return sorted(merged.values(), key=lambda s: s.name)


def select_skills(instruction: str, skills: list[Skill], k: int = MAX_INDEXED) -> list[Skill]:
    """The skills worth listing for this instruction.

    Under the cap, everything is listed — a one-line index is cheap, and the
    agent is a better judge of relevance than token overlap is. Above it,
    fall back to ranking by overlap against name + description (cheap,
    deterministic, no model call) so a large library stays affordable.
    """
    if len(skills) <= k:
        return list(skills)
    query = _tokenize(instruction)
    scored = []
    for skill in skills:
        haystack = _tokenize(f"{skill.name} {skill.description}")
        scored.append((-len(query & haystack), skill.name, skill))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in scored[:k]]


def format_skills_index(skills: list[Skill]) -> str:
    """The prompt tail: one line per skill, no bodies.

    This is what makes the library scale. The agent sees what exists and reads
    the one it needs; a skill that never comes up costs a single line.
    """
    if not skills:
        return ""
    lines = [
        "# Available skills",
        "",
        "Procedures already written down for reuse. When one covers what you're "
        "about to do, read it with read_skill and follow it rather than working "
        "it out again.",
        "",
    ]
    for skill in skills:
        suffix = " (project)" if skill.source == "project" else ""
        description = skill.description or "(no description)"
        lines.append(f"- {skill.name}{suffix}: {description}")
    return "\n".join(lines)


def format_skills_block(skills: list[Skill]) -> str:
    """Name, description, *and* body inlined.

    Used by the optimizer's rollouts, which vary a skill's body between
    candidate and baseline — the body has to reach the model for the score
    delta to be attributable to it. Normal turns use `format_skills_index`.
    """
    if not skills:
        return ""
    sections = []
    for skill in skills:
        header = f"## Skill: {skill.name}"
        if skill.description:
            header += f" — {skill.description}"
        sections.append(header + ("\n\n" + skill.content if skill.content else ""))
    return "# Reusable skills\n\n" + "\n\n".join(sections)
