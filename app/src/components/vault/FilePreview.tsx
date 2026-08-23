import { useState } from "react";
import { MAX_IMPORT_BYTES, type FileKind } from "../../fileKind";
import { assetUrl } from "../../vaultAsset";
import { FileIcon } from "./FileIcon";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex++;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

export interface FilePreviewProps {
  name: string;
  kind: FileKind;
  sizeBytes: number;
  modifiedAt: string;
  /** The vault's absolute path — media streams off disk, not through IPC. */
  vaultDir: string;
  onReveal: () => void;
}

/** Everything the panel shows that isn't editable text.
 *
 *  Each branch is a plain HTML element pointed at an `asset://` URL, which is
 *  deliberate: the browser engine already knows how to decode a JPEG, scrub an
 *  MP4 and paginate a PDF, and every one of those done in JS would be worse.
 */
export function FilePreview({
  name,
  kind,
  sizeBytes,
  modifiedAt,
  vaultDir,
  onReveal,
}: FilePreviewProps) {
  const [zoomed, setZoomed] = useState(false);

  // Until the vault path has arrived there is nothing to point at. It comes
  // from the agent config, so this is a blink on first open, not a failure.
  if (!vaultDir) return <p className="text-sm text-neutral-500">loading…</p>;

  const src = assetUrl(vaultDir, name, modifiedAt);

  switch (kind) {
    case "image":
      return (
        <div className="flex flex-col gap-2">
          <img
            src={src}
            alt={name}
            onClick={() => setZoomed((z) => !z)}
            title={zoomed ? "click to fit" : "click for actual size"}
            className={`rounded-md border border-white/10 bg-black/20 ${
              zoomed ? "max-w-none cursor-zoom-out" : "max-h-[70vh] max-w-full cursor-zoom-in object-contain"
            }`}
          />
        </div>
      );

    case "pdf":
      // An iframe rather than <embed>: WKWebView's own PDF view handles
      // paging and search, and reimplementing either would be worse.
      return (
        <iframe
          src={src}
          title={name}
          className="h-[75vh] w-full rounded-md border border-white/10 bg-black/20"
        />
      );

    case "audio":
      return <audio src={src} controls className="w-full" />;

    case "video":
      return (
        <video
          src={src}
          controls
          className="max-h-[70vh] w-full rounded-md border border-white/10 bg-black"
        />
      );

    default:
      return (
        <NoPreview
          name={name}
          kind={kind}
          sizeBytes={sizeBytes}
          onReveal={onReveal}
          reason={
            sizeBytes > MAX_IMPORT_BYTES
              ? "too large to open here"
              : `no preview for ${name.slice(name.lastIndexOf(".") + 1)} files`
          }
        />
      );
  }
}

/** The honest dead end. Says what the file is, how big, and offers the one
 *  thing that always works — opening it where the OS can. */
export function NoPreview({
  name,
  kind,
  sizeBytes,
  reason,
  onReveal,
}: {
  name: string;
  kind: FileKind;
  sizeBytes: number;
  reason: string;
  onReveal: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-md border border-white/10 bg-black/20 px-6 py-12 text-center">
      <FileIcon kind={kind} className="h-8 w-8 text-neutral-600" />
      <div>
        <p className="text-sm text-neutral-300">{name.slice(name.lastIndexOf("/") + 1)}</p>
        <p className="mt-1 text-xs text-neutral-500">
          {formatBytes(sizeBytes)} · {reason}
        </p>
      </div>
      <button
        type="button"
        onClick={onReveal}
        className="rounded-md bg-white/5 px-3 py-1 text-xs text-neutral-300 transition hover:bg-white/10 hover:text-neutral-100"
      >
        reveal in Finder
      </button>
    </div>
  );
}
