import { useEffect, useState } from "react";
import { createSkill, deleteSkill, listSkills } from "../lib/api";
import type { Skill } from "../types";

type ListState = "loading" | "ready" | "error";
type CreateState = "idle" | "saving" | "error";

function formatRelativeTime(epochMs: number): string {
  const diffSec = Math.round((Date.now() - epochMs) / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return new Date(epochMs).toLocaleDateString();
}

function SkillRow({ skill, onDelete }: { skill: Skill; onDelete: (id: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState("");

  async function handleDelete() {
    setBusy(true);
    setRowError("");
    try {
      await deleteSkill(skill.id);
      onDelete(skill.id);
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <li className="liquid-glass-subtle rounded-xl px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-neutral-100">{skill.name}</p>
          <p className="mt-1 text-xs leading-relaxed text-neutral-400">{skill.description}</p>
          <p className="mt-1 text-xs text-neutral-600">saved {formatRelativeTime(skill.createdAt)}</p>
        </div>
        <button
          type="button"
          onClick={handleDelete}
          disabled={busy}
          title="delete skill"
          className="liquid-glass-subtle shrink-0 rounded-full px-2.5 py-1 text-xs text-neutral-400 transition duration-200 hover:text-red-400 hover:[border-color:rgba(248,113,113,0.4)] active:scale-95 disabled:opacity-40"
        >
          delete
        </button>
      </div>
      {rowError && <p className="mt-2 text-xs text-red-400">{rowError}</p>}
    </li>
  );
}

export function SkillsPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [nameDraft, setNameDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [createState, setCreateState] = useState<CreateState>("idle");
  const [createErrorMessage, setCreateErrorMessage] = useState("");

  // Remounts each time the skills tab becomes active (PipelinePanel only
  // renders this component while `view === "skills"`), same "refetch on
  // every switch" pattern as AutomationsPanel — skills can be saved by the
  // agent itself mid-conversation (save_skill), not only through this form.
  useEffect(() => {
    listSkills()
      .then((s) => {
        setSkills(s);
        setListState("ready");
      })
      .catch((err) => {
        setListErrorMessage(err instanceof Error ? err.message : String(err));
        setListState("error");
      });
  }, []);

  function handleDeleted(id: string) {
    setSkills((prev) => prev.filter((s) => s.id !== id));
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const name = nameDraft.trim();
    const description = descriptionDraft.trim();
    if (!name || !description) return;

    setCreateState("saving");
    setCreateErrorMessage("");
    try {
      const created = await createSkill(name, description);
      setSkills((prev) => [created, ...prev]);
      setNameDraft("");
      setDescriptionDraft("");
      setCreateState("idle");
    } catch (err) {
      setCreateErrorMessage(err instanceof Error ? err.message : String(err));
      setCreateState("error");
    }
  }

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <h3 className="text-sm font-semibold tracking-tight text-neutral-100">skills</h3>
      <p className="mt-1 text-xs leading-relaxed text-neutral-500">
        reusable procedures daimon remembers — saved automatically when it completes something
        worth repeating, or add one yourself below.
      </p>

      {listState === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
      {listState === "error" && <p className="mt-3 text-xs text-red-400">{listErrorMessage}</p>}
      {listState === "ready" && skills.length === 0 && (
        <p className="mt-3 text-xs text-neutral-500">
          ○ no skills saved yet — daimon will save one automatically the first time it completes
          something reusable, or add one below.
        </p>
      )}
      {listState === "ready" && skills.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {skills.map((skill) => (
            <SkillRow key={skill.id} skill={skill} onDelete={handleDeleted} />
          ))}
        </ul>
      )}

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">new_skill</h3>
        <form onSubmit={handleCreate} className="mt-4 space-y-2">
          <input
            type="text"
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            placeholder="apply to a job posting"
            className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
          <textarea
            value={descriptionDraft}
            onChange={(e) => setDescriptionDraft(e.target.value)}
            placeholder="go to the company's careers page, find a role matching <role>, fill out the application with the user's info from the vault, and submit"
            rows={3}
            className="w-full resize-none rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
          <p className="text-xs leading-relaxed text-neutral-600">
            describe it generally (parameterized, not tied to one specific instance) so it's
            useful the next time something similar comes up.
          </p>
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!nameDraft.trim() || !descriptionDraft.trim() || createState === "saving"}
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
            >
              {createState === "saving" ? "saving…" : "save"}
            </button>
            {createState === "error" && <span className="text-xs text-red-400">{createErrorMessage}</span>}
          </div>
        </form>
      </div>
    </div>
  );
}
