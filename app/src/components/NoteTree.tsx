import type { TreeNode } from "../noteTree";

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

export interface NoteTreeProps {
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
          return (
            <li key={node.path}>
              <button
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
                className={`w-full truncate rounded-md py-1 pr-2 text-left text-xs transition duration-150 ${
                  isSelected
                    ? "bg-[#4f8dff]/15 text-[#4f8dff]"
                    : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
                }`}
              >
                {node.name}
                {isSelected && dirty && (
                  <span className="ml-1 text-amber-400" title="unsaved changes">
                    •
                  </span>
                )}
              </button>
            </li>
          );
        }

        const isOpen = expanded.has(node.path);
        const isDropTarget = dropTarget === node.path;
        return (
          <li key={node.path}>
            <div
              draggable
              onDragStart={(e) => {
                e.stopPropagation();
                e.dataTransfer.setData("text/plain", `folder:${node.path}`);
                e.dataTransfer.effectAllowed = "move";
              }}
              onDragOver={(e) => {
                e.preventDefault();
                e.stopPropagation();
                e.dataTransfer.dropEffect = "move";
                if (dropTarget !== node.path) onDropTargetChange(node.path);
              }}
              onDragLeave={() => {
                if (dropTarget === node.path) onDropTargetChange(null);
              }}
              onDrop={(e) => {
                e.preventDefault();
                e.stopPropagation();
                const payload = e.dataTransfer.getData("text/plain");
                onDropTargetChange(null);
                if (payload) onMoveNote(payload, node.path);
              }}
              style={indent}
              className={`group flex items-center gap-1 rounded-md py-1 pr-1 transition ${
                isDropTarget ? "bg-[#4f8dff]/20 ring-1 ring-[#4f8dff]/40" : "hover:bg-white/5"
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
