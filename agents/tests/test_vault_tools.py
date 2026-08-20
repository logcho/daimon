"""The agent's own note tools — vault-confined, and indexed for recall.

The point of these being separate from the workspace file tools is that a note
lands in the vault no matter where the session is working, so most of what
matters here is *where things end up* and *what memory knows about them*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daimon_agent.memory import MemoryStore
from daimon_agent.tools import vault as _vault
from daimon_agent.workspace import Confinement, OutsideWorkspace


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def conf(vault: Path) -> Confinement:
    return Confinement(vault)


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory" / "daimon.db")
    yield store
    store.close()


# --- creating -----------------------------------------------------------------

def test_create_note_writes_and_indexes(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    out = _vault.create_note(conf, memory, "kagi-pricing", "# Kagi\n\nunmistakable durian pricing")
    assert "Created" in out
    # `.md` is added, so the app's markdown-only listing can see it.
    assert (vault / "kagi-pricing.md").read_text().startswith("# Kagi")
    assert memory.search_notes("durian")


def test_create_note_files_into_folders(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    _vault.create_note(conf, memory, "research/papers/dinov3.md", "body")
    assert (vault / "research" / "papers" / "dinov3.md").read_text() == "body"


def test_create_note_overwrites_and_reindexes(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    _vault.create_note(conf, memory, "a.md", "original lychee text")
    out = _vault.create_note(conf, memory, "a.md", "replacement text")
    assert "Updated" in out
    assert (vault / "a.md").read_text() == "replacement text"
    # The old body must not still be searchable — that would have recall
    # quoting text the note no longer contains.
    assert not memory.search_notes("lychee")


def test_create_note_rejects_an_empty_name(conf: Confinement, memory: MemoryStore) -> None:
    assert "needs a name" in _vault.create_note(conf, memory, "   ", "body")


def test_notes_cannot_escape_the_vault(conf: Confinement, memory: MemoryStore) -> None:
    """Same discipline as the workspace tools: resolve, then contain."""
    with pytest.raises(OutsideWorkspace):
        _vault.create_note(conf, memory, "../../escaped.md", "nope")


# --- appending ----------------------------------------------------------------

def test_append_extends_rather_than_replacing(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    _vault.create_note(conf, memory, "log.md", "# Log\n")
    _vault.append_to_note(conf, memory, "log.md", "- first entry")
    _vault.append_to_note(conf, memory, "log.md", "- second entry")
    body = (vault / "log.md").read_text()
    assert "# Log" in body and "- first entry" in body and "- second entry" in body
    assert memory.search_notes("second")


def test_append_creates_a_missing_note(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    _vault.append_to_note(conf, memory, "new.md", "content")
    assert (vault / "new.md").read_text().strip() == "content"


# --- reading and listing ------------------------------------------------------

def test_read_note_round_trips(conf: Confinement, memory: MemoryStore) -> None:
    _vault.create_note(conf, memory, "a.md", "the body")
    assert _vault.read_note(conf, "a") == "the body"  # .md implied


def test_read_missing_note_explains_itself(conf: Confinement) -> None:
    assert "no note" in _vault.read_note(conf, "nope.md").lower()


def test_list_notes_hides_internal_directories(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    """`.daimon` holds the workspace's databases and `skills` has its own
    tools — neither is a note the user filed."""
    _vault.create_note(conf, memory, "real.md", "x")
    (vault / ".daimon" / "memory").mkdir(parents=True)
    (vault / ".daimon" / "internal.md").write_text("plumbing")
    (vault / "skills" / "s").mkdir(parents=True)
    (vault / "skills" / "s" / "SKILL.md").write_text("a skill")

    listing = _vault.list_notes(conf)
    assert "real.md" in listing
    assert ".daimon" not in listing and "SKILL.md" not in listing


def test_list_notes_reports_an_empty_vault(conf: Confinement) -> None:
    assert "empty" in _vault.list_notes(conf).lower()


# --- organising ---------------------------------------------------------------

def test_create_note_folder(conf: Confinement, vault: Path) -> None:
    assert "Created folder" in _vault.create_note_folder(conf, "research")
    assert (vault / "research").is_dir()
    assert "already exists" in _vault.create_note_folder(conf, "research")


def test_move_note_rekeys_the_index(conf: Confinement, vault: Path, memory: MemoryStore) -> None:
    _vault.create_note(conf, memory, "loose.md", "distinctive papaya text")
    _vault.move_note(conf, memory, "loose.md", "research/loose.md")

    assert not (vault / "loose.md").exists()
    assert (vault / "research" / "loose.md").exists()
    hits = [dict(r)["filename"] for r in memory.search_notes("papaya")]
    assert hits == ["research/loose.md"]


def test_move_folder_takes_its_notes_and_rekeys_them(
    conf: Confinement, vault: Path, memory: MemoryStore
) -> None:
    _vault.create_note(conf, memory, "inbox/a.md", "unmistakable lychee text")
    _vault.create_note(conf, memory, "inbox/deep/b.md", "b")

    out = _vault.move_note(conf, memory, "inbox", "archive/inbox")
    assert "2 note(s)" in out
    assert (vault / "archive" / "inbox" / "deep" / "b.md").exists()
    hits = [dict(r)["filename"] for r in memory.search_notes("lychee")]
    assert hits == ["archive/inbox/a.md"]


def test_move_will_not_clobber_or_self_nest(conf: Confinement, memory: MemoryStore) -> None:
    _vault.create_note(conf, memory, "one.md", "one")
    _vault.create_note(conf, memory, "two.md", "two")
    _vault.create_note(conf, memory, "projects/x.md", "x")

    assert "already exists" in _vault.move_note(conf, memory, "one.md", "two.md")
    assert "inside itself" in _vault.move_note(conf, memory, "projects", "projects/inner")
    assert "no note or folder" in _vault.move_note(conf, memory, "ghost.md", "x.md")
