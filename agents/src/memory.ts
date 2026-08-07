import { randomUUID } from "node:crypto";
import path from "node:path";
import fs from "node:fs";
import Database from "better-sqlite3";

const DB_PATH = process.env.DAIMON_MEMORY_DB ?? path.join(process.cwd(), "memory", "daimon.db");
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");

db.exec(`
  CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    instruction TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
  );
  CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(id UNINDEXED, instruction, result);

  CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at INTEGER NOT NULL
  );
  CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(id UNINDEXED, name, description);

  CREATE TABLE IF NOT EXISTS notes (
    filename TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at INTEGER NOT NULL
  );
  CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(filename UNINDEXED, content);
`);

export interface TaskMemory {
  id: string;
  instruction: string;
  result: string | null;
  status: "done" | "error";
  createdAt: number;
}

export interface SkillMemory {
  id: string;
  name: string;
  description: string;
  createdAt: number;
}

/** Wraps each token in quotes so punctuation in a raw instruction can't be misread as FTS5 query syntax. */
function ftsQuery(raw: string): string {
  const tokens = raw.match(/[\p{L}\p{N}]+/gu) ?? [];
  if (tokens.length === 0) return '""';
  return tokens.map((t) => `"${t}"`).join(" OR ");
}

export function recordTask(instruction: string, result: string | null, status: "done" | "error"): void {
  const id = randomUUID();
  const createdAt = Date.now();
  db.prepare("INSERT INTO tasks (id, instruction, result, status, created_at) VALUES (?, ?, ?, ?, ?)").run(
    id,
    instruction,
    result,
    status,
    createdAt,
  );
  db.prepare("INSERT INTO tasks_fts (id, instruction, result) VALUES (?, ?, ?)").run(
    id,
    instruction,
    result ?? "",
  );
}

export function searchTasks(query: string, limit = 5): TaskMemory[] {
  const matches = db
    .prepare("SELECT id FROM tasks_fts WHERE tasks_fts MATCH ? ORDER BY rank LIMIT ?")
    .all(ftsQuery(query), limit) as { id: string }[];
  if (matches.length === 0) return [];

  const placeholders = matches.map(() => "?").join(",");
  const rows = db
    .prepare(`SELECT * FROM tasks WHERE id IN (${placeholders}) ORDER BY created_at DESC`)
    .all(...matches.map((m) => m.id)) as Array<{
    id: string;
    instruction: string;
    result: string | null;
    status: string;
    created_at: number;
  }>;

  return rows.map((r) => ({
    id: r.id,
    instruction: r.instruction,
    result: r.result,
    status: r.status as TaskMemory["status"],
    createdAt: r.created_at,
  }));
}

// No writer any more: skills used to be a name+description row written by a
// `save_skill` tool and edited through a SkillsPanel tab, both of which are
// gone — a skill is now a real `skills/<name>/SKILL.md` in the vault, which
// the agent writes with the built-in Write tool and the user can read and
// edit in Obsidian. The read path below stays because the `skills` table
// still holds everything saved under the old scheme, and `recall` should keep
// surfacing it rather than pretending that history never happened. Nothing
// new lands here, so the table only shrinks from now on.
export function searchSkills(query: string, limit = 5): SkillMemory[] {
  const matches = db
    .prepare("SELECT id FROM skills_fts WHERE skills_fts MATCH ? ORDER BY rank LIMIT ?")
    .all(ftsQuery(query), limit) as { id: string }[];
  if (matches.length === 0) return [];

  const placeholders = matches.map(() => "?").join(",");
  const rows = db
    .prepare(`SELECT * FROM skills WHERE id IN (${placeholders})`)
    .all(...matches.map((m) => m.id)) as Array<{
    id: string;
    name: string;
    description: string;
    created_at: number;
  }>;

  return rows.map((r) => ({ id: r.id, name: r.name, description: r.description, createdAt: r.created_at }));
}

export interface NoteMemory {
  filename: string;
  content: string;
  updatedAt: number;
}

/**
 * Notes are upserted by filename (a note can be rewritten), unlike the
 * append-only `tasks`/`skills` tables — FTS5 virtual tables don't support
 * `ON CONFLICT`, so the correct upsert is delete-then-insert into both
 * tables here.
 */
export function indexNote(filename: string, content: string): void {
  const updatedAt = Date.now();
  db.prepare(
    "INSERT INTO notes (filename, content, updated_at) VALUES (?, ?, ?) " +
      "ON CONFLICT(filename) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
  ).run(filename, content, updatedAt);
  db.prepare("DELETE FROM notes_fts WHERE filename = ?").run(filename);
  db.prepare("INSERT INTO notes_fts (filename, content) VALUES (?, ?)").run(filename, content);
}

export function searchNotes(query: string, limit = 5): NoteMemory[] {
  const matches = db
    .prepare("SELECT filename FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?")
    .all(ftsQuery(query), limit) as { filename: string }[];
  if (matches.length === 0) return [];

  const placeholders = matches.map(() => "?").join(",");
  const rows = db
    .prepare(`SELECT * FROM notes WHERE filename IN (${placeholders}) ORDER BY updated_at DESC`)
    .all(...matches.map((m) => m.filename)) as Array<{
    filename: string;
    content: string;
    updated_at: number;
  }>;

  return rows.map((r) => ({ filename: r.filename, content: r.content, updatedAt: r.updated_at }));
}

export function formatMemoryContext(tasks: TaskMemory[], skills: SkillMemory[], notes: NoteMemory[]): string {
  const parts: string[] = [];
  if (tasks.length > 0) {
    parts.push(
      "Related past tasks:\n" +
        tasks.map((t) => `- "${t.instruction}" -> ${t.result ?? "(no result)"}`).join("\n"),
    );
  }
  if (skills.length > 0) {
    parts.push(
      "Known reusable skills:\n" + skills.map((s) => `- ${s.name}: ${s.description}`).join("\n"),
    );
  }
  if (notes.length > 0) {
    parts.push(
      "Vault notes:\n" + notes.map((n) => `- ${n.filename}: ${n.content.slice(0, 200)}`).join("\n"),
    );
  }
  return parts.join("\n\n");
}
