import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createVaultFolder,
  deleteVaultFile,
  deleteVaultFolder,
  listVaultEntries,
  listVaultFolders,
  revealInFinder,
  vaultRoot,
  writeVaultBytes,
  moveVaultFile,
  moveVaultFolder,
  readVaultFile,
  writeVaultFile,
} from "../api";
import { forgetNote, indexNotes, noteTexts, rememberNote } from "../vaultIndex";
import { extensionOf, isEditable, kindOf, MAX_IMPORT_BYTES } from "../fileKind";
import { collectDrop, isFileDrag, uniqueName, type DroppedTree } from "../vaultDrop";
import { useResizableSidebar } from "../hooks/useResizableSidebar";
import { onBusControl } from "../lib/bus";
import { allFolderPaths, basename, buildTree, joinPath, parentFolder } from "../noteTree";
import type { VaultFile } from "../types";
import { findBacklinks, resolveWikiLink, wikiLinkToNoteName } from "../wikilinks";
import { DeleteButton } from "./DeleteButton";
import { NoteTree } from "./NoteTree";
import { FilePreview } from "./vault/FilePreview";
import { ContextMenu, type MenuItem } from "./vault/ContextMenu";
import { MarkdownView } from "./vault/MarkdownView";

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
  /** Bumped when the background scan caches another note's body, to
   *  recompute backlinks. The bodies themselves live in `vaultIndex`, outside
   *  React — this panel unmounts on every tab switch, and a cache that went
   *  with it made the scan restart every time. */
  const [indexTick, setIndexTick] = useState(0);

  /** The vault's absolute path, for the asset URLs media streams from. From
   *  the agent config, so there is still one answer to where the vault is. */
  const [vaultDir, setVaultDir] = useState("");

  const [folders, setFolders] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  /** The inline "name this" input. Tauri's webview implements no
   *  `window.prompt` (it just returns null), so naming has to happen in the
   *  panel itself — with a prompt, every create silently did nothing. */
  const [pending, setPending] = useState<{ kind: "note" | "folder"; parent: string } | null>(null);
  const [draft, setDraft] = useState("");
  /** An OS drop in progress: which folder it lands in, and how far along.
   *  A strip rather than a modal — the import runs while the panel stays
   *  usable, and Tauri's webview has no dialog to put it in anyway. */
  const [importing, setImporting] = useState<{ done: number; total: number; label: string } | null>(
    null,
  );
  /** Folder an OS drag is hovering. Separate from `dropTarget`, which is a
   *  note being moved *inside* the vault — the two drops do different things
   *  and must not look alike. */
  const [fileDropTarget, setFileDropTarget] = useState<string | null>(null);

  /** The row currently being renamed, and the open right-click menu. Both
   *  exist because Tauri's webview has no `window.prompt` or `confirm` — a
   *  rename is an inline input and a confirm is a second click. */
  const [renaming, setRenaming] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);

  /** Folder whose delete is armed — the first click arms, the second commits. */
  const [armedFolder, setArmedFolder] = useState<string | null>(null);
  const draftRef = useRef<HTMLInputElement | null>(null);

  const {
    width: sidebarWidth,
    dragging,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    reset: resetSidebar,
  } = useResizableSidebar();

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dirty = content !== saved;

  const noteNames = useMemo(() => files.map((f) => f.name), [files]);

  /** Record a body we already have in hand, so the background scan doesn't
   *  re-fetch what was just on screen. `modifiedAt` is deliberately a
   *  placeholder here: a listing refresh carries the real one and supersedes
   *  this, and being re-read once is cheaper than being missed. */
  const cacheContent = useCallback((name: string, text: string) => {
    rememberNote(name, text, "");
    setIndexTick((n) => n + 1);
  }, []);

  const openFile = useCallback(
    (name: string) => {
      setSelectedFile(name);
      setActionErrorMessage("");
      setMode("preview");
      // Media and unknown binaries have nothing to fetch: they stream off
      // disk through the asset protocol, and asking the server for their text
      // is a round trip that now correctly ends in a refusal.
      if (!isEditable(kindOf(name))) {
        setContent("");
        setSaved("");
        setDetailState("ready");
        return;
      }
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

  useEffect(() => {
    // Best-effort: without it media has no URL to point at and shows a
    // loading line, which is a better failure than a broken image.
    vaultRoot().then(setVaultDir).catch(() => {});
  }, []);

  const refreshFolders = useCallback(() => {
    listVaultFolders()
      .then(setFolders)
      // A folder listing that fails only costs empty folders and drop
      // targets; the notes themselves still render from their own paths.
      .catch(() => setFolders([]));
  }, []);

  // A note written or deleted elsewhere — by a phone, or by the agent
  // mid-turn — changes what this list should show. Re-read rather than
  // applying the change: the listing is cheap next to getting it wrong, which
  // is the same reasoning the folder-move path uses.
  useEffect(
    () =>
      onBusControl((frame) => {
        if (frame.control !== "vault_changed") return;
        void listVaultEntries().then(setFiles).catch(() => {});
      }),
    [],
  );

  useEffect(() => {
    listVaultEntries()
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

  /** Backlinks need every note's text, so pull the bodies in in the
   *  background. The cache, and the skip-what-hasn't-changed logic, live in
   *  `vaultIndex` outside React: this panel unmounts on every tab switch, and
   *  keeping them here restarted the whole scan on every visit.
   *
   *  Markdown only, now that the listing includes images — asking the server
   *  for a PNG's text is n round trips ending in n refusals. */
  useEffect(() => {
    if (listState !== "ready" || files.length === 0) return;
    return indexNotes(files, () => setIndexTick((n) => n + 1));
  }, [listState, files]);

  const save = useCallback(
    async (name: string, text: string) => {
      setSaving(true);
      setActionErrorMessage("");
      try {
        // Markdown goes through the note write, which indexes it for recall.
      // Anything else goes through the byte write — the note route refuses a
      // non-.md name, and a .txt has no business in the recall index anyway.
      const meta =
        kindOf(name) === "markdown"
          ? await writeVaultFile(name, text)
          : await writeVaultBytes(name, new TextEncoder().encode(text));
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
    // An extension the user typed is one they meant — `todo.txt` used to
    // become `todo.txt.md`. Only a bare name gets `.md` assumed for it.
    const name = extensionOf(withFolder) ? withFolder : `${withFolder}.md`;
    if (noteNames.includes(name)) {
      openFile(name);
      return;
    }
    // A heading seeds a note; anything else starts empty, because a `#` line
    // is not a helpful first line of a CSV.
    const seed =
      kindOf(name) === "markdown"
        ? `# ${basename(name).replace(/\.md$/i, "")}\n\n`
        : "";
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
      // The body is unchanged but its name is not, and the index is keyed
      // by name — drop the old entry and let the next scan pick the new one up.
      forgetNote(from);
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
      const [refreshedFiles] = await Promise.all([listVaultEntries()]);
      setFiles(refreshedFiles);

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

  /** Open the tree down to a folder — what a breadcrumb segment does. */
  function revealFolder(path: string) {
    setQuery("");
    setExpanded((prev) => {
      const next = new Set(prev);
      let walk = path;
      while (walk) {
        next.add(walk);
        walk = parentFolder(walk);
      }
      return next;
    });
  }

  /** Rename in place. A rename and a move are the same operation on disk and
   *  the same call here — only the part of the path that changes differs. */
  async function commitRename(path: string, rawName: string) {
    setRenaming(null);
    const next = rawName.trim();
    if (!next || next === basename(path)) return;
    if (next.includes("/")) {
      setActionErrorMessage("a name can't contain a slash — drag it instead to move it");
      return;
    }
    const to = joinPath(parentFolder(path), next);
    const isFolder = folders.includes(path) || files.some((f) => f.name.startsWith(`${path}/`));
    setActionErrorMessage("");
    try {
      if (isFolder) {
        // Not `moveFolder`, which files a folder *into* another and keeps its
        // own name — a rename changes exactly the last segment.
        await moveVaultFolder(path, to);
        const refreshed = await listVaultEntries();
        setFiles(refreshed);
        if (selectedFile?.startsWith(`${path}/`)) {
          setSelectedFile(selectedFile.replace(path, to));
        }
        setExpanded((prev) => {
          const nextSet = new Set<string>();
          for (const open of prev) {
            nextSet.add(open === path || open.startsWith(`${path}/`) ? open.replace(path, to) : open);
          }
          return nextSet;
        });
        refreshFolders();
      } else {
        await moveNote(path, to);
      }
    } catch (err) {
      setActionErrorMessage(`couldn't rename ${path}: ${errText(err)}`);
    }
  }

  /** Build the menu for whatever was right-clicked. */
  function openContextMenu(e: React.MouseEvent, path: string, kind: "note" | "folder") {
    e.preventDefault();
    e.stopPropagation();
    const items: MenuItem[] =
      kind === "folder"
        ? [
            { label: "New file", onSelect: () => startDraft("note", path) },
            { label: "New folder", onSelect: () => startDraft("folder", path) },
            { label: "Rename", onSelect: () => setRenaming(path), separated: true },
            { label: "Reveal in Finder", onSelect: () => void reveal(path) },
            {
              label: "Delete folder",
              destructive: true,
              separated: true,
              onSelect: () => void deleteFolderNow(path),
            },
          ]
        : [
            { label: "Open", onSelect: () => selectAnother(path) },
            { label: "Rename", onSelect: () => setRenaming(path) },
            { label: "Reveal in Finder", onSelect: () => void reveal(path), separated: true },
            {
              label: "Delete",
              destructive: true,
              separated: true,
              onSelect: () => handleDelete(path),
            },
          ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  }

  const reveal = (path: string) =>
    revealInFinder(path).catch((err) => setActionErrorMessage(errText(err)));

  /** The menu's delete skips the arm step the sidebar button uses — the menu
   *  item does its own arming, so a second confirm would be one too many. */
  async function deleteFolderNow(path: string) {
    const inside = files.filter((f) => f.name.startsWith(`${path}/`));
    setActionErrorMessage("");
    try {
      await deleteVaultFolder(path, inside.length > 0);
      setFiles((prev) => prev.filter((f) => !f.name.startsWith(`${path}/`)));
      if (selectedFile?.startsWith(`${path}/`)) {
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

  /** Bring dropped files (and folders) into the vault.
   *
   *  Folders first, including empty ones — the same reason the vault lists
   *  folders separately from notes: one you just made holds nothing, and
   *  inferring it from file paths would make it vanish. Then the files, one at
   *  a time so the count means something and 200 of them don't open 200
   *  sockets at once.
   *
   *  A failure is collected, not thrown: nineteen files landing and one
   *  failing is a far better outcome than the drop aborting halfway with no
   *  way to tell what made it in.
   */
  async function importDrop(tree: DroppedTree, intoFolder: string) {
    if (tree.files.length === 0 && tree.folders.length === 0) return;
    setActionErrorMessage("");

    for (const folder of tree.folders) {
      try {
        await createVaultFolder(joinPath(intoFolder, folder));
      } catch {
        /* the file writes below create parents anyway; only an empty folder
           is actually lost, and that must not stop the import */
      }
    }

    // Claimed names include the ones taken earlier in this same batch —
    // without that, two files of the same name both get offered "shot 1.png".
    const taken = new Set(files.map((f) => f.name));
    const failures: string[] = [];
    let done = 0;
    setImporting({ done: 0, total: tree.files.length, label: "" });

    for (const { relPath, file } of tree.files) {
      setImporting({ done, total: tree.files.length, label: relPath });
      const target = uniqueName(joinPath(intoFolder, relPath), taken);
      try {
        if (file.size > MAX_IMPORT_BYTES) {
          throw new Error(
            `${formatBytes(file.size)} — too large to import, copy it into the vault in Finder instead`,
          );
        }
        const bytes = new Uint8Array(await file.arrayBuffer());
        const meta = await writeVaultBytes(target, bytes);
        taken.add(target);
        setFiles((prev) => [meta, ...prev.filter((f) => f.name !== target)]);
      } catch (err) {
        failures.push(`${relPath}: ${errText(err)}`);
      }
      done++;
    }

    setImporting(null);
    refreshFolders();
    if (failures.length > 0) {
      setActionErrorMessage(
        `${failures.length} of ${tree.files.length} couldn't be imported — ${failures[0]}${
          failures.length > 1 ? ` (and ${failures.length - 1} more)` : ""
        }`,
      );
    }
  }

  /** The drop itself. Branching on which kind of drag this is comes first:
   *  an OS drop imports, an internal one moves, and treating either as the
   *  other loses data. */
  function handleFileDrop(e: React.DragEvent, intoFolder: string) {
    if (!isFileDrag(e.dataTransfer)) return false;
    e.preventDefault();
    e.stopPropagation();
    setFileDropTarget(null);
    // Collect synchronously — `dataTransfer` is dead the moment this returns.
    void collectDrop(e.dataTransfer)
      .then((tree) => importDrop(tree, intoFolder))
      .catch((err) => setActionErrorMessage(`couldn't read the drop: ${errText(err)}`));
    return true;
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
        if (selectedFile && isEditable(kindOf(selectedFile))) {
          setMode((m) => (m === "edit" ? "preview" : "edit"));
        }
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
  /** What the open file is — which editor, which preview, which icon. */
  const selectedKind = selectedFile ? kindOf(selectedFile) : undefined;

  const visibleFiles = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return files;
    return files.filter((f) => f.name.toLowerCase().includes(q));
  }, [files, query]);

  const tree = useMemo(() => buildTree(files, folders), [files, folders]);

  const backlinks = useMemo(
    () => (selectedFile ? findBacklinks(selectedFile, noteTexts(), noteNames) : []),
    // `indexTick` is the dependency that matters — the bodies live outside
    // React, so this is how a newly-scanned note reaches the list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedFile, noteNames, indexTick],
  );

  return (
    <div className="flex min-h-0 flex-1">
      {menu && (
        <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />
      )}
      <div
        // Width is inline rather than a class because it is a dragged value.
        // The transition is suppressed mid-drag — animating toward a target
        // that moves every pointer event lags behind the cursor.
        style={{ width: sidebarCollapsed ? 0 : sidebarWidth }}
        className={`themed-scroll shrink-0 overflow-y-auto border-r border-white/10 ${
          dragging ? "" : "transition-[width,opacity,padding] duration-200"
        } ${sidebarCollapsed ? "overflow-hidden p-0 opacity-0" : "p-3 opacity-100"}`}
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
            placeholder="search files"
            className="mt-2 w-full rounded-md border border-white/10 bg-black/20 px-2 py-1 text-xs text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/40 focus:outline-none"
          />
        )}

        {importing && (
          <div className="mt-2 rounded-md border border-emerald-400/20 bg-emerald-400/5 p-1.5">
            <p className="px-0.5 text-[10px] uppercase tracking-wider text-emerald-300/70">
              importing {importing.done}/{importing.total}
            </p>
            <p className="truncate px-0.5 text-[10px] text-neutral-400" title={importing.label}>
              {importing.label}
            </p>
            <div className="mt-1 h-0.5 overflow-hidden rounded bg-white/10">
              <div
                className="h-full bg-emerald-400/60 transition-all"
                style={{
                  width: `${importing.total ? (importing.done / importing.total) * 100 : 0}%`,
                }}
              />
            </div>
          </div>
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
              placeholder={pending.kind === "note" ? "name.md, notes.txt, …" : "folder name"}
              className="w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-xs text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/50 focus:outline-none"
            />
            <p className="mt-1 px-0.5 text-[10px] text-neutral-600">enter to create · esc to cancel</p>
          </div>
        )}

        {listState === "ready" && files.length === 0 && folders.length === 0 && !pending && (
          <p className="mt-2 px-1 text-xs leading-relaxed text-neutral-500">
            ○ empty — press + to write a note, or drop files here from Finder.
          </p>
        )}
        {listState === "ready" && query.trim() && visibleFiles.length === 0 && (
          <p className="mt-2 px-1 text-xs text-neutral-500">nothing matches “{query}”</p>
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
              fileDropTarget === ""
                ? "bg-emerald-400/10 ring-1 ring-dashed ring-emerald-400/50"
                : dropTarget === ""
                  ? "bg-[#4f8dff]/10 ring-1 ring-[#4f8dff]/30"
                  : ""
            }`}
            // The gap around the tree is the vault root as a drop target, so a
            // note can be dragged back out of a folder.
            onDragOver={(e) => {
              e.preventDefault();
              // An OS drop copies files in; an internal one moves a note.
              // Different cursors so which is about to happen is visible
              // before the mouse is released.
              if (isFileDrag(e.dataTransfer)) {
                e.dataTransfer.dropEffect = "copy";
                if (fileDropTarget !== "") setFileDropTarget("");
                return;
              }
              if (dropTarget !== "") setDropTarget("");
            }}
            onDragLeave={() => {
              setDropTarget((t) => (t === "" ? null : t));
              setFileDropTarget((t) => (t === "" ? null : t));
            }}
            onDrop={(e) => {
              if (handleFileDrop(e, "")) return;
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
              fileDropTarget={fileDropTarget}
              onFileDrop={handleFileDrop}
              onFileDropTargetChange={setFileDropTarget}
              renaming={renaming}
              onRenameCommit={(path, next) => void commitRename(path, next)}
              onRenameCancel={() => setRenaming(null)}
              onContextMenu={openContextMenu}
            />
          </div>
        )}
      </div>

      {/* The resize handle. Sits between the panes and is only a few pixels
          wide, so it gets a generous cursor target and a visible hover. */}
      {!sidebarCollapsed && (
        <div
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onDoubleClick={resetSidebar}
          title="drag to resize · double-click to reset"
          className={`w-1 shrink-0 cursor-col-resize transition ${
            dragging ? "bg-[#4f8dff]/50" : "hover:bg-[#4f8dff]/30"
          }`}
        />
      )}

      <div
        className={`themed-scroll relative min-h-0 flex-1 overflow-y-auto p-4 transition ${
          fileDropTarget === "__pane__" ? "ring-1 ring-dashed ring-emerald-400/50" : ""
        }`}
        // Dropping onto the reading pane files things beside whatever is
        // open, which is where you nearly always mean when you drag an image
        // in while writing.
        onDragOver={(e) => {
          if (!isFileDrag(e.dataTransfer)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          if (fileDropTarget !== "__pane__") setFileDropTarget("__pane__");
        }}
        onDragLeave={() => setFileDropTarget((t) => (t === "__pane__" ? null : t))}
        onDrop={(e) => handleFileDrop(e, selectedFile ? parentFolder(selectedFile) : "")}
      >
        {fileDropTarget === "__pane__" && (
          <div className="pointer-events-none absolute inset-2 z-10 flex items-center justify-center rounded-md border border-dashed border-emerald-400/50 bg-emerald-400/5">
            <p className="text-xs text-emerald-300">
              drop to add to {selectedFile ? parentFolder(selectedFile) || "the vault" : "the vault"}
            </p>
          </div>
        )}
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
            <h3 className="flex min-w-0 items-center gap-1 truncate text-sm tracking-tight">
              {/* Path as breadcrumb — each folder expands the tree to it, so
                  a note found through search can be located in the tree. */}
              {parentFolder(selectedFile)
                .split("/")
                .filter(Boolean)
                .map((segment, i, all) => {
                  const path = all.slice(0, i + 1).join("/");
                  return (
                    <span key={path} className="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        onClick={() => revealFolder(path)}
                        title={`show ${path}`}
                        className="rounded px-0.5 text-neutral-500 transition hover:text-neutral-200"
                      >
                        {segment}
                      </button>
                      <span className="text-neutral-700">/</span>
                    </span>
                  );
                })}
              <span className="truncate font-semibold text-neutral-100">
                {basename(selectedFile)}
              </span>
              {dirty && <span className="text-amber-400">•</span>}
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
              {/* Only where there is something to edit — a video has no
                  edit mode, and offering one is a button that does nothing. */}
              {selectedKind !== undefined && isEditable(selectedKind) && (
                <button
                  type="button"
                  onClick={() => setMode((m) => (m === "edit" ? "preview" : "edit"))}
                  title="toggle edit/preview (⌘E)"
                  className="rounded-md px-2 py-0.5 text-xs text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
                >
                  {mode === "edit" ? "preview" : "edit"}
                </button>
              )}
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
          <div className="text-sm leading-relaxed text-neutral-500">
            <p>Select a file to read, or press + to write a note.</p>
            <p className="mt-2 text-xs">
              Link notes with <code className="rounded bg-white/5 px-1">[[name]]</code>, show an
              image with <code className="rounded bg-white/5 px-1">![[shot.png]]</code>. Drag files
              or folders in from Finder to add them.
            </p>
          </div>
        )}
        {selectedFile && detailState === "loading" && <p className="text-sm text-neutral-500">loading…</p>}
        {selectedFile && detailState === "error" && <p className="text-sm text-red-400">{detailErrorMessage}</p>}

        {/* Editable text — markdown and code alike. Same editor either way:
            the difference is what preview mode does with it. */}
        {selectedFile && detailState === "ready" && selectedKind !== undefined &&
          isEditable(selectedKind) && mode === "edit" && (
          <textarea
            ref={textareaRef}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onBlur={() => {
              if (dirty && !saving) void save(selectedFile, content);
            }}
            spellCheck={false}
            placeholder={
              selectedKind === "markdown"
                ? "Write in markdown. Link other notes with [[name]]."
                : ""
            }
            className="themed-scroll min-h-[60vh] w-full resize-none rounded-md border border-white/10 bg-black/20 p-3 font-mono text-sm leading-relaxed text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/40 focus:outline-none"
          />
        )}

        {selectedFile && detailState === "ready" && selectedKind === "markdown" &&
          mode === "preview" && (
          <article
            className="daimon-prose prose prose-invert prose-sm max-w-none"
            onDoubleClick={() => setMode("edit")}
            title="double-click to edit"
          >
            <MarkdownView
              text={content}
              noteName={selectedFile}
              files={files}
              vaultDir={vaultDir}
              onFollow={handleFollowLink}
            />
          </article>
        )}

        {/* Code and data read better as themselves than as prose — no
            highlighting, which would mean a new dependency this doesn't need. */}
        {selectedFile && detailState === "ready" && selectedKind === "text" &&
          mode === "preview" && (
          <pre
            onDoubleClick={() => setMode("edit")}
            title="double-click to edit"
            className="themed-scroll overflow-x-auto rounded-md border border-white/10 bg-black/20 p-3 font-mono text-xs leading-relaxed text-neutral-300"
          >
            {content}
          </pre>
        )}

        {/* Everything else: streamed off disk, or an honest dead end. */}
        {selectedFile && detailState === "ready" && selectedKind !== undefined &&
          !isEditable(selectedKind) && (
          <FilePreview
            name={selectedFile}
            kind={selectedKind}
            sizeBytes={selectedFileMeta?.sizeBytes ?? 0}
            modifiedAt={selectedFileMeta?.modifiedAt ?? ""}
            vaultDir={vaultDir}
            onReveal={() => void revealInFinder(selectedFile).catch((err) =>
              setActionErrorMessage(errText(err)),
            )}
          />
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
