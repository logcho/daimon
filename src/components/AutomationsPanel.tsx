import { useEffect, useState } from "react";
import { createAutomation, deleteAutomation, listAutomations, setAutomationEnabled } from "../lib/api";
import type { Automation } from "../types";

type ListState = "loading" | "ready" | "error";
type CreateState = "idle" | "saving" | "error";

function formatRelativeTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diffMs = Date.now() - date.getTime();
  const diffSec = Math.round(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

// A one-shot reminder (`onceAt` set) shows its actual fire date/time instead
// of a raw cron expression, since there isn't one — see
// `Automation.onceAt`'s doc comment in types.ts.
function ScheduleLine({ automation }: { automation: Automation }) {
  if (automation.onceAt) {
    const date = new Date(automation.onceAt);
    const when = Number.isNaN(date.getTime()) ? automation.onceAt : date.toLocaleString();
    const fired = Boolean(automation.lastRunAt);
    return (
      <p className="mt-1 truncate text-xs text-neutral-500">
        {fired ? `reminded once — was set for ${when}` : `reminds you once — ${when}`}
      </p>
    );
  }
  return <p className="mt-1 truncate font-mono text-xs text-neutral-500">{automation.schedule}</p>;
}

function LastRunLine({ automation }: { automation: Automation }) {
  if (!automation.lastRunAt) return null;
  const when = formatRelativeTime(automation.lastRunAt);
  if (automation.lastRunStatus === "error") {
    return (
      <p className="mt-1 text-xs text-red-400">
        last ran {when} — error{automation.lastRunResult ? `: ${truncate(automation.lastRunResult, 80)}` : ""}
      </p>
    );
  }
  return (
    <p className="mt-1 text-xs text-emerald-400">
      last ran {when} — done
    </p>
  );
}

function AutomationRow({
  automation,
  onToggle,
  onDelete,
}: {
  automation: Automation;
  onToggle: (id: string, enabled: boolean) => void;
  onDelete: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState("");

  async function handleToggle() {
    setBusy(true);
    setRowError("");
    try {
      await setAutomationEnabled(automation.id, !automation.enabled);
      onToggle(automation.id, !automation.enabled);
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    setBusy(true);
    setRowError("");
    try {
      await deleteAutomation(automation.id);
      onDelete(automation.id);
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <li className="liquid-glass-subtle rounded-xl px-3 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${automation.enabled ? "bg-[#4f8dff]" : "bg-neutral-600"}`}
            />
            <span className="truncate text-sm font-medium text-neutral-100">{automation.name}</span>
          </div>
          <ScheduleLine automation={automation} />
          <LastRunLine automation={automation} />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={handleToggle}
            disabled={busy}
            title={automation.enabled ? "disable" : "enable"}
            className={`rounded-full border px-3 py-1 text-xs transition active:scale-95 disabled:opacity-40 ${
              automation.enabled
                ? "border-[#4f8dff]/40 bg-[#4f8dff]/15 text-[#4f8dff] hover:border-[#4f8dff]/60"
                : "border-white/15 bg-white/5 text-neutral-500 hover:border-white/25 hover:text-neutral-200"
            }`}
          >
            {automation.enabled ? "enabled" : "disabled"}
          </button>
          <button
            type="button"
            onClick={handleDelete}
            disabled={busy}
            title="delete automation"
            className="liquid-glass-subtle rounded-full px-2.5 py-1 text-xs text-neutral-400 transition duration-200 hover:text-red-400 hover:[border-color:rgba(248,113,113,0.4)] active:scale-95 disabled:opacity-40"
          >
            delete
          </button>
        </div>
      </div>
      {rowError && <p className="mt-2 text-xs text-red-400">{rowError}</p>}
    </li>
  );
}

export function AutomationsPanel() {
  const [listState, setListState] = useState<ListState>("loading");
  const [automations, setAutomations] = useState<Automation[]>([]);
  const [listErrorMessage, setListErrorMessage] = useState("");

  const [nameDraft, setNameDraft] = useState("");
  const [instructionDraft, setInstructionDraft] = useState("");
  const [scheduleDraft, setScheduleDraft] = useState("");
  const [createState, setCreateState] = useState<CreateState>("idle");
  const [createErrorMessage, setCreateErrorMessage] = useState("");

  // Remounts each time the automations tab becomes active (PipelinePanel only
  // renders this component while `view === "automations"`), so this mount
  // effect doubles as "refetch on every switch" — deliberate, since
  // automations can be created by the agent itself mid-conversation, not
  // only through this form.
  useEffect(() => {
    listAutomations()
      .then((a) => {
        setAutomations(a);
        setListState("ready");
      })
      .catch((err) => {
        setListErrorMessage(err instanceof Error ? err.message : String(err));
        setListState("error");
      });
  }, []);

  function handleToggled(id: string, enabled: boolean) {
    setAutomations((prev) => prev.map((a) => (a.id === id ? { ...a, enabled } : a)));
  }

  function handleDeleted(id: string) {
    setAutomations((prev) => prev.filter((a) => a.id !== id));
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const name = nameDraft.trim();
    const instruction = instructionDraft.trim();
    const schedule = scheduleDraft.trim();
    if (!name || !instruction || !schedule) return;

    setCreateState("saving");
    setCreateErrorMessage("");
    try {
      const created = await createAutomation(name, instruction, schedule);
      setAutomations((prev) => [...prev, created]);
      setNameDraft("");
      setInstructionDraft("");
      setScheduleDraft("");
      setCreateState("idle");
    } catch (err) {
      setCreateErrorMessage(err instanceof Error ? err.message : String(err));
      setCreateState("error");
    }
  }

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <h3 className="text-sm font-semibold tracking-tight text-neutral-100">automations</h3>

      {listState === "loading" && (
        <p className="mt-3 text-xs text-neutral-500">loading…</p>
      )}
      {listState === "error" && (
        <p className="mt-3 text-xs text-red-400">{listErrorMessage}</p>
      )}
      {listState === "ready" && automations.length === 0 && (
        <p className="mt-3 text-xs text-neutral-500">
          ○ no automations yet — ask daimon to schedule something recurring or remind you about
          something on a specific date, or add a recurring one below.
        </p>
      )}
      {listState === "ready" && automations.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {automations.map((automation) => (
            <AutomationRow
              key={automation.id}
              automation={automation}
              onToggle={handleToggled}
              onDelete={handleDeleted}
            />
          ))}
        </ul>
      )}

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">new_automation</h3>
        <form onSubmit={handleCreate} className="mt-4 space-y-2">
          <input
            type="text"
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            placeholder="daily brief"
            className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
          <textarea
            value={instructionDraft}
            onChange={(e) => setInstructionDraft(e.target.value)}
            placeholder="summarize my unread email and calendar for today"
            rows={2}
            className="w-full resize-none rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
          <input
            type="text"
            value={scheduleDraft}
            onChange={(e) => setScheduleDraft(e.target.value)}
            placeholder="0 8 * * *"
            className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
          <p className="text-xs leading-relaxed text-neutral-600">
            cron format — minute hour day month weekday, e.g. <span className="font-mono text-neutral-500">0 8 * * *</span> for
            daily at 8am.
          </p>
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={
                !nameDraft.trim() || !instructionDraft.trim() || !scheduleDraft.trim() || createState === "saving"
              }
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
            >
              {createState === "saving" ? "creating…" : "create"}
            </button>
            {createState === "error" && (
              <span className="text-xs text-red-400">{createErrorMessage}</span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
