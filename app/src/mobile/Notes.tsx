import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { BusClient } from "../lib/busClient";
import type { VaultFile } from "../types";

/**
 * The notes vault, read and edited from a phone.
 *
 * Notes are fetched one at a time rather than eagerly like the desktop panel
 * does — that panel reads *every* note on mount to build backlinks, which is
 * fine over loopback and is a lot of round trips over a tailnet. Backlinks and
 * wikilink resolution are deliberately left out here for the same reason;
 * `wikilinks.ts` is ready when they earn their cost.
 */
/** Two taps to delete rather than a dialog — see the note in Remote.tsx. */
function ConfirmButton({ label, onConfirm }: { label: string; onConfirm: () => void }) {
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(timer);
  }, [armed]);

  return (
    <button
      onClick={() => (armed ? onConfirm() : setArmed(true))}
      className={`shrink-0 rounded-xl px-3 py-2 text-xs ${
        armed ? "bg-red-500/20 text-red-300" : "text-neutral-500"
      }`}
    >
      {armed ? "sure?" : label}
    </button>
  );
}

export function Notes({
  bus,
  workspace,
  onVaultChanged,
}: {
  bus: BusClient;
  workspace: string;
  /** Registers this view's refresh so the shell can call it when the server
   *  says a note changed — the socket's callbacks are registered once, and
   *  cannot reach into a component that mounted later. */
  onVaultChanged: (refresh: (() => void) | null) => void;
}) {
  const [notes, setNotes] = useState<VaultFile[]>([]);
  const [open, setOpen] = useState<{ name: string; content: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [naming, setNaming] = useState(false);
  const [newName, setNewName] = useState("");

  const refresh = useCallback(async () => {
    const ack = await bus.send("vault.list", { workspace });
    if (ack.ok) setNotes((ack.notes ?? []) as VaultFile[]);
    else setError(ack.error ?? "could not list notes");
  }, [bus, workspace]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    onVaultChanged(() => void refresh());
    return () => onVaultChanged(null);
  }, [onVaultChanged, refresh]);

  async function createNote() {
    const trimmed = newName.trim();
    if (!trimmed) return;
    // The server only stores .md — the listing is a glob for it, so anything
    // else would be written once and then invisible. Add it rather than
    // bouncing the user for a detail they cannot be expected to know.
    const name = trimmed.endsWith(".md") ? trimmed : `${trimmed}.md`;
    const ack = await bus.send("vault.write", { workspace, name, content: "" });
    if (!ack.ok) {
      setError(ack.error ?? "could not create that note");
      return;
    }
    setNaming(false);
    setNewName("");
    await refresh();
    setOpen({ name, content: "" });
    setDraft("");
    setEditing(true);
  }

  async function removeNote(name: string) {
    const ack = await bus.send("vault.delete", { workspace, name });
    if (!ack.ok) {
      setError(ack.error ?? "could not delete that note");
      return;
    }
    if (open?.name === name) setOpen(null);
    await refresh();
  }

  async function openNote(name: string) {
    const ack = await bus.send("vault.read", { workspace, name });
    if (!ack.ok) {
      setError(ack.error ?? "could not read that note");
      return;
    }
    const content = String(ack.content ?? "");
    setOpen({ name, content });
    setDraft(content);
    setEditing(false);
  }

  async function save() {
    if (!open) return;
    setSaving(true);
    const ack = await bus.send("vault.write", { workspace, name: open.name, content: draft });
    setSaving(false);
    if (!ack.ok) {
      setError(ack.error ?? "could not save");
      return;
    }
    setOpen({ name: open.name, content: draft });
    setEditing(false);
  }

  if (open) {
    const dirty = editing && draft !== open.content;
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center gap-2 border-b border-white/10 px-3 py-2">
          <button onClick={() => setOpen(null)} className="text-sm text-neutral-400">‹ notes</button>
          <span className="min-w-0 flex-1 truncate text-xs text-neutral-500">{open.name}</span>
          {editing ? (
            <button
              onClick={save}
              disabled={saving || !dirty}
              className="rounded-lg bg-[#4f8dff] px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
            >
              {saving ? "saving…" : "save"}
            </button>
          ) : (
            <>
              <ConfirmButton label="delete" onConfirm={() => removeNote(open.name)} />
              <button onClick={() => setEditing(true)} className="text-xs text-[#4f8dff]">edit</button>
            </>
          )}
        </div>

        {error && <p className="px-3 py-2 text-xs text-red-400">{error}</p>}

        {editing ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="min-h-0 flex-1 resize-none bg-transparent px-4 py-3 font-mono text-sm text-neutral-200 outline-none"
            style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
          />
        ) : (
          <div className="prose prose-invert prose-sm min-h-0 max-w-none flex-1 overflow-y-auto px-4 py-3">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{open.content}</ReactMarkdown>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
      {error && (
        <button onClick={() => setError(null)} className="pb-2 text-left text-xs text-red-400">
          {error} — tap to dismiss
        </button>
      )}

      {naming ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void createNote();
          }}
          className="mb-3 flex gap-2"
        >
          <input
            autoFocus
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="notes/thing.md"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-white/5 px-4 py-2.5 text-sm text-neutral-100 outline-none focus:border-[#4f8dff]/60"
          />
          <button
            type="submit"
            disabled={!newName.trim()}
            className="rounded-2xl bg-[#4f8dff] px-4 py-2.5 text-sm font-medium text-white disabled:opacity-40"
          >
            create
          </button>
        </form>
      ) : (
        <button
          onClick={() => setNaming(true)}
          className="mb-3 w-full rounded-2xl border border-dashed border-white/15 px-4 py-3 text-sm text-neutral-400 active:bg-white/5"
        >
          + new note
        </button>
      )}
      {notes.length === 0 && <p className="px-1 py-6 text-center text-sm text-neutral-500">no notes yet</p>}
      {notes.map((note) => (
        <div
          key={note.name}
          className="mb-2 flex items-center gap-1 rounded-2xl border border-white/10 bg-white/5 pr-1"
        >
          <button
            onClick={() => openNote(note.name)}
            className="min-w-0 flex-1 px-4 py-3 text-left"
          >
            <div className="truncate text-sm text-neutral-100">{note.name}</div>
            <div className="text-xs text-neutral-500">
              {new Date(note.modifiedAt).toLocaleDateString()} · {Math.max(1, Math.round(note.sizeBytes / 1024))} KB
            </div>
          </button>
          <ConfirmButton label="delete" onConfirm={() => removeNote(note.name)} />
        </div>
      ))}
    </div>
  );
}
