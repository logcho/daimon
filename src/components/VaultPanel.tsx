import { useEffect, useState } from "react";
import { listVaultFiles, readVaultFile } from "../lib/api";
import type { VaultFile } from "../types";

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

export function VaultPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [files, setFiles] = useState<VaultFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [content, setContent] = useState("");
  const [detailErrorMessage, setDetailErrorMessage] = useState("");

  // Remounts each time the vault tab becomes active (PipelinePanel only
  // renders this component while `view === "vault"`), so this mount effect
  // doubles as "refetch on every switch" — deliberate, since the vault is
  // meant to reflect the agent's live activity, not a one-time snapshot.
  useEffect(() => {
    listVaultFiles()
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
    readVaultFile(name)
      .then((text) => {
        setContent(text);
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
    setContent("");
    setDetailErrorMessage("");
  }

  if (selectedFile) {
    return (
      <div className="themed-scroll flex-1 overflow-y-auto p-4">
        <button
          type="button"
          onClick={handleBack}
          className="font-mono text-xs text-neutral-500 transition hover:text-neutral-200"
        >
          ← back to vault
        </button>
        <h3 className="mt-3 font-mono text-sm text-neutral-100">{selectedFile}</h3>
        {detailState === "loading" && (
          <p className="mt-3 font-mono text-xs text-neutral-500">loading…</p>
        )}
        {detailState === "error" && (
          <p className="mt-3 font-mono text-xs text-red-400">{detailErrorMessage}</p>
        )}
        {detailState === "ready" && (
          <pre className="mt-3 whitespace-pre-wrap rounded-lg border border-white/10 bg-white/5 p-3 font-mono text-xs leading-relaxed text-neutral-300">
            {content}
          </pre>
        )}
      </div>
    );
  }

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <h3 className="font-mono text-sm text-neutral-100">vault_files</h3>

      {listState === "loading" && (
        <p className="mt-3 font-mono text-xs text-neutral-500">loading…</p>
      )}
      {listState === "error" && (
        <p className="mt-3 font-mono text-xs text-red-400">{listErrorMessage}</p>
      )}
      {listState === "ready" && files.length === 0 && (
        <p className="mt-3 font-mono text-xs text-neutral-500">
          ○ vault is empty — the agent will save notes here as it works
        </p>
      )}
      {listState === "ready" && files.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {files.map((file) => (
            <li key={file.name}>
              <button
                type="button"
                onClick={() => handleOpenFile(file.name)}
                className="flex w-full items-center justify-between rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-left transition hover:border-white/25 hover:bg-white/10 active:scale-[0.99]"
              >
                <span className="truncate font-mono text-sm text-neutral-100">{file.name}</span>
                <span className="ml-3 shrink-0 font-mono text-xs text-neutral-500">
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
