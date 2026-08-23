/** Turning an OS drag-and-drop into files the vault can be given.
 *
 *  Two things here fail *partially and silently*, which is worse than failing
 *  loudly, so both are handled deliberately and neither should be simplified
 *  away:
 *
 *  1. A `DataTransfer` is neutered the moment the drop handler returns. Every
 *     item has to be turned into a `FileSystemEntry` before the first `await`
 *     — the entries stay valid afterwards, the `DataTransfer` does not.
 *  2. `readEntries()` returns **at most 100 entries per call** and must be
 *     called until it answers with an empty array. Read it once and a
 *     300-file folder imports exactly 100, with no error anywhere.
 *
 *  This works at all because `dragDropEnabled: false` in `tauri.conf.json`
 *  leaves OS drops to the webview instead of Tauri's native handler.
 */

export interface DroppedFile {
  /** Path relative to the drop target, keeping any folder structure. */
  relPath: string;
  file: File;
}

export interface DroppedTree {
  files: DroppedFile[];
  /** Every directory encountered, so empty ones survive the import — the same
   *  reason the vault lists folders separately from notes. */
  folders: string[];
}

/** Names the vault has no use for and the server would refuse anyway. */
function skippable(name: string): boolean {
  return name.startsWith(".");
}

function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => {
    const all: FileSystemEntry[] = [];
    const next = () =>
      reader.readEntries((batch) => {
        // An empty batch is the only end-of-directory signal there is.
        if (batch.length === 0) return resolve(all);
        all.push(...batch);
        next();
      }, reject);
    next();
  });
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function walk(entry: FileSystemEntry, prefix: string, out: DroppedTree): Promise<void> {
  if (skippable(entry.name)) return;
  const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    try {
      out.files.push({ relPath, file: await fileOf(entry as FileSystemFileEntry) });
    } catch {
      /* one unreadable file must not abandon the rest of the drop */
    }
    return;
  }
  if (!entry.isDirectory) return;
  out.folders.push(relPath);
  const children = await readAllEntries((entry as FileSystemDirectoryEntry).createReader());
  for (const child of children) await walk(child, relPath, out);
}

/**
 * Collect a drop into a flat list of files plus the folders they came in.
 *
 * Call this with the `DataTransfer` still live — it harvests the entries
 * synchronously before doing any async work.
 */
export function collectDrop(transfer: DataTransfer): Promise<DroppedTree> {
  // Synchronous harvest. Everything after this point is safe.
  const entries: FileSystemEntry[] = [];
  const plain: File[] = [];
  for (const item of Array.from(transfer.items)) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry?.();
    if (entry) entries.push(entry);
    else {
      // No entry API — a flat file list is all that's on offer.
      const file = item.getAsFile();
      if (file) plain.push(file);
    }
  }

  const out: DroppedTree = { files: [], folders: [] };
  for (const file of plain) {
    if (!skippable(file.name)) out.files.push({ relPath: file.name, file });
  }
  return (async () => {
    for (const entry of entries) await walk(entry, "", out);
    return out;
  })();
}

/**
 * A name that doesn't collide, Finder-style: `shot.png` → `shot 1.png`.
 *
 * Never overwrite. The vault holds the only copy of whatever is already
 * there, a drop is one gesture, and Tauri's webview has no `window.confirm`
 * to ask with — so the safe resolution is the automatic one.
 *
 * `taken` must include names claimed earlier in the same batch, or dropping
 * two files of the same name would hand both the same answer.
 */
export function uniqueName(desired: string, taken: Set<string>): string {
  if (!taken.has(desired)) return desired;
  const slash = desired.lastIndexOf("/");
  const dir = slash === -1 ? "" : desired.slice(0, slash + 1);
  const base = desired.slice(slash + 1);
  const dot = base.lastIndexOf(".");
  const stem = dot <= 0 ? base : base.slice(0, dot);
  const ext = dot <= 0 ? "" : base.slice(dot);
  for (let n = 1; ; n++) {
    const candidate = `${dir}${stem} ${n}${ext}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/** Whether a drag carries OS files, as opposed to a note being moved inside
 *  the tree. The two drops mean different things and must not be confused. */
export function isFileDrag(transfer: DataTransfer | null): boolean {
  return !!transfer && Array.from(transfer.types).includes("Files");
}
