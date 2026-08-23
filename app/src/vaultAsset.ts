/** Building URLs the webview can stream a vault file from.
 *
 *  Media does not come through IPC. `convertFileSrc` hands the webview an
 *  `asset://` URL it fetches itself, which is the only path where WKWebView
 *  issues range requests — and without those a `<video>` cannot be seeked at
 *  all. It also means a 200 MB file costs nothing to *display*, because
 *  nothing ever loads it into JS.
 *
 *  The directory is granted to the asset scope at launch (see
 *  `src-tauri/src/lib.rs`); a path outside it is refused by the webview, not
 *  by this function.
 */

import { convertFileSrc } from "@tauri-apps/api/core";

/**
 * @param root      the vault's absolute path, from `vaultRoot()`
 * @param name      the file's vault-relative path
 * @param modifiedAt the file's mtime — the cache key, and it matters
 */
export function assetUrl(root: string, name: string, modifiedAt: string): string {
  const absolute = `${root.replace(/\/+$/, "")}/${name}`;
  // Overwriting a file leaves its path identical, and WKWebView will happily
  // serve the response it already cached — so a replaced image would keep
  // showing the old one. Keying on mtime busts the cache exactly when the
  // bytes changed and never otherwise.
  const version = Date.parse(modifiedAt) || 0;
  return `${convertFileSrc(absolute)}?v=${version}`;
}
