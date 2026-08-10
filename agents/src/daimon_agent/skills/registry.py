"""Discovering published skills — the ~15k in the open agent-skills ecosystem.

Looking for an existing solution before writing one is the whole point. Daimon
can already *use* skills; this is how it finds ones it doesn't have.

Three steps, because the registry and the content live in different places:

1. **Search** `claudeskills.info/api/v1/search` — good metadata (name,
   description, stars, whether a human curated it), but no skill content.
2. **Resolve** — an entry points at a GitHub *repo*, not a skill. The
   `anthropic-official` entry is 16 skills in `anthropics/skills`. So list the
   repo tree and find its `SKILL.md` files. Paths cannot be guessed: the
   obvious guess for Anthropic's pdf skill 404s, because it lives at
   `skills/pdf/`, not `document-skills/pdf/`.
3. **Fetch** the bundle. A skill is a *directory*: Anthropic's pdf skill is 13
   files — SKILL.md, two reference documents it tells the reader to open, a
   licence, and seven Python scripts. Pulling only SKILL.md gives you a skill
   whose instructions point at files that aren't there.

That last point is also why installing is a user decision rather than something
the agent does on its own: a skill is instructions the agent will follow plus
code it may run, and most of the registry is uncurated. Search is safe and
useful; installing is deliberate. See `cli/tui.py`'s install flow.

GitHub's unauthenticated API allows 60 requests an hour and one browse can
spend several, so responses are cached on disk and exhaustion is reported as
itself rather than as a mysterious failure. `GITHUB_TOKEN` lifts the limit.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REGISTRY_BASE = "https://claudeskills.info/api/v1"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

TIMEOUT_S = 20.0
CACHE_TTL_S = 3600.0
#: Cap on a single bundle. A skill is documentation and small scripts; anything
#: this size is not that, and shouldn't be written into someone's library.
MAX_BUNDLE_FILES = 40
MAX_FILE_BYTES = 512_000

SKILL_FILE = "SKILL.md"


class RegistryError(RuntimeError):
    """Anything that went wrong reaching the registry or GitHub, phrased for
    the user rather than for a stack trace."""


@dataclass
class RegistryEntry:
    slug: str
    name: str
    description: str
    repo: str  # "owner/name"
    url: str
    stars: int = 0
    featured: bool = False
    category: str = ""
    kind: str = "skill"

    @classmethod
    def from_api(cls, raw: dict) -> "RegistryEntry":
        source = raw.get("source") or {}
        return cls(
            slug=str(raw.get("slug", "")),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            repo=str(source.get("repo", "")),
            url=str(source.get("url", "")),
            stars=int(raw.get("stars") or 0),
            featured=bool(raw.get("featured")),
            category=str(raw.get("category", "")),
            kind=str(raw.get("type", "skill")),
        )

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "repo": self.repo,
            "url": self.url,
            "stars": self.stars,
            "featured": self.featured,
            "category": self.category,
            "type": self.kind,
        }


@dataclass
class BundleFile:
    path: str  # relative to the skill directory
    content: str

    @property
    def is_executable(self) -> bool:
        """Scripts a skill might tell the agent to run — worth pointing out
        before any of this lands on someone's disk."""
        return self.path.endswith((".py", ".sh", ".js", ".ts", ".rb", ".pl"))


@dataclass
class SkillBundle:
    slug: str
    repo: str
    skill_dir: str  # path within the repo, e.g. "skills/pdf"
    name: str = ""
    description: str = ""
    license: str = ""
    files: list[BundleFile] = field(default_factory=list)

    @property
    def scripts(self) -> list[BundleFile]:
        return [f for f in self.files if f.is_executable]

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "repo": self.repo,
            "skill_dir": self.skill_dir,
            "name": self.name,
            "description": self.description,
            "license": self.license,
            "files": [{"path": f.path, "executable": f.is_executable} for f in self.files],
        }


# --- caching -----------------------------------------------------------------

class _Cache:
    """A plain file cache. The registry is a catalogue, not live data, and the
    GitHub budget is 60/hour — re-asking the same question inside an hour is
    pure waste."""

    def __init__(self, root: Path | None, ttl: float = CACHE_TTL_S) -> None:
        self.root = Path(root) if root else None
        self.ttl = ttl

    def _path(self, key: str) -> Path | None:
        if self.root is None:
            return None
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.root / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if path is None or not path.is_file():
            return None
        try:
            if time.time() - path.stat().st_mtime > self.ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # a corrupt entry is a miss, not a failure

    def put(self, key: str, value: Any) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
        except (OSError, TypeError):
            pass  # caching is an optimisation; never fail a lookup for it


