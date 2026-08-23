import { kindOf } from "../fileKind";
import type { TreeNode } from "../noteTree";
import { FileIcon } from "./vault/FileIcon";

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`h-3 w-3 shrink-0 transition-transform duration-150 ${open ? "rotate-90" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    >
      <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The in-place rename field. Selects the stem but not the extension, so
 *  typing replaces the name and leaves `.md` alone — retyping the extension
 *  every rename is how a file quietly becomes the wrong type. */
function RenameInput({
  initial,
  onCommit,
  onCancel,
}: {
  initial: string;
  onCommit: (next: string) => void;
  onCancel: () => void;
}) {
  return (
    <input
      autoFocus
      defaultValue={initial}
      onFocus={(e) => {
        const dot = initial.lastIndexOf(".");
        e.target.setSelectionRange(0, dot > 0 ? dot : initial.length);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          onCommit((e.target as HTMLInputElement).value);
        } else if (e.key === "Escape") {
          e.preventDefault();
          onCancel();
        }
      }}
      // Cancel on blur rather than commit: a blur is as likely to be the user
      // clicking away from a rename they thought better of.
      onBlur={onCancel}
      className="w-full rounded-md border border-[#4f8dff]/50 bg-black/30 px-1.5 py-0.5 text-xs text-neutral-100 focus:outline-none"
    />
  );
}

export interface NoteTreeProps {
  /** Folder an OS file-drag is hovering — a different gesture from moving a
   *  note inside the tree, so a different highlight. */
  fileDropTarget: string | null;
  /** Returns true when it handled an OS drop, so the internal move path can
   *  bow out. */
  onFileDrop: (e: React.DragEvent, folder: string) => boolean;
  onFileDropTargetChange: (folder: string | null) => void;
  /** Path currently being renamed — its row becomes an input. Renaming has to
   *  happen in the tree because Tauri's webview has no `window.prompt`. */
  renaming: string | null;
  onRenameCommit: (path: string, nextName: string) => void;
  onRenameCancel: () => void;
  onContextMenu: (e: React.MouseEvent, path: string, kind: "note" | "folder") => void;
  nodes: TreeNode[];
  selected: string | null;
  /** Folder paths currently expanded. */
  expanded: Set<string>;
  /** Folder the user is dragging a note over, for the drop highlight. */
  dropTarget: string | null;
  dirty: boolean;
  /** Folder whose delete is armed — its × becomes an explicit confirm. */
  armedFolder: string | null;
  /** Every folder path, for the per-folder "move to" picker. */
  folderPaths: string[];
  onMoveFolder: (from: string, toFolder: string) => void;
  depth?: number;
  onToggleFolder: (path: string) => void;
  onSelectNote: (path: string) => void;
  onNewNoteIn: (folder: string) => void;
  onNewFolderIn: (folder: string) => void;
  onDeleteFolder: (path: string) => void;
  onMoveNote: (from: string, toFolder: string) => void;
  onDropTargetChange: (path: string | null) => void;
}

/** The vault's folder tree. Notes drag onto folders to move — the fastest way
 *  to file something, and the interaction people already expect from a notes
 *  app. Every drag action also has a menu equivalent in the note header,
 *  since drag-and-drop is unusable by keyboard. */
export function NoteTree(props: NoteTreeProps) {
  const {
    nodes,
    selected,
    expanded,
    dropTarget,
    fileDropTarget,
    onFileDrop,
    onFileDropTargetChange,
    renaming,
    onRenameCommit,
    onRenameCancel,
    onContextMenu,
    dirty,
    armedFolder,
    folderPaths,
    onMoveFolder,
    depth = 0,
    onToggleFolder,
    onSelectNote,
    onNewNoteIn,
    onNewFolderIn,
    onDeleteFolder,
    onMoveNote,
    onDropTargetChange,
  } = props;

  return (
    <ul className="space-y-0.5">
      {nodes.map((node) => {
        const indent = { paddingLeft: `${depth * 10 + 4}px` };

        if (node.kind === "note") {
          const isSelected = selected === node.path;
          if (renaming === node.path) {
            return (
              <li key={node.path} style={indent}>
                <RenameInput
                  initial={node.name}
                  onCommit={(next) => onRenameCommit(node.path, next)}
                  onCancel={onRenameCancel}
                />
              </li>
            );
          }
          return (
            <li key={node.path}>
              <button
                onContextMenu={(e) => onContextMenu(e, node.path, "note")}
                type="button"
                draggable
                onDragStart={(e) => {
                  // `note:`/`folder:` prefix so the drop handler knows which
                  // move it is without re-deriving it from the tree.
                  e.dataTransfer.setData("text/plain", `note:${node.path}`);
                  e.dataTransfer.effectAllowed = "move";
                }}
                onClick={() => onSelectNote(node.path)}
                title={node.path}
                style={indent}
                className={`flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left text-xs transition duration-150 ${
                  isSelected
                    ? "bg-[#4f8dff]/15 text-[#4f8dff]"
                    : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
                }`}
              >
                {/* Muted against the label: at this size the icon is for
                    scanning the shape of a folder, not for reading. */}
                <FileIcon kind={kindOf(node.path)} className={isSelected ? "h-3.5 w-3.5" : "h-3.5 w-3.5 opacity-50"} />
                <span className="truncate">{node.name}</span>
                {isSelected && dirty && (
                  <span className="text-amber-400" title="unsaved changes">
                    •
                  </span>
                )}
              </button>
            </li>
          );
        }

        if (renaming === node.path) {
          return (
            <li key={node.path} style={indent}>
              <RenameInput
                initial={node.name}
                onCommit={(next) => onRenameCommit(node.path, next)}
                onCancel={onRenameCancel}
              />
            </li>
          );
        }

        const isOpen = expanded.has(node.path);
        const isDropTarget = dropTarget === node.path;
        const isFileDropTarget = fileDropTarget === node.path;
        return (
          <li key={node.path}>
            <div
              draggable
              onContextMenu={(e) => onContextMenu(e, node.path, "folder")}
              onDragStart={(e) => {
                e.stopPropagation();
                e.dataTransfer.setData("text/plain", `folder:${node.path}`);
                e.dataTransfer.effectAllowed = "move";
              }}
              onDragOver={(e) => {
                e.preventDefault();
                e.stopPropagation();
                // Files from the OS are copied in; a note from the tree is
                // moved. The cursor says which before anything happens.
                if (Array.from(e.dataTransfer.types).includes("Files")) {
                  e.dataTransfer.dropEffect = "copy";
                  if (fileDropTarget !== node.path) onFileDropTargetChange(node.path);
                  return;
                }
                e.dataTransfer.dropEffect = "move";
                if (dropTarget !== node.path) onDropTargetChange(node.path);
              }}
              onDragLeave={() => {
                if (dropTarget === node.path) onDropTargetChange(null);
                if (fileDropTarget === node.path) onFileDropTargetChange(null);
              }}
              onDrop={(e) => {
                if (onFileDrop(e, node.path)) return;
                e.preventDefault();
                e.stopPropagation();
                const payload = e.dataTransfer.getData("text/plain");
                onDropTargetChange(null);
                if (payload) onMoveNote(payload, node.path);
              }}
              style={indent}
              className={`group flex items-center gap-1 rounded-md py-1 pr-1 transition ${
                isFileDropTarget
                  ? "bg-emerald-400/15 ring-1 ring-dashed ring-emerald-400/50"
                  : isDropTarget
                    ? "bg-[#4f8dff]/20 ring-1 ring-[#4f8dff]/40"
                    : "hover:bg-white/5"
              }`}
            >
              <button
                type="button"
                onClick={() => onToggleFolder(node.path)}
                title={isOpen ? `collapse ${node.path}` : `expand ${node.path}`}
                className="flex min-w-0 flex-1 items-center gap-1 text-left text-xs text-neutral-300"
              >
                <Chevron open={isOpen} />
                <span className="truncate">{node.name}</span>
                <span className="shrink-0 text-[10px] text-neutral-600">
                  {node.children.filter((c) => c.kind === "note").length || ""}
                </span>
              </button>
              {/* Revealed on hover so a deep tree isn't a wall of buttons —
                  except while armed, when the confirm must stay visible. */}
              <span
                className={`flex shrink-0 items-center transition ${
                  armedFolder === node.path ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                }`}
              >
                <button
                  type="button"
                  onClick={() => onNewNoteIn(node.path)}
                  title={`new note in ${node.path}`}
                  className="rounded px-1 text-[11px] text-neutral-500 hover:text-neutral-100"
                >
                  +
                </button>
                <button
                  type="button"
                  onClick={() => onNewFolderIn(node.path)}
                  title={`new subfolder in ${node.path}`}
                  className="rounded px-1 text-[11px] text-neutral-500 hover:text-neutral-100"
                >
                  ⊞
                </button>
                {/* Keyboard-reachable equivalent of dragging the folder. Its
                    own subtree is excluded: a folder can't contain itself. */}
                <select
                  value=""
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => onMoveFolder(node.path, e.target.value)}
                  title={`move ${node.path}`}
                  className="max-w-[3.5rem] rounded border border-white/10 bg-black/30 px-0.5 text-[10px] text-neutral-500 hover:text-neutral-200 focus:outline-none"
                >
                  <option value="" disabled>
                    move
                  </option>
                  {folderPaths
                    .filter(
                      (f) =>
                        f !== node.path &&
                        !f.startsWith(`${node.path}/`) &&
                        f !== node.path.slice(0, node.path.lastIndexOf("/")),
                    )
                    .map((folder) => (
                      <option key={folder || "__root__"} value={folder}>
                        {folder || "(vault root)"}
                      </option>
                    ))}
                </select>
                <button
                  type="button"
                  onClick={() => onDeleteFolder(node.path)}
                  title={
                    armedFolder === node.path
                      ? `click again to delete ${node.path}`
                      : `delete ${node.path}`
                  }
                  className={`rounded px-1 text-[11px] transition ${
                    armedFolder === node.path
                      ? "bg-red-500/20 text-red-300"
                      : "text-neutral-600 hover:text-red-400"
                  }`}
                >
                  {armedFolder === node.path ? "confirm" : "×"}
                </button>
              </span>
            </div>
            {isOpen && node.children.length > 0 && (
              <div className="mt-0.5">
                <NoteTree {...props} nodes={node.children} depth={depth + 1} />
              </div>
            )}
            {isOpen && node.children.length === 0 && (
              <p
                style={{ paddingLeft: `${(depth + 1) * 10 + 16}px` }}
                className="py-0.5 text-[11px] text-neutral-600"
              >
                empty
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
