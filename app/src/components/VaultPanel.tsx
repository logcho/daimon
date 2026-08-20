import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  createVaultFolder,
  deleteVaultFile,
  deleteVaultFolder,
  listVaultFiles,
  listVaultFolders,
  moveVaultFile,
  moveVaultFolder,
  readVaultFile,
  writeVaultFile,
} from "../api";
import { allFolderPaths, basename, buildTree, joinPath, parentFolder } from "../noteTree";
import type { VaultFile } from "../types";
import { findBacklinks, findWikiLinks, resolveWikiLink, wikiLinkToNoteName } from "../wikilinks";
import { DeleteButton } from "./DeleteButton";
import { NoteTree } from "./NoteTree";

type ListState = "loading" | "ready" | "error";
type DetailState = "idle" | "loading" | "ready" | "error";
type Mode = "preview" | "edit";

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

const errText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** Split note text on `[[wikilinks]]` so the plain stretches can go through
 *  ReactMarkdown untouched and the links become buttons. Markdown doesn't know
 *  the syntax — left alone it renders as literal brackets — and rewriting them
 *  into `[]()` links first would break any that appear inside a code fence. */
function renderWithWikiLinks(
  text: string,
  noteNames: string[],
  onFollow: (target: string) => void,
) {
  const links = findWikiLinks(text);
  if (links.length === 0) {
    return (
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    );
  }
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  links.forEach((link, i) => {
    if (link.start > cursor) {
      parts.push(
        <ReactMarkdown key={`t${i}`} remarkPlugins={[remarkGfm]}>
          {text.slice(cursor, link.start)}
        </ReactMarkdown>,
      );
    }
    const resolved = resolveWikiLink(link.target, noteNames);
    parts.push(
      <button
        key={`l${i}`}
        type="button"
        onClick={() => onFollow(link.target)}
        title={resolved ? `open ${resolved}` : `create ${wikiLinkToNoteName(link.target)}`}
        className={`mx-0.5 rounded px-1 transition ${
          resolved
            ? "text-[#4f8dff] hover:bg-[#4f8dff]/10 hover:underline"
            : "text-amber-400/80 hover:bg-amber-400/10 hover:underline"
        }`}
      >
        {link.label}
        {!resolved && <span className="ml-0.5 text-[10px] opacity-70">+</span>}
      </button>,
    );
    cursor = link.end;
  });
  if (cursor < text.length) {
    parts.push(
      <ReactMarkdown key="tail" remarkPlugins={[remarkGfm]}>
        {text.slice(cursor)}
      </ReactMarkdown>,
    );
  }
  return parts;
}

/** Two-pane Obsidian-style notes browser: browse, edit, create, and link.
 *
 *  The agent and the user write into the same vault, so this reads and writes
 *  through the agent server rather than the filesystem — one source of truth
 *  for where notes live, and a save immediately reindexes the note for
 *  `recall`, which is what keeps the agent's memory and what you see in
 *  agreement. */
