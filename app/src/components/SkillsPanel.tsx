import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { deleteSkill, listSkills, readSkill, writeSkill } from "../api";
import type { SkillFile } from "../types";
import { DeleteButton } from "./DeleteButton";

/** A SKILL.md without its `---` frontmatter block. The name and description
 *  are already shown in the header; rendered as markdown the fences turn into
 *  a horizontal rule with loose `key: value` text under it. */
function stripFrontmatter(text: string): string {
  if (!text.startsWith("---")) return text;
  const end = text.indexOf("\n---", 3);
  return end === -1 ? text : text.slice(end + 4).replace(/^\n+/, "");
}

type ListState = "loading" | "ready" | "error";
type DetailState = "idle" | "loading" | "ready" | "error";

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

/** Two-pane skill browser, sharing VaultPanel's shape — skills are markdown
 *  files like notes are, so they read the same way.
 *
 *  Editable, but only SKILL.md. The agent writes skills with save_skill and
 *  an installed one is a directory that can hold scripts and reference
 *  documents — those are better changed in a real editor. What this view is
 *  for is the file that decides whether a skill gets used at all: fixing a
 *  description the agent keeps misreading is a one-line change, and having to
 *  leave the app to make it is why it doesn't get made.
 *
 *  Refetches on mount (the component only renders when view === "skills"), so
 *  a skill the agent just saved shows up without a restart. */
