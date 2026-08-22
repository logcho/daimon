import { useEffect, useState } from "react";
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
export function Notes({ bus, workspace }: { bus: BusClient; workspace: string }) {
  const [notes, setNotes] = useState<VaultFile[]>([]);
  const [open, setOpen] = useState<{ name: string; content: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const ack = await bus.send("vault.list", { workspace });
      if (ack.ok) setNotes((ack.notes ?? []) as VaultFile[]);
      else setError(ack.error ?? "could not list notes");
    })();
  }, [bus, workspace]);

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
            <button onClick={() => setEditing(true)} className="text-xs text-[#4f8dff]">edit</button>
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
      {error && <p className="pb-2 text-xs text-red-400">{error}</p>}
      {notes.length === 0 && <p className="px-1 py-6 text-center text-sm text-neutral-500">no notes yet</p>}
      {notes.map((note) => (
        <button
          key={note.name}
          onClick={() => openNote(note.name)}
          className="mb-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-left active:bg-white/10"
        >
          <div className="truncate text-sm text-neutral-100">{note.name}</div>
          <div className="text-xs text-neutral-500">
            {new Date(note.modifiedAt).toLocaleDateString()} · {Math.max(1, Math.round(note.sizeBytes / 1024))} KB
          </div>
        </button>
      ))}
    </div>
  );
}