export function VaultPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [files, setFiles] = useState<VaultFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [content, setContent] = useState("");
  const [detailErrorMessage, setDetailErrorMessage] = useState("");
  const [actionErrorMessage, setActionErrorMessage] = useState("");

  const [mode, setMode] = useState<Mode>("preview");
  /** Last text known to match what's on disk — `content !== saved` is the
   *  dirty check, so it survives edit/preview toggles and reselection. */
  const [saved, setSaved] = useState("");
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  /** Note text cache, for backlinks — computing them needs every note's body,
   *  not just the open one. Filled as notes are opened plus one bulk load. */
  const [contents, setContents] = useState<Map<string, string>>(new Map());

  const [folders, setFolders] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  /** The inline "name this" input. Tauri's webview implements no
   *  `window.prompt` (it just returns null), so naming has to happen in the
   *  panel itself — with a prompt, every create silently did nothing. */
  const [pending, setPending] = useState<{ kind: "note" | "folder"; parent: string } | null>(null);
  const [draft, setDraft] = useState("");
  /** Folder whose delete is armed — the first click arms, the second commits. */
  const [armedFolder, setArmedFolder] = useState<string | null>(null);
  const draftRef = useRef<HTMLInputElement | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dirty = content !== saved;

  const noteNames = useMemo(() => files.map((f) => f.name), [files]);

  const cacheContent = useCallback((name: string, text: string) => {
    setContents((prev) => {
      if (prev.get(name) === text) return prev;
      const next = new Map(prev);
      next.set(name, text);
      return next;
    });
  }, []);

  const openFile = useCallback(
    (name: string) => {
      setSelectedFile(name);
      setActionErrorMessage("");
      setMode("preview");
      setDetailState("loading");
      readVaultFile(name)
        .then((text) => {
          setContent(text);
          setSaved(text);
          cacheContent(name, text);
          setDetailState("ready");
        })
        .catch((err) => {
          setDetailErrorMessage(errText(err));
          setDetailState("error");
        });
    },
    [cacheContent],
  );

  const refreshFolders = useCallback(() => {
    listVaultFolders()
      .then(setFolders)
      // A folder listing that fails only costs empty folders and drop
      // targets; the notes themselves still render from their own paths.
      .catch(() => setFolders([]));
  }, []);

  useEffect(() => {
    listVaultFiles()
      .then((f) => {
        setFiles(f);
        setListState("ready");
        // Expand the folders that already hold notes, so the tree opens on
        // something useful rather than a row of closed folders.
        setExpanded((prev) => {
          const next = new Set(prev);
          for (const file of f) {
            let folder = parentFolder(file.name);
            while (folder) {
              next.add(folder);
              folder = parentFolder(folder);
            }
          }
          return next;
        });
        if (f.length > 0) openFile(f[0].name);
      })
      .catch((err) => {
        setListErrorMessage(errText(err));
        setListState("error");
      });
    refreshFolders();
  }, [openFile, refreshFolders]);

  /** Backlinks need every note's text, so pull the bodies in once in the
   *  background. Best-effort and non-blocking: an unreadable note just doesn't
   *  contribute backlinks rather than failing the panel. */
  useEffect(() => {
    if (listState !== "ready" || files.length === 0) return;
    let cancelled = false;
    (async () => {
      for (const file of files) {
        if (cancelled) return;
        if (contents.has(file.name)) continue;
        try {
          const text = await readVaultFile(file.name);
          if (cancelled) return;
          cacheContent(file.name, text);
        } catch {
          /* skip — one unreadable note must not stop the scan */
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Deliberately keyed on the *names*: re-running whenever `contents`
    // changed would restart the scan on every note it just cached.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listState, noteNames.join(" "), cacheContent]);

  const save = useCallback(
    async (name: string, text: string) => {
      setSaving(true);
      setActionErrorMessage("");
      try {
        const meta = await writeVaultFile(name, text);
        setSaved(text);
        cacheContent(name, text);
        setFiles((prev) => {
          const without = prev.filter((f) => f.name !== name);
          // Newest first, matching the server's own ordering.
          return [meta, ...without];
        });
      } catch (err) {
        setActionErrorMessage(`couldn't save ${name}: ${errText(err)}`);
      } finally {
        setSaving(false);
      }
    },
    [cacheContent],
  );

  /** Create a note from an already-decided name. Naming happens in the
   *  sidebar's inline input, never in `window.prompt` — Tauri's webview has
   *  no prompt implementation, so it returns null and the create silently
   *  did nothing. */
  function handleCreate(rawName: string, inFolder = "") {
    const trimmed = rawName.trim();
    if (!trimmed) return;
    // A name typed into a folder's own "+" is relative to that folder unless
    // it already carries a path of its own.
    const withFolder = inFolder && !trimmed.includes("/") ? joinPath(inFolder, trimmed) : trimmed;
    const name = /\.md$/i.test(withFolder) ? withFolder : `${withFolder}.md`;
    if (noteNames.includes(name)) {
      openFile(name);
      return;
    }
    const seed = `# ${name.slice(name.lastIndexOf("/") + 1).replace(/\.md$/i, "")}\n\n`;
    setSelectedFile(name);
    setContent(seed);
    setSaved(""); // nothing on disk yet, so this counts as unsaved
    setDetailState("ready");
    setMode("edit");
    void save(name, seed);
  }

  async function handleNewFolder(rawName: string, parent = "") {
    const name = rawName.trim().replace(/^\/+|\/+$/g, "");
    if (!name) return;
    const path = parent && !name.includes("/") ? joinPath(parent, name) : name;
    setActionErrorMessage("");
    try {
      await createVaultFolder(path);
      refreshFolders();
      // Open it (and its parents) so the folder you just made is visible
      // instead of collapsed somewhere in the tree.
      setExpanded((prev) => {
        const next = new Set(prev);
        let walk = path;
        while (walk) {
          next.add(walk);
          walk = parentFolder(walk);
        }
        return next;
      });
    } catch (err) {
      setActionErrorMessage(`couldn't create ${path}: ${errText(err)}`);
    }
  }

  /** Two-step, like DeleteButton: the first click arms and the second
   *  commits. A folder can take any number of notes with it, and the count is
   *  shown on the armed button so that decision is made with the number in
   *  view. */
  async function handleDeleteFolder(path: string) {
    const inside = files.filter((f) => f.name.startsWith(`${path}/`));
    if (armedFolder !== path) {
      setArmedFolder(path);
      return;
    }
    setArmedFolder(null);
    setActionErrorMessage("");
    try {
      await deleteVaultFolder(path, inside.length > 0);
      setFiles((prev) => prev.filter((f) => !f.name.startsWith(`${path}/`)));
      if (selectedFile && selectedFile.startsWith(`${path}/`)) {
        setSelectedFile(null);
        setContent("");
        setSaved("");
        setDetailState("idle");
      }
      refreshFolders();
    } catch (err) {
      setActionErrorMessage(`couldn't delete ${path}: ${errText(err)}`);
    }
  }

  /** Move a note into a folder. */
  async function moveNote(from: string, toFolder: string) {
    const to = joinPath(toFolder, basename(from));
    if (to === from) return;
    setActionErrorMessage("");
    // Flush pending edits first: the move renames the file on disk, and a
    // later save would otherwise recreate the note at the old path.
    if (selectedFile === from && dirty) await save(from, content);
    try {
      const meta = await moveVaultFile(from, to);
      setFiles((prev) => [meta, ...prev.filter((f) => f.name !== from)]);
      setContents((prev) => {
        const next = new Map(prev);
        const text = next.get(from);
        next.delete(from);
        if (text !== undefined) next.set(to, text);
        return next;
      });
      if (selectedFile === from) setSelectedFile(to);
      refreshFolders();
    } catch (err) {
      setActionErrorMessage(`couldn't move ${from}: ${errText(err)}`);
    }
  }

  /** Move a folder (and everything under it) into another folder. */
  async function moveFolder(from: string, toFolder: string) {
    const to = joinPath(toFolder, basename(from));
    if (to === from) return;
    // The server refuses this too, but catching it here keeps a meaningless
    // drag from looking like a server error.
    if (to === from || to.startsWith(`${from}/`)) {
      setActionErrorMessage(`can't move ${from} into itself`);
      return;
    }
    setActionErrorMessage("");
    if (selectedFile?.startsWith(`${from}/`) && dirty) await save(selectedFile, content);
    try {
      await moveVaultFolder(from, to);
      // Every path under the folder shifted, so re-read rather than trying to
      // patch each one — the listing is cheap next to getting it wrong.
      const [refreshedFiles] = await Promise.all([listVaultFiles()]);
      setFiles(refreshedFiles);
      setContents(new Map());
      if (selectedFile?.startsWith(`${from}/`)) {
        setSelectedFile(selectedFile.replace(from, to));
      }
      setExpanded((prev) => {
        // Keep the moved subtree open at its new location.
        const next = new Set<string>();
        for (const path of prev) {
          next.add(path === from || path.startsWith(`${from}/`) ? path.replace(from, to) : path);
        }
        next.add(to);
        return next;
      });
      refreshFolders();
    } catch (err) {
      setActionErrorMessage(`couldn't move ${from}: ${errText(err)}`);
    }
  }

  /** Drop handler for the tree. The payload carries which kind was dragged,
   *  since notes and folders move differently on the client side even though
   *  they share one endpoint. */
  function handleDrop(payload: string, toFolder: string) {
    const [kind, ...rest] = payload.split(":");
    const from = rest.join(":");
    if (!from) return;
    if (kind === "folder") void moveFolder(from, toFolder);
    else void moveNote(from, toFolder);
  }

  function startDraft(kind: "note" | "folder", parent = "") {
    setActionErrorMessage("");
    setPending({ kind, parent });
    setDraft("");
    // Creating inside a collapsed folder would otherwise put the new item
    // somewhere the user can't see.
    if (parent) {
      setExpanded((prev) => {
        const next = new Set(prev);
        let walk = parent;
        while (walk) {
          next.add(walk);
          walk = parentFolder(walk);
        }
        return next;
      });
    }
  }

  function commitDraft() {
    if (!pending || !draft.trim()) {
      setPending(null);
      return;
    }
    const { kind, parent } = pending;
    setPending(null);
    if (kind === "note") handleCreate(draft, parent);
    else void handleNewFolder(draft, parent);
    setDraft("");
  }

  // Focus the inline input the moment it appears, so it behaves like the
  // dialog it replaces: click "+", type, press Enter.
  useEffect(() => {
    if (pending) draftRef.current?.focus();
  }, [pending]);

  // Disarm a folder delete after a few seconds, matching DeleteButton.
  useEffect(() => {
    if (!armedFolder) return;
    const t = setTimeout(() => setArmedFolder(null), 3000);
    return () => clearTimeout(t);
  }, [armedFolder]);

  /** Following a link to a note that doesn't exist creates it — the same
   *  "link first, write later" flow Obsidian is built around. */
  function handleFollowLink(target: string) {
    const resolved = resolveWikiLink(target, noteNames);
    if (resolved) {
      openFile(resolved);
      return;
    }
    handleCreate(wikiLinkToNoteName(target));
  }

  function handleDelete(name: string) {
    const index = files.findIndex((f) => f.name === name);
    if (index === -1) return;
    const removed = files[index];

    setActionErrorMessage("");
    setFiles((prev) => prev.filter((f) => f.name !== name));
    if (selectedFile === name) {
      setSelectedFile(null);
      setContent("");
      setSaved("");
      setDetailState("idle");
    }

    deleteVaultFile(name).catch((err) => {
      setFiles((prev) => {
        if (prev.some((f) => f.name === name)) return prev;
        const restored = [...prev];
        restored.splice(Math.min(index, restored.length), 0, removed);
        return restored;
      });
      setActionErrorMessage(`couldn't delete ${name}: ${errText(err)}`);
    });
  }

  /** Cmd/Ctrl+S saves, Cmd/Ctrl+E toggles edit/preview — both Obsidian's. */
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key === "s") {
        e.preventDefault();
        if (selectedFile && dirty && !saving) void save(selectedFile, content);
      } else if (e.key === "e") {
        e.preventDefault();
        if (selectedFile) setMode((m) => (m === "edit" ? "preview" : "edit"));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedFile, dirty, saving, content, save]);

  /** Switching away from a note with unsaved edits would lose them silently,
   *  and the agent could overwrite the file in the meantime — so flush first. */
  function selectAnother(name: string) {
    if (name === selectedFile) return;
    if (selectedFile && dirty) void save(selectedFile, content);
    openFile(name);
  }

  const selectedFileMeta = selectedFile ? files.find((f) => f.name === selectedFile) : undefined;

  const visibleFiles = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return files;
    return files.filter((f) => f.name.toLowerCase().includes(q));
  }, [files, query]);

  const tree = useMemo(() => buildTree(files, folders), [files, folders]);

  const backlinks = useMemo(
    () => (selectedFile ? findBacklinks(selectedFile, contents, noteNames) : []),
    [selectedFile, contents, noteNames],
  );

  return (
    <div className="flex min-h-0 flex-1">
      <div
        className={`themed-scroll shrink-0 overflow-y-auto border-r border-white/10 transition-[width,opacity,padding] duration-200 ${
          sidebarCollapsed ? "w-0 overflow-hidden p-0 opacity-0" : "w-52 p-3 opacity-100"
        }`}
      >
        <div className="flex items-center justify-between px-1">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-500">vault</h3>
          <span className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => startDraft("note")}
              title="new note"
              className="rounded-md px-1.5 text-sm leading-none text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
            >
              +
            </button>
            <button
              type="button"
              onClick={() => startDraft("folder")}
              title="new folder"
              className="rounded-md px-1.5 text-sm leading-none text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
            >
              ⊞
            </button>
          </span>
        </div>

        {listState === "ready" && files.length > 0 && (
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search notes"
            className="mt-2 w-full rounded-md border border-white/10 bg-black/20 px-2 py-1 text-xs text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/40 focus:outline-none"
          />
        )}

        {listState === "loading" && <p className="mt-2 px-1 text-xs text-neutral-500">loading…</p>}
        {listState === "error" && <p className="mt-2 px-1 text-xs text-red-400">{listErrorMessage}</p>}
        {pending && (
          <div className="mt-2 rounded-md border border-[#4f8dff]/30 bg-[#4f8dff]/5 p-1.5">
            <p className="mb-1 px-0.5 text-[10px] uppercase tracking-wider text-neutral-500">
              new {pending.kind}
              {pending.parent ? ` in ${pending.parent}` : ""}
            </p>
            <input
              ref={draftRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitDraft();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setPending(null);
                  setDraft("");
                }
              }}
              // Committing on blur would fire when the click lands on the
              // cancel button, creating the thing the user just cancelled.
              onBlur={() => setPending(null)}
              placeholder={pending.kind === "note" ? "name.md" : "folder name"}
              className="w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-xs text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/50 focus:outline-none"
            />
            <p className="mt-1 px-0.5 text-[10px] text-neutral-600">enter to create · esc to cancel</p>
          </div>
        )}

        {listState === "ready" && files.length === 0 && folders.length === 0 && !pending && (
          <p className="mt-2 px-1 text-xs text-neutral-500">○ empty — press + to write your first note</p>
        )}
        {listState === "ready" && query.trim() && visibleFiles.length === 0 && (
          <p className="mt-2 px-1 text-xs text-neutral-500">no notes match “{query}”</p>
        )}

        {/* While searching, a flat list of full paths beats a tree: the whole
            point is to find one note without knowing where it's filed. */}
        {listState === "ready" && query.trim() && visibleFiles.length > 0 && (
          <ul className="mt-2 space-y-0.5">
            {visibleFiles.map((file) => (
              <li key={file.name}>
                <button
                  type="button"
                  onClick={() => selectAnother(file.name)}
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

        {listState === "ready" && !query.trim() && tree.length > 0 && (
          <div
            className={`mt-2 rounded-md transition ${
              dropTarget === "" ? "bg-[#4f8dff]/10 ring-1 ring-[#4f8dff]/30" : ""
            }`}
            // The gap around the tree is the vault root as a drop target, so a
            // note can be dragged back out of a folder.
            onDragOver={(e) => {
              e.preventDefault();
              if (dropTarget !== "") setDropTarget("");
            }}
            onDragLeave={() => setDropTarget((t) => (t === "" ? null : t))}
            onDrop={(e) => {
              e.preventDefault();
              const from = e.dataTransfer.getData("text/plain");
              setDropTarget(null);
              if (from) handleDrop(from, "");
            }}
          >
            <NoteTree
              nodes={tree}
              selected={selectedFile}
              expanded={expanded}
              dropTarget={dropTarget}
              dirty={dirty}
              onToggleFolder={(path) =>
                setExpanded((prev) => {
                  const next = new Set(prev);
                  if (next.has(path)) next.delete(path);
                  else next.add(path);
                  return next;
                })
              }
              onSelectNote={selectAnother}
              onNewNoteIn={(folder) => startDraft("note", folder)}
              onNewFolderIn={(folder) => startDraft("folder", folder)}
              armedFolder={armedFolder}
              folderPaths={allFolderPaths(folders)}
              onMoveFolder={(from, toFolder) => void moveFolder(from, toFolder)}
              onDeleteFolder={(path) => void handleDeleteFolder(path)}
              onMoveNote={handleDrop}
              onDropTargetChange={setDropTarget}
            />
          </div>
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
            <h3 className="truncate text-sm font-semibold tracking-tight text-neutral-100">
              {selectedFile}
              {dirty && <span className="ml-1 text-amber-400">•</span>}
            </h3>
          )}
          {selectedFile && detailState === "ready" && (
            <div className="ml-auto flex shrink-0 items-center gap-1">
              {dirty && (
                <button
                  type="button"
                  onClick={() => void save(selectedFile, content)}
                  disabled={saving}
                  title="save (⌘S)"
                  className="rounded-md bg-[#4f8dff]/15 px-2 py-0.5 text-xs text-[#4f8dff] transition hover:bg-[#4f8dff]/25 disabled:opacity-50"
                >
                  {saving ? "saving…" : "save"}
                </button>
              )}
              <button
                type="button"
                onClick={() => setMode((m) => (m === "edit" ? "preview" : "edit"))}
                title="toggle edit/preview (⌘E)"
                className="rounded-md px-2 py-0.5 text-xs text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
              >
                {mode === "edit" ? "preview" : "edit"}
              </button>
              {/* Keyboard-reachable equivalent of dragging onto a folder —
                  and the only way to move a note when the tree is filtered by
                  a search, where there are no folders on screen to drop onto. */}
              <select
                value={parentFolder(selectedFile)}
                onChange={(e) => void moveNote(selectedFile, e.target.value)}
                title="move to another folder"
                className="rounded-md border border-white/10 bg-black/20 px-1.5 py-0.5 text-xs text-neutral-400 transition hover:text-neutral-100 focus:border-[#4f8dff]/40 focus:outline-none"
              >
                {allFolderPaths(folders).map((folder) => (
                  <option key={folder || "__root__"} value={folder}>
                    {folder || "(vault root)"}
                  </option>
                ))}
              </select>
              <DeleteButton name={selectedFile} onDelete={() => handleDelete(selectedFile)} />
            </div>
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

        {!selectedFile && (
          <p className="text-sm text-neutral-500">
            Select a note to read, or press + to write one. Link notes with{" "}
            <code className="rounded bg-white/5 px-1">[[name]]</code>.
          </p>
        )}
        {selectedFile && detailState === "loading" && <p className="text-sm text-neutral-500">loading…</p>}
        {selectedFile && detailState === "error" && <p className="text-sm text-red-400">{detailErrorMessage}</p>}

        {selectedFile && detailState === "ready" && mode === "edit" && (
          <textarea
            ref={textareaRef}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onBlur={() => {
              if (dirty && !saving) void save(selectedFile, content);
            }}
            spellCheck={false}
            placeholder="Write in markdown. Link other notes with [[name]]."
            className="themed-scroll min-h-[60vh] w-full resize-none rounded-md border border-white/10 bg-black/20 p-3 font-mono text-sm leading-relaxed text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/40 focus:outline-none"
          />
        )}

        {selectedFile && detailState === "ready" && mode === "preview" && (
          <article
            className="daimon-prose prose prose-invert prose-sm max-w-none"
            onDoubleClick={() => setMode("edit")}
            title="double-click to edit"
          >
            {renderWithWikiLinks(content, noteNames, handleFollowLink)}
          </article>
        )}

        {selectedFile && detailState === "ready" && backlinks.length > 0 && (
          <section className="mt-6 border-t border-white/10 pt-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-neutral-500">
              linked mentions ({backlinks.length})
            </h4>
            <ul className="mt-2 space-y-0.5">
              {backlinks.map((name) => (
                <li key={name}>
                  <button
                    type="button"
                    onClick={() => selectAnother(name)}
                    className="w-full truncate rounded-md px-2 py-1 text-left text-xs text-neutral-400 transition hover:bg-white/5 hover:text-neutral-100"
                  >
                    {name}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
