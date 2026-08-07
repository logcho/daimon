import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
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

export function VaultPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [files, setFiles] = useState<VaultFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [content, setContent] = useState("");
  const [detailErrorMessage, setDetailErrorMessage] = useState("");

  // Obsidian-style collapsible file sidebar — purely local UI state, reset
  // each time this tab reopens, same as the rest of this panel's "remounts
  // fresh each time" convention (see the mount effect below); not worth
  // persisting across sessions for a single collapse toggle.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

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

  // Remounts each time the vault tab becomes active (PipelinePanel only
  // renders this component while `view === "vault"`), so this mount effect
  // doubles as "refetch on every switch" — deliberate, since the vault is
  // meant to reflect the agent's live activity, not a one-time snapshot.
  useEffect(() => {
    listVaultFiles()
      .then((f) => {
        setFiles(f);
        setListState("ready");
        // Auto-open the most recently modified note (list_vault_files
        // already sorts that way) so the preview pane has something to show
        // the moment this tab opens, matching Obsidian's own behavior of
        // always having a note open rather than a blank pane by default.
        if (f.length > 0) handleOpenFile(f[0].name);
      })
      .catch((err) => {
        setListErrorMessage(err instanceof Error ? err.message : String(err));
        setListState("error");
      });
    // Deliberately runs once on mount only — see the comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        </div>
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
