import { useEffect, useState } from "react";
import { deleteAutomation, listAutomations, setAutomationEnabled } from "../lib/api";
import type { Automation } from "../types";

// Replaces AutomationsPanel, which presented one-shot reminders and recurring
// cron jobs as peers behind a raw-cron-string create form. Reminders are the
// capability people actually reach for — set by voice, fire once, announce
// themselves — so they lead here, and recurring automations collapse into a
// secondary section. There is deliberately no create form: scheduling happens
// by asking in chat or by voice, which is the whole point of the feature.

function isReminder(a: Automation): boolean {
  return a.onceAt !== null;
}

/** "Today 5:00 PM", "Tomorrow 9:00 AM", "Fri 9:00 AM", "Mar 3, 9:00 AM". */
function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;

  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(date) - startOfDay(new Date())) / 86_400_000);

  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  if (days === -1) return `Yesterday ${time}`;
  // Inside the coming week a weekday name is the most readable form; past
  // that it stops being unambiguous and a date is clearer.
  if (days > 1 && days < 7) return `${date.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
  return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${time}`;
}

function relativeTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function LastRun({ automation }: { automation: Automation }) {
  if (!automation.lastRunAt) return null;
  const when = relativeTime(automation.lastRunAt);
  if (automation.lastRunStatus === "error") {
    return (
      <p className="mt-1 text-xs text-red-400">
        ran {when} — error
        {automation.lastRunResult ? `: ${truncate(automation.lastRunResult, 80)}` : ""}
      </p>
    );
  }
  return <p className="mt-1 text-xs text-emerald-400">ran {when} — done</p>;
}

/** A pending or already-fired one-shot reminder. */
function ReminderRow({ reminder, onDelete }: { reminder: Automation; onDelete: (id: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A reminder is disabled by the scheduler once it fires (not deleted), so
  // "already happened" is exactly `!enabled` — no clock comparison needed,
  // which also keeps a reminder that fired while the app was closed correct.
  const fired = !reminder.enabled;

  async function remove() {
    setBusy(true);
    setError("");
    try {
      await deleteAutomation(reminder.id);
      onDelete(reminder.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <li className="liquid-glass-subtle rounded-xl px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${fired ? "bg-neutral-600" : "bg-[#4f8dff]"}`}
            />
            <span className={`text-xs ${fired ? "text-neutral-500" : "text-[#4f8dff]"}`}>
              {formatWhen(reminder.onceAt ?? "")}
            </span>
          </div>
          <p
            className={`mt-1 truncate text-sm ${fired ? "text-neutral-500" : "font-medium text-neutral-100"}`}
          >
            {reminder.name}
          </p>
          <LastRun automation={reminder} />
          {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
        </div>
        <button
          type="button"
          onClick={remove}
          disabled={busy}
          title={fired ? "dismiss" : "cancel this reminder"}
          className="shrink-0 rounded-full px-2 text-sm text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90 disabled:opacity-40"
        >
          ×
        </button>
      </div>
    </li>
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
  const [error, setError] = useState("");

  async function act(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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
          <p className="mt-1 truncate font-mono text-xs text-neutral-500">{automation.schedule}</p>
          <LastRun automation={automation} />
          {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() =>
              act(async () => {
                await setAutomationEnabled(automation.id, !automation.enabled);
                onToggle(automation.id, !automation.enabled);
              })
            }
            disabled={busy}
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
            onClick={() =>
              act(async () => {
                await deleteAutomation(automation.id);
                onDelete(automation.id);
              })
            }
            disabled={busy}
            className="liquid-glass-subtle rounded-full px-2.5 py-1 text-xs text-neutral-400 transition duration-200 hover:text-red-400 hover:[border-color:rgba(248,113,113,0.4)] active:scale-95 disabled:opacity-40"
          >
            delete
          </button>
        </div>
      </div>
    </li>
  );
}

export function ScheduledPanel() {
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [items, setItems] = useState<Automation[]>([]);
  const [error, setError] = useState("");
  const [showRecurring, setShowRecurring] = useState(false);

  // Remounts whenever this tab becomes active (PipelinePanel only renders it
  // while `view === "scheduled"`), so this doubles as refetch-on-switch —
  // necessary, since the agent creates most of these itself mid-conversation.
  useEffect(() => {
    listAutomations()
      .then((all) => {
        setItems(all);
        setState("ready");
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
        setState("error");
      });
  }, []);

  const remove = (id: string) => setItems((prev) => prev.filter((a) => a.id !== id));
  const toggle = (id: string, enabled: boolean) =>
    setItems((prev) => prev.map((a) => (a.id === id ? { ...a, enabled } : a)));

  // Pending reminders first (soonest first), then already-fired ones (most
  // recent first) — the top of the list is what's coming, not what's done.
  const reminders = items.filter(isReminder).sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    const at = new Date(a.onceAt ?? 0).getTime();
    const bt = new Date(b.onceAt ?? 0).getTime();
    return a.enabled ? at - bt : bt - at;
  });
  const recurring = items.filter((a) => !isReminder(a));

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <h3 className="text-xs font-medium uppercase tracking-widest text-neutral-500">upcoming</h3>

      {state === "loading" && <p className="mt-3 text-xs text-neutral-500">loading…</p>}
      {state === "error" && <p className="mt-3 text-xs text-red-400">{error}</p>}

      {state === "ready" && reminders.length === 0 && (
        <p className="mt-3 text-xs leading-relaxed text-neutral-500">
          ○ nothing scheduled — ask daimon to remind you about something and it'll show up here.
        </p>
      )}
      {state === "ready" && reminders.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {reminders.map((reminder) => (
            <ReminderRow key={reminder.id} reminder={reminder} onDelete={remove} />
          ))}
        </ul>
      )}

      {state === "ready" && recurring.length > 0 && (
        <div className="mt-8 border-t border-white/5 pt-6">
          <button
            type="button"
            onClick={() => setShowRecurring((open) => !open)}
            className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-widest text-neutral-500 transition hover:text-neutral-300"
          >
            <span className={`transition-transform duration-200 ${showRecurring ? "rotate-90" : ""}`}>
              ▸
            </span>
            recurring ({recurring.length})
          </button>
          {showRecurring && (
            <ul className="mt-3 space-y-1.5">
              {recurring.map((automation) => (
                <AutomationRow
                  key={automation.id}
                  automation={automation}
                  onToggle={toggle}
                  onDelete={remove}
                />
              ))}
            </ul>
          )}
        </div>
      )}

      {state === "ready" && (
        <p className="mt-8 text-xs leading-relaxed text-neutral-600">
          scheduling happens in conversation — "remind me friday at 9 to follow up on the tesla
          posting", or "every morning at 8, summarize my unread email". dictation (⌘⇧D) works for
          this too.
        </p>
      )}
    </div>
  );
}
