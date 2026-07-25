import { useEffect, useRef, useState } from "react";
import { listRecordings, readRecordingFile } from "../lib/api";
import type { RecordingFile } from "../types";

type ListState = "loading" | "ready" | "error";
type DetailState = "idle" | "loading" | "ready" | "error";

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

function formatModifiedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

// A short, human-friendly title in place of the raw `page@<hash>.webm`
// filename — reads like a real video title rather than an opaque hash. The
// real filename is still used for every actual file operation and shown as
// a tooltip (`title` attribute) on the card.
function titleFromModifiedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Recording";
  return `Recording — ${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.round(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

interface ThumbnailInfo {
  dataUrl: string;
  durationSeconds: number;
}

// Extracts one representative frame (skipping a hair past the very start,
// which is often black/blank right as a recording begins) as a JPEG data
// URL, plus the video's duration — both read off a single hidden <video>
// element that's never attached to the DOM. There's no server-side/ffmpeg
// thumbnail step in this app, so this is the only way to get a real frame
// without shipping a new native dependency; cheap enough for the short
// task-demonstration clips this feature is meant for.
function extractThumbnail(videoDataUri: string): Promise<ThumbnailInfo> {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    video.src = videoDataUri;

    function cleanup() {
      video.removeEventListener("loadedmetadata", onLoadedMetadata);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("error", onError);
      video.src = "";
    }

    function onError() {
      cleanup();
      reject(new Error("failed to load video for thumbnail extraction"));
    }

    function onLoadedMetadata() {
      video.currentTime = Math.min(0.3, Math.max(0, video.duration - 0.05));
    }

    function onSeeked() {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth || 320;
      canvas.height = video.videoHeight || 180;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        cleanup();
        reject(new Error("canvas 2d context unavailable"));
        return;
      }
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const info: ThumbnailInfo = { dataUrl: canvas.toDataURL("image/jpeg", 0.75), durationSeconds: video.duration };
      cleanup();
      resolve(info);
    }

    video.addEventListener("loadedmetadata", onLoadedMetadata);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("error", onError);
  });
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 translate-x-[1px] fill-white">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

export function ScreenPanel({ isLive, liveFrame }: { isLive: boolean; liveFrame?: string }) {
  const [listState, setListState] = useState<ListState>("loading");
  const [files, setFiles] = useState<RecordingFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");
  const [thumbnails, setThumbnails] = useState<Record<string, ThumbnailInfo>>({});
  // Tracks which files have already had a thumbnail extraction *started*
  // (not just completed) — a plain ref, not state, so re-renders triggered
  // by one file's thumbnail arriving don't cause this effect to re-fire and
  // kick off duplicate fetches for every other file still in flight.
  const startedThumbnailsRef = useRef<Set<string>>(new Set());

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [videoDataUri, setVideoDataUri] = useState<string | null>(null);
  const [detailErrorMessage, setDetailErrorMessage] = useState("");

  // Remounts each time the screen tab becomes active (PipelinePanel only
  // renders this component while `view === "screen"`), same "refetch on
  // every switch" pattern as VaultPanel/AutomationsPanel — recordings are
  // meant to reflect whatever the agent has just saved, not a one-time
  // snapshot from whenever this tab was first opened.
  useEffect(() => {
    listRecordings()
      .then((f) => {
        setFiles(f);
        setListState("ready");
      })
      .catch((err) => {
        setListErrorMessage(err instanceof Error ? err.message : String(err));
        setListState("error");
      });
  }, []);

  // Lazily generates a thumbnail (+ duration) for every listed recording
  // that hasn't had one started yet. Each one requires fetching that file's
  // full bytes over IPC — there's no cheaper way to get a real frame — so
  // this is deliberately fire-and-forget per file rather than blocking the
  // list on all of them.
  useEffect(() => {
    let cancelled = false;
    for (const file of files) {
      if (startedThumbnailsRef.current.has(file.name)) continue;
      startedThumbnailsRef.current.add(file.name);
      readRecordingFile(file.name)
        .then((base64) => extractThumbnail(`data:video/webm;base64,${base64}`))
        .then((info) => {
          if (cancelled) return;
          setThumbnails((prev) => ({ ...prev, [file.name]: info }));
        })
        .catch(() => {
          // Best-effort — a file that fails to produce a thumbnail (e.g. a
          // corrupt/incomplete recording) just keeps its placeholder rather
          // than blocking the rest of the grid.
        });
    }
    return () => {
      cancelled = true;
    };
  }, [files]);

  function handleOpenFile(name: string) {
    setSelectedFile(name);
    setDetailState("loading");
    readRecordingFile(name)
      .then((base64) => {
        // A plain data: URI, not a Blob/object URL — WKWebView (what Tauri
        // uses on macOS) has documented, longstanding bugs specifically with
        // `blob:` URLs in <video> elements; a data URI sidesteps that bug
        // class entirely, and costs nothing extra here since the base64
        // payload is already in hand from the IPC call.
        setVideoDataUri(`data:video/webm;base64,${base64}`);
        setDetailState("ready");
      })
      .catch((err) => {
        setDetailErrorMessage(err instanceof Error ? err.message : String(err));
        setDetailState("error");
      });
  }

  function handleBack() {
    setSelectedFile(null);
    setDetailState("idle");
    setVideoDataUri(null);
    setDetailErrorMessage("");
  }

  if (selectedFile) {
    return (
      <div className="themed-scroll flex-1 overflow-y-auto p-4">
        <button
          type="button"
          onClick={handleBack}
          className="text-xs text-neutral-500 transition hover:text-neutral-200"
        >
          ← back to recordings
        </button>
        <h3 className="mt-3 text-sm font-semibold tracking-tight text-neutral-100">
          {titleFromModifiedAt(files.find((f) => f.name === selectedFile)?.modifiedAt ?? "")}
        </h3>
        {detailState === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
        {detailState === "error" && <p className="mt-3 text-xs text-red-400">{detailErrorMessage}</p>}
        {detailState === "ready" && videoDataUri && (
          <video
            controls
            autoPlay
            src={videoDataUri}
            className="mt-3 w-full rounded-xl border border-white/10 bg-black/40"
          />
        )}
      </div>
    );
  }

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      {isLive && liveFrame && (
        <div className="mb-5">
          <div className="mb-2 flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 shrink-0 animate-daimon-pulse rounded-full bg-red-400" />
            <span className="text-xs font-medium text-red-400">live — watching the background browser</span>
          </div>
          <img
            src={`data:image/png;base64,${liveFrame}`}
            alt="Live view of the background browser"
            className="w-full rounded-xl border border-white/10 bg-black/40"
          />
        </div>
      )}

      <h3 className="text-sm font-semibold tracking-tight text-neutral-100">recordings</h3>

      {listState === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
      {listState === "error" && <p className="mt-3 text-xs text-red-400">{listErrorMessage}</p>}
      {listState === "ready" && files.length === 0 && (
        <p className="mt-3 text-xs text-neutral-500">
          ○ no recordings yet — ask daimon to record what it's doing (e.g. "go to X and show me
          what you did"), or watch live above while a task is running.
        </p>
      )}
      {listState === "ready" && files.length > 0 && (
        <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-4">
          {files.map((file) => {
            const thumb = thumbnails[file.name];
            return (
              <button
                key={file.name}
                type="button"
                onClick={() => handleOpenFile(file.name)}
                title={file.name}
                className="group text-left"
              >
                <div className="relative aspect-video overflow-hidden rounded-xl border border-white/10 bg-black/40">
                  {thumb ? (
                    <img src={thumb.dataUrl} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <div className="h-full w-full animate-pulse bg-white/[0.04]" />
                  )}
                  <div className="absolute inset-0 flex items-center justify-center bg-black/0 transition duration-200 group-hover:bg-black/25">
                    <div className="flex h-9 w-9 items-center justify-center rounded-full bg-black/60 opacity-0 shadow-lg transition duration-200 group-hover:opacity-100">
                      <PlayIcon />
                    </div>
                  </div>
                  {thumb && thumb.durationSeconds > 0 && (
                    <span className="absolute bottom-1 right-1 rounded bg-black/80 px-1 py-0.5 text-[10px] font-medium text-white">
                      {formatDuration(thumb.durationSeconds)}
                    </span>
                  )}
                </div>
                <p className="mt-1.5 truncate text-sm font-medium text-neutral-100">
                  {titleFromModifiedAt(file.modifiedAt)}
                </p>
                <p className="text-xs text-neutral-500">
                  {formatBytes(file.sizeBytes)} · {formatModifiedAt(file.modifiedAt)}
                </p>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
