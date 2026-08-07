"""Persistent cross-session memory — port of `legacy/agents/src/memory.ts`.

SQLite (WAL) with three FTS5 virtual tables over tasks, skills, and notes.
Search wraps every alphanumeric token in quotes so raw instructions with
punctuation can never be misread as FTS5 query syntax. The `skills` table
indexes `vault/skills/<name>/SKILL.md` files (upserted by the save_skill tool)
so recall can find them; the files themselves stay the source of truth.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  instruction TEXT NOT NULL,
  result TEXT,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(id UNINDEXED, instruction, result);

CREATE TABLE IF NOT EXISTS skills (
  name TEXT PRIMARY KEY,
  description TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(name, description);

CREATE TABLE IF NOT EXISTS notes (
  filename TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(filename UNINDEXED, content);
"""

# \w with re.UNICODE (the default in py3) matches Unicode letters and digits,
# so accented words like "café" survive; FTS5's unicode61 tokenizer agrees.
_WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(raw: str) -> str:
    """Quote each token so punctuation in a raw instruction can't be misread
    as FTS5 query syntax."""
    tokens = _WORD.findall(raw)
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def _matches_and_rows(
    conn: sqlite3.Connection,
    fts_table: str,
    table: str,
    key_col: str,
    query: str,
    limit: int,
    order_col: str,
) -> list[sqlite3.Row]:
    matches = conn.execute(
        f"SELECT {key_col} FROM {fts_table} WHERE {fts_table} MATCH ? ORDER BY rank LIMIT ?",
        (fts_query(query), limit),
    ).fetchall()
    if not matches:
        return []
    keys = [m[0] for m in matches]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE {key_col} IN ({placeholders}) ORDER BY {order_col} DESC",
        keys,
    ).fetchall()
    return rows


class MemoryStore:
    """One store per process. Sync sqlite3 is fine here: writes are single
    inserts and reads are bounded, and langchain already runs sync tool calls
    in a worker thread."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- tasks -------------------------------------------------------------

    def record_task(self, instruction: str, result: str | None, status: str) -> None:
        task_id = str(uuid.uuid4())
        created_at = int(time.time() * 1000)
        self._conn.execute(
            "INSERT INTO tasks (id, instruction, result, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, instruction, result, status, created_at),
        )
        self._conn.execute(
            "INSERT INTO tasks_fts (id, instruction, result) VALUES (?, ?, ?)",
            (task_id, instruction, result or ""),
        )
        self._conn.commit()

    def search_tasks(self, query: str, limit: int = 5) -> list[sqlite3.Row]:
        return _matches_and_rows(self._conn, "tasks_fts", "tasks", "id", query, limit, "created_at")

    # -- skills ------------------------------------------------------------

    def upsert_skill(self, name: str, description: str) -> None:
        created_at = int(time.time() * 1000)
        self._conn.execute(
            "INSERT INTO skills (name, description, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET description = excluded.description",
            (name, description, created_at),
        )
        self._conn.execute("DELETE FROM skills_fts WHERE name = ?", (name,))
        self._conn.execute(
            "INSERT INTO skills_fts (name, description) VALUES (?, ?)", (name, description)
        )
        self._conn.commit()

    def search_skills(self, query: str, limit: int = 5) -> list[sqlite3.Row]:
        return _matches_and_rows(self._conn, "skills_fts", "skills", "name", query, limit, "created_at")

    # -- notes -------------------------------------------------------------

    def index_note(self, filename: str, content: str) -> None:
        """Upsert by filename — FTS5 virtual tables don't support ON CONFLICT,
        so it's delete-then-insert into both tables."""
        updated_at = int(time.time() * 1000)
        self._conn.execute(
            "INSERT INTO notes (filename, content, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET content = excluded.content, "
            "updated_at = excluded.updated_at",
            (filename, content, updated_at),
        )
        self._conn.execute("DELETE FROM notes_fts WHERE filename = ?", (filename,))
        self._conn.execute("INSERT INTO notes_fts (filename, content) VALUES (?, ?)", (filename, content))
        self._conn.commit()

    def search_notes(self, query: str, limit: int = 5) -> list[sqlite3.Row]:
        return _matches_and_rows(self._conn, "notes_fts", "notes", "filename", query, limit, "updated_at")

    # -- combined ----------------------------------------------------------

    def recall(self, query: str) -> str:
        """The recall tool's body — tasks + skills + notes, formatted."""
        return format_memory_context(
            self.search_tasks(query, 3), self.search_skills(query, 3), self.search_notes(query, 3)
        )


def format_memory_context(tasks: list, skills: list, notes: list) -> str:
    parts: list[str] = []
    if tasks:
        parts.append(
            "Related past tasks:\n"
            + "\n".join(f'- "{t["instruction"]}" -> {t["result"] or "(no result)"}' for t in tasks)
        )
    if skills:
        parts.append(
            "Known reusable skills:\n"
            + "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
        )
    if notes:
        parts.append(
            "Vault notes:\n"
            + "\n".join(f"- {n['filename']}: {n['content'][:200]}" for n in notes)
        )
    return "\n\n".join(parts)
