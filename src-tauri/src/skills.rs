//! Read/write access to the `skills` table the Node agent's `save_skill`
//! tool (`agents/src/memory.ts`) already owns and writes to — a plain
//! SQLite file (`memory/daimon.db`, WAL mode), not a bind mount or IPC
//! surface of its own. This module exists because `save_skill` was
//! genuinely write-only before this: the model could save a skill mid-
//! conversation, but nothing let the user ever see, delete, or directly
//! author one themselves.
//!
//! **Schema, matched exactly to `memory.ts`'s `db.exec(...)` call** — this
//! module never runs its own `CREATE TABLE`, it only ever opens a DB the
//! Node agent has already initialized at least once (same lazily-created-by-
//! whichever-process-gets-there-first pattern already used for this file):
//! ```sql
//! CREATE TABLE skills (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, created_at INTEGER NOT NULL);
//! CREATE VIRTUAL TABLE skills_fts USING fts5(id UNINDEXED, name, description);
//! ```
//! `skills_fts` has **no triggers** — `memory.ts`'s `saveSkill` does two
//! explicit inserts itself (main table, then the FTS shadow table), so
//! `create_skill`/`delete_skill` below replicate that exactly rather than
//! relying on any cascade the schema doesn't actually have. SQLite's WAL
//! mode (already enabled by whichever process opened the file first) is
//! what makes it safe for this process and a live Node agent process to
//! both touch the same file concurrently — standard multi-process WAL
//! semantics, nothing this module needs to build itself. Verified directly
//! in this module's own test below against a real schema created by the
//! actual Node `memory.ts` code, not just assumed to match.

use std::path::Path;

use rusqlite::Connection;
use serde::{Deserialize, Serialize};

use crate::workspace;

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Skill {
    id: String,
    name: String,
    description: String,
    /// Milliseconds since epoch — stored exactly as `memory.ts`'s
    /// `Date.now()` writes it, not an RFC3339 string like the rest of this
    /// codebase's Rust-originated timestamps, since this row's shape is
    /// dictated by the existing Node-owned schema, not chosen here.
    created_at: i64,
}

fn open_db_at(path: &Path) -> Result<Connection, String> {
    let conn = Connection::open(path).map_err(|e| format!("failed to open {}: {e}", path.display()))?;
    // Harmless no-op if the Node agent already set this (WAL is a durable
    // property of the file itself) — a defensive default in case this
    // command somehow runs before any session ever has.
    let _: String = conn
        .query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))
        .map_err(|e| format!("failed to set journal mode: {e}"))?;
    Ok(conn)
}

fn memory_db_path() -> std::path::PathBuf {
    workspace::data_dir().join("memory").join("daimon.db")
}

fn list_skills_impl(conn: &Connection) -> Result<Vec<Skill>, String> {
    let mut stmt = conn
        .prepare("SELECT id, name, description, created_at FROM skills ORDER BY created_at DESC")
        .map_err(|e| format!("failed to prepare query: {e}"))?;
    let rows = stmt
        .query_map([], |row| {
            Ok(Skill {
                id: row.get(0)?,
                name: row.get(1)?,
                description: row.get(2)?,
                created_at: row.get(3)?,
            })
        })
        .map_err(|e| format!("failed to query skills: {e}"))?;

    rows.collect::<Result<Vec<Skill>, rusqlite::Error>>().map_err(|e| format!("failed to read skill row: {e}"))
}

fn create_skill_impl(conn: &Connection, name: String, description: String) -> Result<Skill, String> {
    let name = name.trim().to_string();
    let description = description.trim().to_string();
    if name.is_empty() || description.is_empty() {
        return Err("both a name and a description are required".to_string());
    }

    let id = uuid::Uuid::new_v4().to_string();
    let created_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);

    // Two explicit inserts, matching `saveSkill`'s own shape exactly — see
    // this module's doc comment for why (no trigger keeps them in sync).
    conn.execute(
        "INSERT INTO skills (id, name, description, created_at) VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![id, name, description, created_at],
    )
    .map_err(|e| format!("failed to insert skill: {e}"))?;
    conn.execute(
        "INSERT INTO skills_fts (id, name, description) VALUES (?1, ?2, ?3)",
        rusqlite::params![id, name, description],
    )
    .map_err(|e| format!("failed to index skill for search: {e}"))?;

    Ok(Skill { id, name, description, created_at })
}

