"""Runtime skill library — SkillOpt's core idea in its simplest form.

Skills are plain `skills/<name>/SKILL.md` files in the vault: frontmatter
(name, description) plus a markdown body the agent should follow. Each turn
the injector discovers the library, ranks skills against the instruction by
token overlap, and appends the best few after the frozen rules prefix of the
system prompt — so the rules text (the DeepSeek prefix-cache hit) never
changes, only the per-task tail.

The optimizer (daimon_agent/optimizer) treats these files as the evolution
target: propose edits, validate on held-out tasks, gate by strict
improvement. The files are the source of truth; the FTS `skills` table in
memory.py only indexes them for recall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SKILL_FILE = "SKILL.md"


@dataclass
class Skill:
    name: str
    description: str
    content: str = ""
    path: Path | None = None


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


def discover_skills(skills_dir: Path) -> list[Skill]:
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
            )
        )
    return skills


def select_skills(instruction: str, skills: list[Skill], k: int = 3) -> list[Skill]:
    """Rank by token overlap against name + description — cheap, deterministic,
    and no model call (Flash is for bulk calls, not per-turn ranking). Returns
    at most k skills with any overlap, best first."""
    query = _tokenize(instruction)
    scored = []
    for skill in skills:
        haystack = _tokenize(f"{skill.name} {skill.description}")
        overlap = len(query & haystack)
        if overlap:
            scored.append((overlap, skill.name, skill))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:k]]


def format_skills_block(skills: list[Skill]) -> str:
    """The prompt tail: name + description up front, body after. Skills live
    after the frozen rules prefix, so cache hits are untouched."""
    if not skills:
        return ""
    sections = []
    for skill in skills:
        header = f"## Skill: {skill.name}"
        if skill.description:
            header += f" — {skill.description}"
        sections.append(header + ("\n\n" + skill.content if skill.content else ""))
    return "# Reusable skills\n\n" + "\n\n".join(sections)
