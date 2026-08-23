/** What a vault file *is*, decided from its name.
 *
 *  One table, because the answer drives four separate things — which icon a
 *  row gets, which preview the pane renders, whether the editor opens, and
 *  whether the backlink scan bothers reading it. Deriving it separately in
 *  each of those is how they end up disagreeing about the same file.
 *
 *  By extension rather than by sniffing the bytes: the listing has only names,
 *  and a tree of a thousand files cannot read a thousand headers to draw
 *  itself.
 */

export type FileKind = "markdown" | "text" | "image" | "pdf" | "audio" | "video" | "binary";

/** Text that opens in the editor. Deliberately generous — anything here that
 *  turns out to be binary still fails safely, because the read route refuses
 *  to decode it and the panel shows that instead of mojibake. */
const TEXT = new Set([
  "txt", "text", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "ini", "conf",
  "env", "xml", "svg", "html", "htm", "css", "scss", "js", "jsx", "ts", "tsx", "mjs", "cjs",
  "py", "rb", "rs", "go", "java", "kt", "swift", "c", "h", "cpp", "hpp", "cs", "php", "lua",
  "sh", "bash", "zsh", "fish", "sql", "graphql", "r", "jl", "tex", "gitignore", "lock",
]);

const IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico", "heic", "tiff", "tif"]);
const AUDIO = new Set(["mp3", "wav", "m4a", "aac", "ogg", "oga", "flac", "opus"]);
const VIDEO = new Set(["mp4", "mov", "m4v", "webm", "mkv", "avi"]);

/** The extension, lowercased, without the dot. `""` for a name that has none. */
export function extensionOf(name: string): string {
  const base = name.slice(name.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  // A leading dot is a hidden file, not an extension — `.gitignore` has no
  // extension, it *is* one.
  return dot <= 0 ? "" : base.slice(dot + 1).toLowerCase();
}

export function kindOf(name: string): FileKind {
  const ext = extensionOf(name);
  if (ext === "md" || ext === "markdown") return "markdown";
  if (IMAGE.has(ext)) return "image";
  if (ext === "pdf") return "pdf";
  if (AUDIO.has(ext)) return "audio";
  if (VIDEO.has(ext)) return "video";
  if (TEXT.has(ext)) return "text";
  return "binary";
}

/** Whether the editor opens on it. Markdown and text save back; everything
 *  else is a viewer. */
export function isEditable(kind: FileKind): boolean {
  return kind === "markdown" || kind === "text";
}

/** Whether it renders through the webview's asset protocol rather than by
 *  being read as text. */
export function isMedia(kind: FileKind): boolean {
  return kind === "image" || kind === "pdf" || kind === "audio" || kind === "video";
}

/** The ceiling the Rust import command enforces (`MAX_IMPORT_BYTES`), repeated
 *  here so the panel can refuse a file before spending a base64 encode on it
 *  and say something useful about why. */
export const MAX_IMPORT_BYTES = 32 * 1024 * 1024;
