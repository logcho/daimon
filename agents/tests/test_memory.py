"""MemoryStore: FTS5 quoting, upsert-by-filename, and the recall format."""

from __future__ import annotations

from daimon_agent.memory import MemoryStore, format_memory_context, fts_query


def test_fts_query_quotes_every_token() -> None:
    assert fts_query('has "quotes" and punctuation!') == '"has" OR "quotes" OR "and" OR "punctuation"'
    assert fts_query("   ") == '""'
    assert fts_query("café-naïve 123") == '"café" OR "naïve" OR "123"'


def test_recall_works_from_worker_threads_concurrently(tmp_path) -> None:
    # langchain runs sync tools (recall) in worker threads, and concurrent
    # sessions' turns can hit the store from two threads at once. Without
    # check_same_thread=False + the lock this raises "SQLite objects created
    # in a thread can only be used in that same thread".
    import threading

    store = MemoryStore(tmp_path / "memory.db")
    store.record_task("favorite color?", "blue", "done")
    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(store.recall("color"))
        except Exception as exc:  # noqa: BLE001 — the point is to catch any
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.close()
    assert errors == []
    assert len(results) == 2
    assert all("blue" in r for r in results)


def test_busy_timeout_waits_not_errors(tmp_path) -> None:
    # Two daimon-agent processes (app + CLI) may share the memory file — the
    # pragma value IS the contract: wait, don't error.
    store = MemoryStore(tmp_path / "memory.db")
    try:
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    finally:
        store.close()


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
    assert "Notes:" in context
    assert "notes/stripe.md: Stripe recruiter" in context


def test_format_memory_context_empty() -> None:
    assert format_memory_context([], [], []) == ""
