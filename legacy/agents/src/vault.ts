import fs from "node:fs/promises";
import path from "node:path";

// The in-container mount point the daemon's `run_container` bind-mounts the
// host vault directory onto (see `src-tauri/src/workspace.rs`'s `vault_mount`
// and `src-tauri/src/vault.rs`'s `vault_path`) — either a real Obsidian vault
// the user pointed us at, or Daimon's own default `vault/` folder. Same
// mechanism either way; this module never needs to know which.
export const VAULT_DIR = process.env.DAIMON_VAULT_DIR ?? "/workspace/vault";

/** Reduces an arbitrary filename to a safe basename with a `.md` extension — never trust a string argument for a filesystem path. */
function sanitizeFilename(filename: string): string {
  const base = path.basename(filename).trim();
  if (!base || base === "." || base === "..") {
    throw new Error(`invalid note filename: ${JSON.stringify(filename)}`);
  }
  return base.toLowerCase().endsWith(".md") ? base : `${base}.md`;
}

export async function writeNote(filename: string, content: string): Promise<string> {
  const safeName = sanitizeFilename(filename);
  await fs.mkdir(VAULT_DIR, { recursive: true });
  await fs.writeFile(path.join(VAULT_DIR, safeName), content, "utf-8");
  return safeName;
}

export async function readNote(filename: string): Promise<string> {
  const safeName = sanitizeFilename(filename);
  try {
    return await fs.readFile(path.join(VAULT_DIR, safeName), "utf-8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      throw new Error(`note "${safeName}" does not exist in the vault`);
    }
    throw err;
  }
}

export async function listNotes(): Promise<string[]> {
  await fs.mkdir(VAULT_DIR, { recursive: true });
  const entries = await fs.readdir(VAULT_DIR, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".md"))
    .map((entry) => entry.name);
}
