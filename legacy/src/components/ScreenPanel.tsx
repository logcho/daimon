import { useEffect, useRef, useState } from "react";
import { getRecordingThumbnail, listRecordings, readRecordingFile } from "../lib/api";
import type { RecordingFile, RecordingThumbnail } from "../types";

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

// A short, human-friendly title in place of the raw `page@<hash>.webm`
// filename. The real filename is still used for every file operation and
// shown as a tooltip on the card.
function titleFromModifiedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Recording";
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `Recording — ${day}, ${time}`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${(total % 60).toString().padStart(2, "0")}`;
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 translate-x-[1px] fill-white">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

// One tile in the live-view grid — one per currently-live session, not just
// the active one, so multiple concurrent sessions can all be watched at once
// (PipelinePanel computes this across the full `sessions` array).
export interface LiveScreen {
  sessionId: string;
  label: string;
  liveFrame: string;
}

function LiveScreens({ screens }: { screens: LiveScreen[] }) {
  if (screens.length === 0) return null;

  return (
    <div className="mb-5">
      <div className="mb-2 flex items-center gap-1.5">
        <span className="h-1.5 w-1.5 shrink-0 animate-daimon-pulse rounded-full bg-red-400" />
        <span className="text-xs font-medium text-red-400">
          {screens.length === 1
            ? "live — watching the background browser"
            : `live — ${screens.length} sessions running`}
        </span>
      </div>
      <div className={screens.length === 1 ? "" : "grid grid-cols-2 gap-3"}>
        {screens.map((screen) => (
          <div key={screen.sessionId}>
            <img
              src={`data:image/png;base64,${screen.liveFrame}`}
              alt={`Live view of ${screen.label || "a background browser session"}`}
              className="w-full rounded-xl border border-white/10 bg-black/40"
            />
            {screens.length > 1 && (
              <p className="mt-1 truncate text-xs text-neutral-400" title={screen.label}>
                {screen.label || "untitled session"}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function RecordingsGallery({ refreshSignal }: { refreshSignal: number }) {
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [files, setFiles] = useState<RecordingFile[]>([]);
  const [error, setError] = useState("");
  const [thumbnails, setThumbnails] = useState<Record<string, RecordingThumbnail>>({});
  // Which files have had a thumbnail request *started* (not just finished) —
  // a ref, not state, so one thumbnail arriving doesn't re-fire the effect and
  // duplicate requests for everything still in flight.
  const started = useRef<Set<string>>(new Set());

  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [videoDataUri, setVideoDataUri] = useState<string | null>(null);
  const [detailError, setDetailError] = useState("");

  // Refetches on mount (this component only exists while the screen tab is
  // active) and whenever a turn completes — a recording is only written once
  // `finish_recording` runs, and there's no dedicated "recording saved" event,
  // so without the signal a recording saved while sitting on this tab would
  // never appear.
  useEffect(() => {
    listRecordings()
      .then((f) => {
        setFiles(f);
        setState("ready");
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
        setState("error");
      });
  }, [refreshSignal]);

  // One cheap IPC call per file. This used to download every recording in
  // full — `readRecordingFile` base64-encodes the whole `.webm` — purely to
  // seek a detached <video> and draw one frame to a canvas. ffmpeg now does it
  // host-side and caches the result, so opening this tab costs a few tens of
  // KB instead of every video the user has ever recorded.
  useEffect(() => {
    let cancelled = false;
    for (const file of files) {
      if (started.current.has(file.name)) continue;
      started.current.add(file.name);
      getRecordingThumbnail(file.name)
        .then((thumb) => {
          if (!cancelled) setThumbnails((prev) => ({ ...prev, [file.name]: thumb }));
        })
        .catch(() => {
          // Best-effort: a corrupt or still-being-written recording keeps its
          // placeholder rather than blocking the rest of the grid.
        });
    }
    return () => {
      cancelled = true;
    };
  }, [files]);

  function open(name: string) {
    setSelected(name);
    setDetail("loading");
    readRecordingFile(name)
      .then((base64) => {
        // A plain data: URI, not a Blob/object URL — WKWebView (what Tauri
        // uses on macOS) has documented, longstanding bugs specifically with
        // `blob:` URLs in <video> elements.
        setVideoDataUri(`data:video/webm;base64,${base64}`);
        setDetail("ready");
      })
      .catch((err) => {
        setDetailError(err instanceof Error ? err.message : String(err));
        setDetail("error");
      });
  }

  if (selected) {
    return (
      <>
        <button
          type="button"
          onClick={() => {
            setSelected(null);
            setDetail("idle");
            setVideoDataUri(null);
            setDetailError("");
          }}
          className="text-xs text-neutral-500 transition hover:text-neutral-200"
        >
          ← back to recordings
        </button>
        <h3 className="mt-3 text-sm font-semibold tracking-tight text-neutral-100">
          {titleFromModifiedAt(files.find((f) => f.name === selected)?.modifiedAt ?? "")}
        </h3>
        {detail === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
        {detail === "error" && <p className="mt-3 text-xs text-red-400">{detailError}</p>}
        {detail === "ready" && videoDataUri && (
          <video
            controls
            autoPlay
            src={videoDataUri}
            className="mt-3 w-full rounded-xl border border-white/10 bg-black/40"
          />
        )}
      </>
    );
  }

  return (
    <>
      <h3 className="text-sm font-semibold tracking-tight text-neutral-100">recordings</h3>

      {state === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
      {state === "error" && <p className="mt-3 text-xs text-red-400">{error}</p>}
      {state === "ready" && files.length === 0 && (
        <p className="mt-3 text-xs leading-relaxed text-neutral-500">
          ○ no recordings yet — ask daimon to record what it's doing (e.g. "go to X and show me what
          you did"), or watch live above while a task is running.
        </p>
      )}
      {state === "ready" && files.length > 0 && (
        <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-4">
          {files.map((file) => {
            const thumb = thumbnails[file.name];
            const duration = thumb ? formatDuration(thumb.durationSeconds) : "";
            return (
              <button
                key={file.name}
                type="button"
                onClick={() => open(file.name)}
                title={file.name}
                className="group text-left"
              >
                <div className="relative aspect-video overflow-hidden rounded-xl border border-white/10 bg-black/40">
                  {thumb ? (
                    <img src={`data:image/jpeg;base64,${thumb.data}`} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <div className="h-full w-full animate-pulse bg-white/[0.04]" />
                  )}
                  <div className="absolute inset-0 flex items-center justify-center bg-black/0 transition duration-200 group-hover:bg-black/25">
                    <div className="flex h-9 w-9 items-center justify-center rounded-full bg-black/60 opacity-0 shadow-lg transition duration-200 group-hover:opacity-100">
                      <PlayIcon />
                    </div>
                  </div>
                  {duration && (
                    <span className="absolute bottom-1 right-1 rounded bg-black/80 px-1 py-0.5 text-[10px] font-medium text-white">
                      {duration}
                    </span>
                  )}
                </div>
                <p className="mt-1.5 truncate text-sm font-medium text-neutral-100">
                  {titleFromModifiedAt(file.modifiedAt)}
                </p>
                <p className="text-xs text-neutral-500">
                  {formatBytes(file.sizeBytes)} · {new Date(file.modifiedAt).toLocaleString()}
                </p>
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}

export function ScreenPanel({
  screens,
  recordingsRefreshSignal,
}: {
  screens: LiveScreen[];
  recordingsRefreshSignal: number;
}) {
  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <LiveScreens screens={screens} />
      <RecordingsGallery refreshSignal={recordingsRefreshSignal} />
    </div>
  );
}
