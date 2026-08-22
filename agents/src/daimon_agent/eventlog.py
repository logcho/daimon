"""Durable record of what a human saw, so a client can attach to a session
that has been running for an hour and see something.

Why not rebuild the transcript from the checkpointer? Because the checkpointer
does not hold a transcript — it holds the model's *context*, and `compaction.py`
rewrites that destructively (`RemoveMessage(REMOVE_ALL_MESSAGES)` plus a
summary and `KEEP_LAST` messages). Reconstructing from it would show a phone a
conversation with most of itself deleted, at exactly the point — a long
session — where attaching remotely is worth doing. On top of that, checkpoints
hold Human/AI/ToolMessages, while the UI renders steps, folds, usage and
todos; rebuilding those from messages means writing a second, lossy renderer.

So: the checkpointer stays the truth for what the model knows, this log is the
truth for what the user saw, and the two are allowed to diverge. Replaying the
recorded TaskEvent stream is the same code path as live — the client's normal
event fold handles it — which is the real prize.

Threading: `record()` is called from `bus.publish`, which is synchronous and
may run in one of langchain's worker threads. It therefore appends to a plain
`deque` (thread-safe for append/popleft) rather than an asyncio queue, and a
polling drain task does the SQLite work on the event loop's side. Nothing here
ever blocks a turn.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path

#: How often the drain task writes. Batching turns a storm of assistant_delta
#: events into one transaction.
FLUSH_INTERVAL_S = 0.1

#: Consecutive assistant_delta events on the same channel are merged before
#: they hit disk — a long answer is thousands of two-token events, and stored
#: individually they dwarf everything else in the file. `applyEvent`
#: concatenates delta text, so a merged row replays identically.
COALESCE_LIMIT = 2048

#: Rows kept per session. Pruned when a turn ends rather than on every write.
RETENTION_ROWS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         REAL NOT NULL,
    json       TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS events_by_session ON events (session_id, seq DESC);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    title          TEXT,
    title_explicit INTEGER NOT NULL DEFAULT 0,
    origin         TEXT,
    created_at     REAL NOT NULL,
    last_active_at REAL NOT NULL,
    turns          INTEGER NOT NULL DEFAULT 0,
    last_result    TEXT
);
"""


def _coalesce(records: list[tuple[str, int, dict]]) -> list[tuple[str, int, dict]]:
    """Merge runs of assistant_delta events that share a session, channel and
    agent. The merged row keeps the *first* sequence number of the run: the
    log is only ever replayed as a whole snapshot (cursor-based resume is
    served from the bus ring, which is exact and uncoalesced), so the number
    only has to sort correctly, not address a specific event."""
    out: list[tuple[str, int, dict]] = []
    for session_id, seq, event in records:
        if event.get("type") == "assistant_delta" and out:
            prev_session, prev_seq, prev = out[-1]
            if (
                prev_session == session_id
                and prev.get("type") == "assistant_delta"
                and prev.get("channel") == event.get("channel")
                and prev.get("agent_id") == event.get("agent_id")
                and len(prev.get("text", "")) < COALESCE_LIMIT
            ):
                merged = dict(prev)
                merged["text"] = prev.get("text", "") + event.get("text", "")
                out[-1] = (prev_session, prev_seq, merged)
                continue
        out.append((session_id, seq, event))
    return out


class EventLog:
    """Append-only TaskEvent store, one file per workspace."""

    def __init__(self, db_path: Path, *, retention: int = RETENTION_ROWS) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention = retention
        self._pending: deque[tuple[str, int, dict]] = deque()
        self._lock = threading.Lock()
        # Same reasoning as MemoryStore: sync tools reach this from langchain's
        # worker threads, and the app and the CLI may hold the same file.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- writing ------------------------------------------------------------

    def record(self, session_id: str, seq: int, event: dict) -> None:
        """Enqueue one event. Sync, non-blocking, safe from any thread."""
        self._pending.append((session_id, seq, event))

    def flush(self) -> int:
        """Write everything queued. Returns the number of rows written.

        Called by the drain task and directly by tests and shutdown, so it must
        be safe to call when there is nothing to do."""
        batch: list[tuple[str, int, dict]] = []
        while True:
            try:
                batch.append(self._pending.popleft())
            except IndexError:
                break
        if not batch:
            return 0
        merged = _coalesce(batch)
        now = time.time()
        rows = [(sid, seq, now, json.dumps(ev, ensure_ascii=False)) for sid, seq, ev in merged]
        finished = {sid for sid, _, ev in merged if ev.get("type") in ("done", "error")}
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO events (session_id, seq, ts, json) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._touch(merged, now)
            self._conn.commit()
        for session_id in finished:
            self.prune(session_id)
        return len(rows)

    def _touch(self, records: Iterable[tuple[str, int, dict]], now: float) -> None:
        """Keep the session directory current. Caller holds the lock."""
        for session_id, _, event in records:
            etype = event.get("type")
            self._conn.execute(
                "INSERT INTO sessions (session_id, created_at, last_active_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_active_at = excluded.last_active_at",
                (session_id, now, now),
            )
            if etype == "user":
                # The first thing the user said names the session, unless they
                # have named it themselves. Truncated here rather than at read
                # time so the row stays small.
                self._conn.execute(
                    "UPDATE sessions SET title = ?, origin = COALESCE(origin, ?) "
                    "WHERE session_id = ? AND title IS NULL AND title_explicit = 0",
                    (str(event.get("text", ""))[:60], event.get("origin"), session_id),
                )
            elif etype == "done":
                self._conn.execute(
                    "UPDATE sessions SET turns = turns + 1, last_result = ? WHERE session_id = ?",
                    (str(event.get("result", ""))[:200], session_id),
                )

    def prune(self, session_id: str) -> None:
        """Drop all but the most recent `retention` rows for one session."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM events WHERE session_id = ? AND seq <= ("
                "  SELECT seq FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?"
                ")",
                (session_id, session_id, self.retention),
            )
            self._conn.commit()

    # --- reading ------------------------------------------------------------

    def replay(self, session_id: str, *, limit: int = 800) -> list[dict]:
        """The tail of a session, oldest first — a fresh snapshot for a client
        with no usable cursor. There is deliberately no `since` parameter: see
        `_coalesce` for why the log cannot answer cursor queries exactly."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [json.loads(r["json"]) for r in reversed(rows)]

    def sessions(self) -> list[dict]:
        """The session directory, most recently active first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, title, origin, created_at, last_active_at, turns, last_result "
                "FROM sessions ORDER BY last_active_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, session_id: str, title: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, title, title_explicit, created_at, last_active_at) "
                "VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET title = excluded.title, title_explicit = 1",
                (session_id, title, now, now),
            )
            self._conn.commit()

    def delete_session(self, session_id: str) -> None:
        """Forget a conversation — the other half of DELETE /sessions/{id}.
        Without this, a session the user asked to forget comes straight back
        the next time a client asks for the directory."""
        self._pending = deque(r for r in self._pending if r[0] != session_id)
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._conn.close()
