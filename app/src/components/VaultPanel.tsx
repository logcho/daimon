import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { deleteVaultFile, listVaultFiles, readVaultFile } from "../api";
import type { VaultFile } from "../types";
import { DeleteButton } from "./DeleteButton";

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

function SidebarToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5">
      {collapsed ? (
        <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
      ) : (
        <path d="M15 6l-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
      )}
    </svg>
  );
}

/** Two-pane Obsidian-style notes browser — ported from legacy VaultPanel.tsx.
 *  Refetches on every mount (the component only renders when view === "vault"),
 *  so the file list always reflects the agent's latest activity. */
export function VaultPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [files, setFiles] = useState<VaultFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [content, setContent] = useState("");
  const [detailErrorMessage, setDetailErrorMessage] = useState("");
  // Failures from actions rather than from viewing a file. Kept separate
  // because the detail error only renders while something is selected, and a
  // delete clears the selection before its request has even finished.
  const [actionErrorMessage, setActionErrorMessage] = useState("");

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  function handleOpenFile(name: string) {
    setSelectedFile(name);
    setActionErrorMessage("");
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

  useEffect(() => {
    listVaultFiles()
      .then((f) => {
        setFiles(f);
        setListState("ready");
        if (f.length > 0) handleOpenFile(f[0].name);
      })
      .catch((err) => {
        setListErrorMessage(err instanceof Error ? err.message : String(err));
        setListState("error");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Optimistic: the row goes as soon as it's confirmed, and the request runs
   *  behind it. The decision is already made at that point — waiting on a
   *  round trip through Tauri, the agent server and the filesystem just makes
   *  the UI look stuck. If the delete does fail, the note comes back where it
   *  was and says why. */
  function handleDelete(name: string) {
    const index = files.findIndex((f) => f.name === name);
    if (index === -1) return;
    const removed = files[index];

    setActionErrorMessage("");
    setFiles((prev) => prev.filter((f) => f.name !== name));
    if (selectedFile === name) {
      setSelectedFile(null);
      setContent("");
      setDetailState("idle");
    }

    deleteVaultFile(name).catch((err) => {
      // Splice back at the original index rather than appending — the list is
      // ordered, and a failed delete shouldn't quietly reshuffle it.
      setFiles((prev) => {
        if (prev.some((f) => f.name === name)) return prev;
        const restored = [...prev];
        restored.splice(Math.min(index, restored.length), 0, removed);
        return restored;
      });
      setActionErrorMessage(
        `couldn't delete ${name}: ${err instanceof Error ? err.message : String(err)}`,
      );
    });
  }

  const selectedFileMeta = selectedFile ? files.find((f) => f.name === selectedFile) : undefined;

  return (
    <div className="flex min-h-0 flex-1">
      <div
        className={`themed-scroll shrink-0 overflow-y-auto border-r border-white/10 transition-[width,opacity,padding] duration-200 ${
          sidebarCollapsed ? "w-0 overflow-hidden p-0 opacity-0" : "w-44 p-3 opacity-100"
        }`}
      >
        <h3 className="px-1 text-xs font-semibold uppercase tracking-wider text-neutral-500">vault</h3>
        {listState === "loading" && <p className="mt-2 px-1 text-xs text-neutral-500">loading…</p>}
        {listState === "error" && <p className="mt-2 px-1 text-xs text-red-400">{listErrorMessage}</p>}
        {listState === "ready" && files.length === 0 && (
          <p className="mt-2 px-1 text-xs text-neutral-500">○ empty — the agent will save notes here</p>
        )}
        {listState === "ready" && files.length > 0 && (
          <ul className="mt-2 space-y-0.5">
            {files.map((file) => (
              <li key={file.name}>
                <button
                  type="button"
                  onClick={() => handleOpenFile(file.name)}
                  title={file.name}
                  className={`w-full truncate rounded-md px-2 py-1 text-left text-xs transition duration-150 ${
                    selectedFile === file.name
                      ? "bg-[#4f8dff]/15 text-[#4f8dff]"
                      : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
                  }`}
                >
                  {file.name}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="themed-scroll min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mb-2 flex items-center gap-2">
          <button
            type="button"
            onClick={() => setSidebarCollapsed((c) => !c)}
            title={sidebarCollapsed ? "show file list" : "hide file list"}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
          >
            <SidebarToggleIcon collapsed={sidebarCollapsed} />
          </button>
          {selectedFile && (
            <h3 className="truncate text-sm font-semibold tracking-tight text-neutral-100">{selectedFile}</h3>
          )}
          {selectedFile && (
            <span className="ml-auto">
              <DeleteButton name={selectedFile} onDelete={() => handleDelete(selectedFile)} />
            </span>
          )}
        </div>
        {actionErrorMessage && (
          <p className="mb-3 rounded-md border border-red-500/20 bg-red-500/10 px-2 py-1 text-xs text-red-300">
            {actionErrorMessage}
          </p>
        )}
        {selectedFileMeta && (
          <p className="-mt-1 mb-3 text-xs text-neutral-500">
            {formatBytes(selectedFileMeta.sizeBytes)} · {formatModifiedAt(selectedFileMeta.modifiedAt)}
          </p>
        )}

        {!selectedFile && <p className="text-sm text-neutral-500">Select a note to preview.</p>}
        {selectedFile && detailState === "loading" && <p className="text-sm text-neutral-500">loading…</p>}
        {selectedFile && detailState === "error" && <p className="text-sm text-red-400">{detailErrorMessage}</p>}
        {selectedFile && detailState === "ready" && (
          <article className="daimon-prose prose prose-invert prose-sm max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          </article>
        )}
      </div>
    </div>
  );
}
