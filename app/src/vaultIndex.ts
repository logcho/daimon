/** The note-text cache backlinks are computed from.
 *
 *  Module-level, not React state, and that is the whole point. `Panel.tsx`
 *  renders the vault as `{view === "vault" && <VaultPanel />}`, so the panel
 *  *unmounts* every time you look at the chat — and a cache in `useState` went
 *  with it, restarting a read-every-note scan on every visit to the tab. This
 *  survives the unmount; a second visit costs nothing.
 *
 *  Keyed on `modifiedAt` so a note the agent rewrote is re-read and one that
 *  did not change is not. Markdown only: a backlink is a markdown idea, and
 *  asking the server for a PNG's text now correctly fails, so scanning
 *  everything would be n wasted round trips ending in n 415s.
 */

import { readVaultFile } from "./api";
import { kindOf } from "./fileKind";
import type { VaultFile } from "./types";

const texts = new Map<string, { text: string; modifiedAt: string }>();

/** Files this big are not prose. Reading one costs more than any backlink it
 *  could contribute is worth. */
const MAX_SCAN_BYTES = 1024 * 1024;

/** A ceiling on a cold scan. A real Obsidian vault can hold tens of thousands
 *  of notes, and this is still one request per note — the honest fix is a
 *  server-side link index over the FTS table that already holds every body
 *  (see `agents/src/daimon_agent/memory.py`), which is a bigger change than
 *  this panel. Until then, stop somewhere rather than hang. */
const MAX_SCAN_FILES = 2000;

/** How many reads are in flight at once. Sequential made a cold scan feel
 *  broken; unbounded opens a socket per note. */
const CONCURRENCY = 4;

/** What's cached right now, in the shape `findBacklinks` wants. */
export function noteTexts(): Map<string, string> {
  const out = new Map<string, string>();
  for (const [name, entry] of texts) out.set(name, entry.text);
  return out;
}

export function forgetNote(name: string): void {
  texts.delete(name);
}

/** Keep the cache in step with a fresh listing, in the background.
 *
 *  Best-effort throughout: a note that won't read just doesn't contribute
 *  backlinks, which beats failing the panel over it. `onProgress` fires as
 *  bodies land so the backlinks list fills in rather than appearing at the
 *  end. Returns a cancel function — the caller is a `useEffect`, and a scan
 *  that outlived its panel would call `setState` on a dead component.
 */
export function indexNotes(files: VaultFile[], onProgress: () => void): () => void {
  let cancelled = false;

  const stale = files
    .filter((file) => kindOf(file.name) === "markdown" && file.sizeBytes <= MAX_SCAN_BYTES)
    .filter((file) => texts.get(file.name)?.modifiedAt !== file.modifiedAt)
    .slice(0, MAX_SCAN_FILES);

  // Names that vanished from the listing — dropping them keeps a deleted note
  // from going on contributing backlinks to notes that no longer link to it.
  const live = new Set(files.map((file) => file.name));
  for (const name of [...texts.keys()]) {
    if (!live.has(name)) texts.delete(name);
  }

  if (stale.length === 0) return () => {};

  let cursor = 0;
  async function worker() {
    while (!cancelled) {
      const file = stale[cursor++];
      if (!file) return;
      try {
        const text = await readVaultFile(file.name);
        if (cancelled) return;
        texts.set(file.name, { text, modifiedAt: file.modifiedAt });
        onProgress();
      } catch {
        /* skip — one unreadable note must not stop the scan */
      }
    }
  }
  void Promise.all(Array.from({ length: CONCURRENCY }, worker));

  return () => {
    cancelled = true;
  };
}

/** Record a body the panel already has in hand — an open note, or one just
 *  saved — so the scan doesn't fetch what was on screen a moment ago. */
export function rememberNote(name: string, text: string, modifiedAt: string): void {
  texts.set(name, { text, modifiedAt });
}
