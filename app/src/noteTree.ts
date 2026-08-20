/** Building the vault's folder tree from the two flat lists the server returns.
 *
 *  Notes come back as full relative paths (`projects/2026/kickoff.md`) and
 *  folders as their own list. Folders have to be merged in explicitly rather
 *  than inferred from note paths alone: a folder you just created holds no
 *  notes yet, so inferring would make it vanish the moment it's drawn.
 */

import type { VaultFile } from "./types";

export interface FolderNode {
  kind: "folder";
  /** Segment name, e.g. `2026`. */
  name: string;
  /** Full path from the vault root, e.g. `projects/2026`. */
  path: string;
  children: TreeNode[];
}

export interface NoteNode {
  kind: "note";
  /** Filename only, e.g. `kickoff.md`. */
  name: string;
  /** Full path, which is also the note's id everywhere else. */
  path: string;
  file: VaultFile;
}

export type TreeNode = FolderNode | NoteNode;

/** The folder containing `path`, or "" for a note at the vault root. */
export function parentFolder(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? "" : path.slice(0, slash);
}

/** The last path segment — a note's filename, or a folder's own name. */
export function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** Join a folder and a name, tolerating the empty (root) folder. */
export function joinPath(folder: string, name: string): string {
  return folder ? `${folder}/${name}` : name;
}

/**
 * Build the nested tree. Folders sort before notes and both sort
 * alphabetically — a tree is for finding things by where you filed them,
 * which recency ordering actively works against.
 */
export function buildTree(files: VaultFile[], folders: string[]): TreeNode[] {
  const root: FolderNode = { kind: "folder", name: "", path: "", children: [] };
  const folderIndex = new Map<string, FolderNode>([["", root]]);

  /** Get (creating if needed) the node for a folder path, building any
   *  intermediate folders on the way down. */
  function ensureFolder(path: string): FolderNode {
    const existing = folderIndex.get(path);
    if (existing) return existing;
    const parent = ensureFolder(parentFolder(path));
    const node: FolderNode = { kind: "folder", name: basename(path), path, children: [] };
    parent.children.push(node);
    folderIndex.set(path, node);
    return node;
  }

  for (const folder of folders) {
    if (folder) ensureFolder(folder);
  }
  for (const file of files) {
    // A note's own folder may not be in `folders` (the agent can write a
    // nested path directly), so create it on demand rather than dropping
    // the note at the root where it would look misfiled.
    ensureFolder(parentFolder(file.name)).children.push({
      kind: "note",
      name: basename(file.name),
      path: file.name,
      file,
    });
  }

  function sortChildren(node: FolderNode) {
    node.children.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "folder" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const child of node.children) {
      if (child.kind === "folder") sortChildren(child);
    }
  }
  sortChildren(root);

  return root.children;
}

/** Every folder path in the tree, root first — for a "move to…" picker. */
export function allFolderPaths(folders: string[]): string[] {
  return ["", ...[...new Set(folders)].filter(Boolean).sort()];
}