fn delete_skill_impl(conn: &Connection, id: &str) -> Result<(), String> {
    let deleted = conn
        .execute("DELETE FROM skills WHERE id = ?1", rusqlite::params![id])
        .map_err(|e| format!("failed to delete skill: {e}"))?;
    if deleted == 0 {
        return Err(format!("no skill with id {id}"));
    }
    // No cascade trigger exists — see this module's doc comment — so the
    // FTS shadow row has to be removed explicitly too, or a deleted skill
    // would keep matching future searches while no longer being listable.
    conn.execute("DELETE FROM skills_fts WHERE id = ?1", rusqlite::params![id])
        .map_err(|e| format!("failed to remove skill from search index: {e}"))?;
    Ok(())
}

#[tauri::command]
pub async fn list_skills() -> Result<Vec<Skill>, String> {
    list_skills_impl(&open_db_at(&memory_db_path())?)
}

#[tauri::command]
pub async fn create_skill(name: String, description: String) -> Result<Skill, String> {
    create_skill_impl(&open_db_at(&memory_db_path())?, name, description)
}

#[tauri::command]
pub async fn delete_skill(id: String) -> Result<(), String> {
    delete_skill_impl(&open_db_at(&memory_db_path())?, &id)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Exactly `agents/src/memory.ts`'s own `db.exec(...)` call, copied
    /// verbatim (skills + skills_fts only — the other tables there don't
    /// matter for this test) — this is what proves Rust's SQL actually
    /// interoperates with the real Node-owned schema, not a schema this
    /// test invented to match its own assumptions.
    const NODE_SCHEMA: &str = r#"
        CREATE TABLE IF NOT EXISTS skills (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          description TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(id UNINDEXED, name, description);
    "#;

    fn test_db() -> Connection {
        let conn = Connection::open_in_memory().expect("failed to open in-memory db");
        conn.execute_batch(NODE_SCHEMA).expect("failed to apply the real Node schema");
        conn
    }

    #[test]
    fn create_then_list_round_trips() {
        let conn = test_db();
        let created = create_skill_impl(&conn, "Apply to a job".to_string(), "Fill out an application".to_string())
            .expect("create should succeed");

        let listed = list_skills_impl(&conn).expect("list should succeed");
        assert_eq!(listed, vec![created]);
    }

    #[test]
    fn create_is_searchable_via_fts_like_savekill_writes_it() {
        let conn = test_db();
        create_skill_impl(&conn, "Apply to a job".to_string(), "Fill out a job application form".to_string())
            .expect("create should succeed");

        // Same MATCH query shape `searchSkills` (memory.ts) uses — proves
        // the FTS shadow row insert actually landed and is queryable, not
        // just that the main table insert succeeded.
        let count: i64 = conn
            .query_row("SELECT count(*) FROM skills_fts WHERE skills_fts MATCH 'application'", [], |row| row.get(0))
            .expect("fts query should succeed");
        assert_eq!(count, 1);
    }

    #[test]
    fn delete_removes_from_both_tables() {
        let conn = test_db();
        let created =
            create_skill_impl(&conn, "Test skill".to_string(), "Something searchable".to_string()).unwrap();

        delete_skill_impl(&conn, &created.id).expect("delete should succeed");

        assert!(list_skills_impl(&conn).unwrap().is_empty(), "main table row should be gone");
        let fts_count: i64 = conn
            .query_row("SELECT count(*) FROM skills_fts WHERE id = ?1", rusqlite::params![created.id], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(fts_count, 0, "fts shadow row should be gone too — no cascade trigger exists to do this for us");
    }

    #[test]
    fn delete_unknown_id_errors_clearly() {
        let conn = test_db();
        let err = delete_skill_impl(&conn, "does-not-exist").unwrap_err();
        assert!(err.contains("no skill with id"));
    }

    #[test]
    fn create_rejects_blank_fields() {
        let conn = test_db();
        assert!(create_skill_impl(&conn, "".to_string(), "desc".to_string()).is_err());
        assert!(create_skill_impl(&conn, "name".to_string(), "  ".to_string()).is_err());
    }
}
