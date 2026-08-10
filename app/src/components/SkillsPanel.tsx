import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { listSkills, readSkill } from "../api";
import type { SkillFile } from "../types";

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
 *  Read-only on purpose: the agent writes skills with save_skill, and editing
 *  a plain markdown file is something a real editor already does better. The
 *  point of this view is seeing what the agent has to work with.
 *
 *  Refetches on mount (the component only renders when view === "skills"), so
 *  a skill the agent just saved shows up without a restart. */
export function SkillsPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [skills, setSkills] = useState<SkillFile[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [selected, setSelected] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [content, setContent] = useState("");
  const [detailErrorMessage, setDetailErrorMessage] = useState("");

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [filter, setFilter] = useState("");

  function handleOpen(name: string) {
    setSelected(name);
    setDetailState("loading");
    readSkill(name)
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
    { label: "vault", items: visible.filter((s) => s.source === "vault") },
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
            ○ empty — the agent saves skills here as it finds procedures worth reusing
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
                    onClick={() => handleOpen(skill.name)}
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
            <h3 className="truncate text-sm font-semibold tracking-tight text-neutral-100">{selected}</h3>
          )}
          {selectedMeta?.source === "project" && (
            <span className="shrink-0 rounded bg-white/5 px-1.5 py-0.5 text-[10px] text-neutral-400">
              project
            </span>
          )}
        </div>
        {selectedMeta?.description && (
          <p className="-mt-1 mb-3 text-xs text-neutral-500">{selectedMeta.description}</p>
        )}

        {!selected && <p className="text-sm text-neutral-500">Select a skill to preview.</p>}
        {selected && detailState === "loading" && <p className="text-sm text-neutral-500">loading…</p>}
        {selected && detailState === "error" && <p className="text-sm text-red-400">{detailErrorMessage}</p>}
        {selected && detailState === "ready" && (
          <article className="daimon-prose prose prose-invert prose-sm max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          </article>
        )}
      </div>
    </div>
  );
}