class SkillRegistry:
    def __init__(self, cache_dir: Path | None = None, *, client: Any = None) -> None:
        self._cache = _Cache(cache_dir)
        self._client = client  # injected in tests; None means "make one"

    # --- HTTP ---------------------------------------------------------------

    def _headers(self, github: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "daimon-agent"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if github and token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _get(self, url: str, *, github: bool = False, raw: bool = False) -> Any:
        cached = self._cache.get(url)
        if cached is not None:
            return cached

        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
        try:
            response = await client.get(url, headers=self._headers(github))
        except Exception as exc:
            raise RegistryError(f"could not reach {url.split('/')[2]}: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code == 403 and github:
            # The 60/hour unauthenticated budget. Say which limit and how to
            # lift it — "403" on its own sends people looking for the wrong bug.
            raise RegistryError(
                "GitHub's API rate limit is exhausted (60 requests/hour without a "
                "token). Wait an hour, or set GITHUB_TOKEN to raise it to 5000."
            )
        if response.status_code == 404:
            raise RegistryError("not found")
        if response.status_code >= 400:
            raise RegistryError(f"request failed (HTTP {response.status_code})")

        value = response.text if raw else response.json()
        self._cache.put(url, value)
        return value

    # --- search -------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        featured: bool = False,
        category: str | None = None,
    ) -> list[RegistryEntry]:
        """Search the catalogue, best first.

        Ranked the way a package search should be: human-curated entries ahead
        of the rest, then by stars. The API's own order is relevance-only, which
        puts an unmaintained one-star repo level with Anthropic's official set.
        """
        params = [f"q={httpx.QueryParams({'q': query})['q']}", f"limit={int(limit)}", "type=skill"]
        if featured:
            params.append("featured=1")
        if category:
            params.append(f"category={category}")
        payload = await self._get(f"{REGISTRY_BASE}/search?{'&'.join(params)}")
        entries = [RegistryEntry.from_api(r) for r in (payload.get("results") or [])]
        entries.sort(key=lambda e: (not e.featured, -e.stars, e.name.lower()))
        return entries

    async def item(self, slug: str) -> RegistryEntry:
        try:
            payload = await self._get(f"{REGISTRY_BASE}/items/{slug}")
        except RegistryError as exc:
            if "not found" in str(exc):
                raise RegistryError(f'no skill named "{slug}" in the registry') from exc
            raise
        return RegistryEntry.from_api(payload)

    # --- resolution ---------------------------------------------------------

    async def _tree(self, repo: str) -> list[dict]:
        payload = await self._get(
            f"{GITHUB_API}/repos/{repo}/git/trees/HEAD?recursive=1", github=True
        )
        if not isinstance(payload, dict) or "tree" not in payload:
            raise RegistryError(f"could not read the file list for {repo}")
        return payload["tree"]

    async def skill_paths(self, repo: str) -> list[str]:
        """Directories in `repo` that contain a SKILL.md.

        A registry entry is a repo, and a repo may hold many skills — which is
        why this returns a list and the caller has to choose.
        """
        paths = [
            entry["path"][: -len(SKILL_FILE) - 1] or "."
            for entry in await self._tree(repo)
            if entry.get("path", "").endswith(SKILL_FILE)
        ]
        return sorted(set(paths))

    async def resolve(self, slug: str) -> tuple[RegistryEntry, list[str]]:
        entry = await self.item(slug)
        if not entry.repo:
            raise RegistryError(f'"{slug}" has no source repository to install from')
        paths = await self.skill_paths(entry.repo)
        if not paths:
            raise RegistryError(f"{entry.repo} contains no SKILL.md")
        return entry, paths

    # --- fetching -----------------------------------------------------------

    async def fetch_bundle(self, entry: RegistryEntry, skill_dir: str) -> SkillBundle:
        """Every file under `skill_dir`, not just SKILL.md — the reference
        documents and scripts a skill points at are part of it."""
        prefix = "" if skill_dir == "." else f"{skill_dir}/"
        wanted = [
            item
            for item in await self._tree(entry.repo)
            if item.get("type") == "blob"
            and item.get("path", "").startswith(prefix)
            and (item.get("size") or 0) <= MAX_FILE_BYTES
        ]
        if len(wanted) > MAX_BUNDLE_FILES:
            raise RegistryError(
                f"{skill_dir} has {len(wanted)} files — too large to be a skill; "
                f"install it by hand if you're sure"
            )

        files: list[BundleFile] = []
        for item in wanted:
            path = item["path"]
            text = await self._get(
                f"{GITHUB_RAW}/{entry.repo}/HEAD/{path}", github=False, raw=True
            )
            files.append(BundleFile(path=path[len(prefix):], content=text))

        bundle = SkillBundle(
            slug=entry.slug, repo=entry.repo, skill_dir=skill_dir, files=files
        )
        skill_md = next((f for f in files if f.path == SKILL_FILE), None)
        if skill_md is None:
            raise RegistryError(f"{skill_dir} has no {SKILL_FILE}")
        meta = _frontmatter(skill_md.content)
        bundle.name = meta.get("name") or Path(skill_dir).name
        bundle.description = meta.get("description", "")
        # Surfaced at confirmation: Anthropic's own skills are "Proprietary",
        # and vendoring someone's file into your library is a licensing act.
        bundle.license = meta.get("license", "")
        return bundle


def _frontmatter(text: str) -> dict[str, str]:
    """Same shape the injector parses; duplicated rather than imported to keep
    registry.py free of a dependency on the local library's loader."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


def install_bundle(bundle: SkillBundle, root: Path) -> list[str]:
    """Write a bundle into a skills library. Returns the paths written.

    Every path is re-anchored under the skill's own directory: a bundle comes
    from a stranger's repo, and `../` in a path would otherwise write wherever
    it liked.
    """
    target = Path(root) / _safe_name(bundle.name)
    written: list[str] = []
    for item in bundle.files:
        destination = (target / item.path).resolve()
        if not str(destination).startswith(str(target.resolve())):
            continue  # path traversal — skip it rather than write outside
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item.content, encoding="utf-8")
        written.append(str(destination))
    return written


def _safe_name(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9-]", "-", name.strip().lower()).strip("-") or "skill"
