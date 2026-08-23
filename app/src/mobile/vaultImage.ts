/** Fetching an image a note references, on a device with no filesystem.
 *
 *  The desktop points an `<img>` at an `asset://` URL and lets the webview
 *  stream the file off disk. A phone is on the other side of a tailnet with no
 *  such path, so the bytes come down the bus socket it already has —
 *  authenticated, and the only door it has anyway. That makes each image one
 *  round trip and one base64 payload, which is why this caches per name and
 *  why the server caps what it will send.
 */

import type { BusClient } from "../lib/busClient";

/** Resolved object URLs, keyed by vault path. Module-level so flipping
 *  between two notes that share an image doesn't refetch it. */
const cache = new Map<string, string>();
/** In-flight requests, so a note referencing the same image three times
 *  fetches it once. */
const pending = new Map<string, Promise<string | null>>();

/** Candidate vault paths for a `src` written inside `fromFolder`.
 *
 *  Relative to the note first, the way any markdown tool resolves it, then the
 *  vault root — which is where a bare `![](cat.jpeg)` most often means, since
 *  the note and the image were dropped in together. No basename search: the
 *  phone's listing is markdown-only, so it has no index of image names to
 *  search, and guessing across folders is how you show the wrong picture.
 */
export function candidatePaths(src: string, fromFolder: string): string[] {
  const clean = src.replace(/^\.\//, "").replace(/^\/+/, "");
  const out: string[] = [];
  if (fromFolder) {
    // `../` walks up; let the URL machinery normalise rather than doing
    // segment arithmetic by hand.
    out.push(decodeURIComponent(new URL(clean, `file:///${fromFolder}/`).pathname).replace(/^\/+/, ""));
  }
  const bare = decodeURIComponent(clean);
  if (!out.includes(bare)) out.push(bare);
  return out;
}

/** An object URL for the image, or null when nothing matches. */
export function loadVaultImage(
  bus: BusClient,
  workspace: string,
  src: string,
  fromFolder: string,
): Promise<string | null> {
  const key = `${fromFolder}::${src}`;
  const hit = cache.get(key);
  if (hit) return Promise.resolve(hit);
  const inFlight = pending.get(key);
  if (inFlight) return inFlight;

  const work = (async () => {
    for (const name of candidatePaths(src, fromFolder)) {
      const ack = await bus.send("vault.blob", { workspace, name });
      if (!ack.ok) continue;
      const bytes = Uint8Array.from(atob(String(ack.base64)), (c) => c.charCodeAt(0));
      // A blob URL rather than a `data:` one: the base64 string is already
      // the largest thing in memory, and this lets it be dropped.
      const url = URL.createObjectURL(new Blob([bytes], { type: String(ack.mime) }));
      cache.set(key, url);
      return url;
    }
    return null;
  })().finally(() => pending.delete(key));

  pending.set(key, work);
  return work;
}

/** Drop everything — for a vault change, where an image may have been
 *  replaced under the same name. */
export function forgetVaultImages(): void {
  for (const url of cache.values()) URL.revokeObjectURL(url);
  cache.clear();
}
