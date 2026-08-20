"""Discovering published skills.

All against a stubbed httpx client — CI has no network, and the shapes here are
pinned to what the live API actually returns (probed while writing this).

The load-bearing facts, each of which cost a real request to learn:
  - a registry entry is a *repo*, and a repo may hold many skills
  - skill paths cannot be guessed; the GitHub tree is the source of truth
  - a skill is a *directory* — SKILL.md plus references and scripts
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daimon_agent.skills.registry import (
    RegistryError,
    SkillRegistry,
    install_bundle,
)


class _Response:
    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload

    @property
    def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Serves canned responses by URL substring, and counts requests so a
    cache hit is observable."""

    def __init__(self, routes: dict, status: int = 200):
        self.routes = routes
        self.status = status
        self.calls: list[str] = []

    async def get(self, url, headers=None):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, _Response):
                    return payload
                return _Response(payload)
        return _Response({"message": "not found"}, status=404)


SEARCH = {
    "total": 3,
    "results": [
        {
            "slug": "visa-doc-translate", "name": "visa", "description": "Translate visas",
            "type": "skill", "stars": 226099, "featured": 0,
            "source": {"repo": "affaan-m/ecc", "url": "https://github.com/affaan-m/ecc"},
        },
        {
            "slug": "anthropic-official", "name": "Anthropic Official Skills",
            "description": "16 official skills", "type": "skill", "stars": 158271,
            "featured": 1,
            "source": {"repo": "anthropics/skills", "url": "https://github.com/anthropics/skills"},
        },
        {
            "slug": "document-skills", "name": "Document Skills", "description": "Docs",
            "type": "skill", "stars": 66826, "featured": 1,
            "source": {"repo": "ComposioHQ/awesome", "url": "https://github.com/ComposioHQ/awesome"},
        },
    ],
}

TREE = {
    "tree": [
        {"path": "README.md", "type": "blob", "size": 100},
        {"path": "skills/pdf/SKILL.md", "type": "blob", "size": 4000},
        {"path": "skills/pdf/reference.md", "type": "blob", "size": 9000},
        {"path": "skills/pdf/LICENSE.txt", "type": "blob", "size": 500},
        {"path": "skills/pdf/scripts", "type": "tree"},
        {"path": "skills/pdf/scripts/fill_form.py", "type": "blob", "size": 2000},
        {"path": "skills/docx/SKILL.md", "type": "blob", "size": 3000},
    ]
}

SKILL_MD = (
    "---\nname: pdf\ndescription: Anything with PDF files\n"
    "license: Proprietary. LICENSE.txt has complete terms\n---\n\n"
    "# PDF\n\nSee REFERENCE.md.\n"
)


def _registry(routes, tmp_path: Path | None = None) -> SkillRegistry:
    return SkillRegistry(tmp_path, client=_FakeClient(routes))


# --- search ------------------------------------------------------------------

async def test_search_ranks_curated_first_then_stars(tmp_path) -> None:
    """The API orders by relevance alone, which puts an unmaintained repo level
    with Anthropic's official set. Curated first, then stars."""
    registry = _registry({"/search": SEARCH}, tmp_path)
    hits = await registry.search("pdf")
    assert [h.slug for h in hits] == [
        "anthropic-official",   # featured, 158k
        "document-skills",      # featured, 67k
        "visa-doc-translate",   # not featured, despite 226k
    ]


async def test_search_parses_the_source_repo(tmp_path) -> None:
    hits = await _registry({"/search": SEARCH}, tmp_path).search("pdf")
    assert hits[0].repo == "anthropics/skills"
    assert hits[0].featured is True
    assert hits[0].stars == 158271


async def test_search_empty_results(tmp_path) -> None:
    assert await _registry({"/search": {"results": []}}, tmp_path).search("zzz") == []


# --- resolution --------------------------------------------------------------

async def test_resolve_lists_every_skill_in_the_repo(tmp_path) -> None:
    """A registry entry is a repo. `anthropic-official` is 16 skills, so the
    caller has to be given the choice."""
    registry = _registry(
        {"/items/": SEARCH["results"][1], "/git/trees/": TREE}, tmp_path
    )
    entry, paths = await registry.resolve("anthropic-official")
    assert entry.repo == "anthropics/skills"
    assert paths == ["skills/docx", "skills/pdf"]


async def test_resolve_rejects_a_repo_with_no_skills(tmp_path) -> None:
    registry = _registry(
        {"/items/": SEARCH["results"][1], "/git/trees/": {"tree": [{"path": "README.md"}]}},
        tmp_path,
    )
    with pytest.raises(RegistryError, match="no SKILL.md"):
        await registry.resolve("anthropic-official")


async def test_unknown_slug_is_reported_by_name(tmp_path) -> None:
    with pytest.raises(RegistryError, match="no skill named"):
        await _registry({}, tmp_path).item("nope")


# --- bundles -----------------------------------------------------------------

