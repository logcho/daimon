import { useEffect, useState } from "react";
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

function decodeBase64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

export function ScreenPanel({ isLive, liveFrame }: { isLive: boolean; liveFrame?: string }) {
  const [listState, setListState] = useState<ListState>("loading");
  const [files, setFiles] = useState<RecordingFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
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

  function handleOpenFile(name: string) {
    setSelectedFile(name);
    setDetailState("loading");
    readRecordingFile(name)
      .then((base64) => {
        const bytes = decodeBase64ToBytes(base64);
        // A fresh object URL per selection — revoked below (on unmount or
        // before the next selection replaces it) so repeatedly opening
        // different recordings in one visit to this tab doesn't leak a
        // growing pile of blob URLs for the lifetime of the webview.
        const url = URL.createObjectURL(new Blob([bytes], { type: "video/webm" }));
        setVideoUrl((previous) => {
          if (previous) URL.revokeObjectURL(previous);
          return url;
        });
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
    setVideoUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    setDetailErrorMessage("");
  }

  // Revoke whatever object URL is still live if the whole tab unmounts
  // (switching to a different view) while a video is open — otherwise that
  // blob stays retained in memory for the rest of the webview's lifetime.
  useEffect(() => {
    return () => {
      setVideoUrl((current) => {
        if (current) URL.revokeObjectURL(current);
        return current;
      });
    };
  }, []);

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
        <h3 className="mt-3 text-sm font-semibold tracking-tight text-neutral-100">{selectedFile}</h3>
        {detailState === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
        {detailState === "error" && <p className="mt-3 text-xs text-red-400">{detailErrorMessage}</p>}
        {detailState === "ready" && videoUrl && (
          <video
            controls
            autoPlay
            src={videoUrl}
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
        <ul className="mt-3 space-y-1.5">
          {files.map((file) => (
            <li key={file.name}>
              <button
                type="button"
                onClick={() => handleOpenFile(file.name)}
                className="liquid-glass-subtle flex w-full items-center justify-between rounded-xl px-3 py-2 text-left transition duration-200 hover:[border-color:rgba(255,255,255,0.25)] active:scale-[0.99]"
              >
                <span className="truncate text-sm font-medium text-neutral-100">{file.name}</span>
                <span className="ml-3 shrink-0 text-xs text-neutral-500">
                  {formatBytes(file.sizeBytes)} · {formatModifiedAt(file.modifiedAt)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