export function SkillsPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [skills, setSkills] = useState<SkillFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selected, setSelected] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  /** The file as it is on disk, frontmatter and all — what the editor shows.
   *  The preview strips the frontmatter, and saving *that* back would delete
   *  the name and description the agent finds the skill by. */
  const [raw, setRaw] = useState("");
  /** Last text known to match the file, so `raw !== saved` is the dirty
   *  check — the same one the vault uses. */
  const [saved, setSaved] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [detailErrorMessage, setDetailErrorMessage] = useState("");
  // Failures from actions rather than from viewing a skill. Kept separate
  // because the detail error only renders while something is selected, and a
  // delete clears the selection before its request has even finished.
  const [actionErrorMessage, setActionErrorMessage] = useState("");

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [filter, setFilter] = useState("");

  /** Switching skills with unsaved edits would drop them silently, so flush
   *  first — the same guarantee the vault gives a note. */
  function selectAnother(name: string) {
    if (name === selected) return;
    if (selected && raw !== saved && !saving) {
      void handleSave().then(() => handleOpen(name));
      return;
    }
    handleOpen(name);
  }

  function handleOpen(name: string) {
    if (name === selected) return;
    setSelected(name);
    setActionErrorMessage("");
    setEditing(false);
    setDetailState("loading");
    readSkill(name)
      .then((skill) => {
        setRaw(skill.content);
        setSaved(skill.content);
        setDetailState("ready");
      })
      .catch((err) => {
        setDetailErrorMessage(err instanceof Error ? err.message : String(err));
        setDetailState("error");
      });
  }

  useEffect(() => {
    listSkills()
      .then((s) => {
        setSkills(s);
        setListState("ready");
        if (s.length > 0) handleOpen(s[0].name);
      })
      .catch((err) => {
        setListErrorMessage(err instanceof Error ? err.message : String(err));
        setListState("error");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dirty = raw !== saved;

  /** Save the edited SKILL.md.
   *
   *  Not optimistic, unlike the delete below: the server re-reads the
   *  frontmatter to work out what the skill is now called, and an edit to the
   *  `name:` line renames it. Guessing that here and being wrong would leave
   *  the selection pointing at a skill that no longer exists. */
  async function handleSave() {
    if (!selected || saving) return;
    setSaving(true);
    setActionErrorMessage("");
    try {
      const result = await writeSkill(selected, raw);
      setSaved(raw);
      setEditing(false);
      // The name and description may both have changed, so re-list rather
      // than patching the row — the grouping is off `source` and the filter
      // is off `description`, and a stale row would be wrong in both.
      const refreshed = await listSkills();
      setSkills(refreshed);
      if (result.name !== selected) setSelected(result.name);
    } catch (err) {
      setActionErrorMessage(
        `couldn't save ${selected}: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setSaving(false);
    }
  }

  /** ⌘S saves, ⌘E toggles edit — the same two the vault binds, because a
   *  SKILL.md is a markdown file and behaves like one. */
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key === "s") {
        e.preventDefault();
        if (selected && dirty && !saving) void handleSave();
      } else if (e.key === "e") {
        e.preventDefault();
        if (selected && detailState === "ready") setEditing((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, dirty, saving, raw, detailState]);

  /** Optimistic: the row goes as soon as it's confirmed, and the request runs
   *  behind it. The decision is already made at that point — waiting on a
   *  round trip through Tauri, the agent server and the filesystem just makes
   *  the UI look stuck. If the delete does fail, the skill comes back where it
   *  was and says why. */
  function handleDelete(name: string) {
    const index = skills.findIndex((s) => s.name === name);
    if (index === -1) return;
    const removed = skills[index];

    setActionErrorMessage("");
    setSkills((prev) => prev.filter((s) => s.name !== name));
    if (selected === name) {
      setSelected(null);
      setRaw("");
      setSaved("");
      setEditing(false);
      setDetailState("idle");
    }

    deleteSkill(name).catch((err) => {
      // Splice back at the original index rather than appending — the list is
      // ordered (and grouped off that order), so a failed delete shouldn't
      // quietly reshuffle it.
      setSkills((prev) => {
        if (prev.some((s) => s.name === name)) return prev;
        const restored = [...prev];
        restored.splice(Math.min(index, restored.length), 0, removed);
        return restored;
      });
      setActionErrorMessage(
        `couldn't delete ${name}: ${err instanceof Error ? err.message : String(err)}`,
      );
    });
  }

  const selectedMeta = selected ? skills.find((s) => s.name === selected) : undefined;

  // Filter on description too, not just name — a skill's description is the
  // condition under which it applies, which is usually what you're looking for.
  const needle = filter.trim().toLowerCase();
  const visible = needle
    ? skills.filter(
        (s) =>
          s.name.toLowerCase().includes(needle) ||
          s.description.toLowerCase().includes(needle),
      )
    : skills;

  // Project skills first: they're the ones specific to what you're working on.
  const groups: { label: string; items: SkillFile[] }[] = [
    { label: "project", items: visible.filter((s) => s.source === "project") },
    { label: "global", items: visible.filter((s) => s.source === "vault") },
  ].filter((g) => g.items.length > 0);

  return (
    <div className="flex min-h-0 flex-1">
      <div
        className={`themed-scroll shrink-0 overflow-y-auto border-r border-white/10 transition-[width,opacity,padding] duration-200 ${
          sidebarCollapsed ? "w-0 overflow-hidden p-0 opacity-0" : "w-44 p-3 opacity-100"
        }`}
      >
        <h3 className="px-1 text-xs font-semibold uppercase tracking-wider text-neutral-500">skills</h3>
        {listState === "ready" && skills.length > 3 && (
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter…"
            className="mt-2 w-full rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/50 focus:outline-none"
          />
        )}
        {listState === "loading" && <p className="mt-2 px-1 text-xs text-neutral-500">loading…</p>}
        {listState === "error" && <p className="mt-2 px-1 text-xs text-red-400">{listErrorMessage}</p>}
        {listState === "ready" && skills.length === 0 && (
          <p className="mt-2 px-1 text-xs text-neutral-500">
            ○ empty — the agent saves skills as it finds procedures worth reusing; “/skills find” in the CLI installs published ones
          </p>
        )}
        {listState === "ready" && skills.length > 0 && visible.length === 0 && (
          <p className="mt-2 px-1 text-xs text-neutral-500">nothing matches “{filter}”</p>
        )}
        {groups.map((group) => (
          <div key={group.label} className="mt-3">
            <p className="px-1 text-[10px] uppercase tracking-wider text-neutral-600">{group.label}</p>
            <ul className="mt-1 space-y-0.5">
              {group.items.map((skill) => (
                <li key={`${skill.source}/${skill.name}`}>
                  <button
                    type="button"
                    onClick={() => selectAnother(skill.name)}
                    title={skill.description || skill.name}
                    className={`w-full truncate rounded-md px-2 py-1 text-left text-xs transition duration-150 ${
                      selected === skill.name
                        ? "bg-[#4f8dff]/15 text-[#4f8dff]"
                        : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
                    }`}
                  >
                    {skill.name}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <div className="themed-scroll min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mb-2 flex items-center gap-2">
          <button
            type="button"
            onClick={() => setSidebarCollapsed((c) => !c)}
            title={sidebarCollapsed ? "show skill list" : "hide skill list"}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
          >
            <SidebarToggleIcon collapsed={sidebarCollapsed} />
          </button>
          {selected && (
            <h3 className="truncate text-sm font-semibold tracking-tight text-neutral-100">
              {selected}
              {dirty && <span className="ml-1 text-amber-400">•</span>}
            </h3>
          )}
          {selectedMeta?.source === "project" && (
            <span className="shrink-0 rounded bg-white/5 px-1.5 py-0.5 text-[10px] text-neutral-400">
              project
            </span>
          )}
          {selected && (
            <span className="ml-auto flex shrink-0 items-center gap-1">
              {dirty && (
                <button
                  type="button"
                  onClick={() => void handleSave()}
                  disabled={saving}
                  title="save (⌘S)"
                  className="rounded-md bg-[#4f8dff]/15 px-2 py-0.5 text-xs text-[#4f8dff] transition hover:bg-[#4f8dff]/25 disabled:opacity-50"
                >
                  {saving ? "saving…" : "save"}
                </button>
              )}
              {detailState === "ready" && (
                <button
                  type="button"
                  onClick={() => setEditing((v) => !v)}
                  title="toggle edit/preview (⌘E)"
                  className="rounded-md px-2 py-0.5 text-xs text-neutral-500 transition hover:bg-white/5 hover:text-neutral-100"
                >
                  {editing ? "preview" : "edit"}
                </button>
              )}
              <DeleteButton name={selected} onDelete={() => handleDelete(selected)} />
            </span>
          )}
        </div>
        {actionErrorMessage && (
          <p className="mb-3 rounded-md border border-red-500/20 bg-red-500/10 px-2 py-1 text-xs text-red-300">
            {actionErrorMessage}
          </p>
        )}
        {selectedMeta?.description && (
          <p className="-mt-1 mb-3 text-xs text-neutral-500">{selectedMeta.description}</p>
        )}

        {!selected && <p className="text-sm text-neutral-500">Select a skill to preview.</p>}
        {selected && detailState === "loading" && <p className="text-sm text-neutral-500">loading…</p>}
        {selected && detailState === "error" && <p className="text-sm text-red-400">{detailErrorMessage}</p>}
        {selected && detailState === "ready" && editing && (
          <>
            <textarea
              value={raw}
              onChange={(e) => setRaw(e.target.value)}
              spellCheck={false}
              className="themed-scroll min-h-[60vh] w-full resize-none rounded-md border border-white/10 bg-black/20 p-3 font-mono text-sm leading-relaxed text-neutral-200 focus:border-[#4f8dff]/40 focus:outline-none"
            />
            <p className="mt-2 text-[11px] leading-relaxed text-neutral-600">
              The <code className="rounded bg-white/5 px-1">---</code> block at the top is what the
              agent reads to decide whether a skill applies — the body is only loaded once it does.
              Editing <code className="rounded bg-white/5 px-1">name:</code> renames the skill.
            </p>
          </>
        )}

        {selected && detailState === "ready" && !editing && (
          <article
            className="daimon-prose prose prose-invert prose-sm max-w-none"
            onDoubleClick={() => setEditing(true)}
            title="double-click to edit"
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{stripFrontmatter(raw)}</ReactMarkdown>
          </article>
        )}
      </div>
    </div>
  );
}