async def test_bundle_pulls_the_whole_directory(tmp_path) -> None:
    """SKILL.md alone gives you a skill whose instructions point at files that
    aren't there — this one says "See REFERENCE.md"."""
    registry = _registry(
        {
            "/items/": SEARCH["results"][1],
            "/git/trees/": TREE,
            "skills/pdf/SKILL.md": SKILL_MD,
            "skills/pdf/reference.md": "# Reference",
            "skills/pdf/LICENSE.txt": "proprietary",
            "skills/pdf/scripts/fill_form.py": "print('hi')",
        },
        tmp_path,
    )
    entry = await registry.item("anthropic-official")
    bundle = await registry.fetch_bundle(entry, "skills/pdf")

    assert sorted(f.path for f in bundle.files) == [
        "LICENSE.txt", "SKILL.md", "reference.md", "scripts/fill_form.py",
    ]
    assert bundle.name == "pdf"
    assert bundle.description == "Anything with PDF files"
    # Surfaced at confirmation: vendoring someone's files is a licensing act.
    assert "Proprietary" in bundle.license
    assert [f.path for f in bundle.scripts] == ["scripts/fill_form.py"]


async def test_bundle_without_a_skill_md_is_rejected(tmp_path) -> None:
    registry = _registry(
        {
            "/items/": SEARCH["results"][1],
            "/git/trees/": {"tree": [{"path": "skills/pdf/other.md", "type": "blob", "size": 10}]},
            "other.md": "hi",
        },
        tmp_path,
    )
    entry = await registry.item("anthropic-official")
    with pytest.raises(RegistryError, match="no SKILL.md"):
        await registry.fetch_bundle(entry, "skills/pdf")


async def test_an_implausibly_large_bundle_is_refused(tmp_path) -> None:
    """A skill is documentation and small scripts. Anything else shouldn't be
    written into someone's library on a single confirmation."""
    huge = {"tree": [
        {"path": f"skills/pdf/f{i}.md", "type": "blob", "size": 10} for i in range(80)
    ]}
    registry = _registry({"/items/": SEARCH["results"][1], "/git/trees/": huge}, tmp_path)
    entry = await registry.item("anthropic-official")
    with pytest.raises(RegistryError, match="too large"):
        await registry.fetch_bundle(entry, "skills/pdf")


# --- caching and limits ------------------------------------------------------

async def test_the_cache_avoids_a_second_request(tmp_path) -> None:
    """GitHub allows 60 requests an hour unauthenticated and one browse spends
    several, so asking the same question twice inside an hour is pure waste."""
    client = _FakeClient({"/search": SEARCH})
    registry = SkillRegistry(tmp_path, client=client)
    await registry.search("pdf")
    await registry.search("pdf")
    assert len(client.calls) == 1


async def test_rate_limit_exhaustion_says_what_happened(tmp_path) -> None:
    """A bare 403 sends people looking for the wrong bug."""
    client = _FakeClient({"/git/trees/": _Response({"message": "rate limited"}, status=403)})
    registry = SkillRegistry(tmp_path, client=client)
    with pytest.raises(RegistryError, match="rate limit"):
        await registry.skill_paths("anthropics/skills")


async def test_without_a_cache_dir_it_still_works(tmp_path) -> None:
    client = _FakeClient({"/search": SEARCH})
    registry = SkillRegistry(None, client=client)
    assert await registry.search("pdf")
    await registry.search("pdf")
    assert len(client.calls) == 2  # no cache, so no saving — but no failure


# --- installing --------------------------------------------------------------

async def test_install_writes_the_whole_bundle(tmp_path) -> None:
    from daimon_agent.skills.registry import BundleFile, SkillBundle

    bundle = SkillBundle(
        slug="s", repo="o/r", skill_dir="skills/pdf", name="pdf",
        files=[
            BundleFile("SKILL.md", "---\nname: pdf\n---\n\nbody"),
            BundleFile("reference.md", "ref"),
            BundleFile("scripts/fill.py", "code"),
        ],
    )
    root = tmp_path / "skills"
    written = install_bundle(bundle, root)

    assert len(written) == 3
    assert (root / "pdf" / "SKILL.md").read_text().endswith("body")
    assert (root / "pdf" / "scripts" / "fill.py").read_text() == "code"


async def test_install_refuses_to_escape_the_skill_directory(tmp_path) -> None:
    """A bundle comes from a stranger's repo; `../` in a path must not write
    wherever it likes."""
    from daimon_agent.skills.registry import BundleFile, SkillBundle

    bundle = SkillBundle(
        slug="s", repo="o/r", skill_dir="d", name="evil",
        files=[
            BundleFile("SKILL.md", "ok"),
            BundleFile("../../pwned.txt", "should not be written"),
        ],
    )
    root = tmp_path / "skills"
    written = install_bundle(bundle, root)

    assert len(written) == 1
    assert not (tmp_path / "pwned.txt").exists()
    assert not (tmp_path.parent / "pwned.txt").exists()


async def test_install_sanitises_the_directory_name(tmp_path) -> None:
    from daimon_agent.skills.registry import BundleFile, SkillBundle

    bundle = SkillBundle(
        slug="s", repo="o/r", skill_dir="d", name="PDF Forms!",
        files=[BundleFile("SKILL.md", "ok")],
    )
    install_bundle(bundle, tmp_path)
    assert (tmp_path / "pdf-forms" / "SKILL.md").is_file()
