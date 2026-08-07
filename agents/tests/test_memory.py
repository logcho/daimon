"""MemoryStore: FTS5 quoting, upsert-by-filename, and the recall format."""

from __future__ import annotations

from daimon_agent.memory import MemoryStore, format_memory_context, fts_query


def test_fts_query_quotes_every_token() -> None:
    assert fts_query('has "quotes" and punctuation!') == '"has" OR "quotes" OR "and" OR "punctuation"'
    assert fts_query("   ") == '""'
    assert fts_query("café-naïve 123") == '"café" OR "naïve" OR "123"'


def test_record_and_search_tasks(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.record_task("build a job application spreadsheet", "Done.", "done")
    store.record_task("water the plants", "Done.", "done")

    rows = store.search_tasks("job application")
    assert len(rows) == 1
    assert rows[0]["instruction"] == "build a job application spreadsheet"
    assert rows[0]["result"] == "Done."

    assert store.search_tasks("plants watering")[0]["instruction"] == "water the plants"
    assert store.search_tasks("unrelated topic") == []


def test_index_note_upserts_by_filename(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.index_note("notes/job-search.md", "Apple interview tomorrow")
    store.index_note("notes/job-search.md", "Stripe interview tomorrow")

    rows = store.search_notes("Stripe")
    assert len(rows) == 1  # upsert, not a second row
    assert rows[0]["content"] == "Stripe interview tomorrow"
    assert store.search_notes("Apple") == []  # old content is gone


def test_upsert_skill_replaces_description(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.upsert_skill("submit-job-application", "old description")
    store.upsert_skill("submit-job-application", "new description")

    rows = store.search_skills("submit")
    assert len(rows) == 1
    assert rows[0]["description"] == "new description"


def test_recall_formats_all_three_sections(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.record_task("apply to Stripe", "Submitted.", "done")
    store.upsert_skill("resume-tune", "Tailor a resume to a Stripe posting")
    store.index_note("notes/stripe.md", "Stripe recruiter: Priya, interview Thu")

    context = store.recall("Stripe")
    assert "Related past tasks:" in context
    assert '"apply to Stripe" -> Submitted.' in context
    assert "Known reusable skills:" in context
    assert "resume-tune: Tailor a resume to a Stripe posting" in context
    assert "Vault notes:" in context
    assert "notes/stripe.md: Stripe recruiter" in context


def test_format_memory_context_empty() -> None:
    assert format_memory_context([], [], []) == ""
