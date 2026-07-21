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

export function saveSkill(name: string, description: string): void {
  const id = randomUUID();
  const createdAt = Date.now();
  db.prepare("INSERT INTO skills (id, name, description, created_at) VALUES (?, ?, ?, ?)").run(
    id,
    name,
    description,
    createdAt,
  );
  db.prepare("INSERT INTO skills_fts (id, name, description) VALUES (?, ?, ?)").run(id, name, description);
}

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

export function formatMemoryContext(tasks: TaskMemory[], skills: SkillMemory[]): string {
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
  return parts.join("\n\n");
}
