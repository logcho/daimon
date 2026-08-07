import type { ReminderFiredEvent } from "../types";

function BellIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 shrink-0" stroke="#4f8dff" strokeWidth="1.5">
      <path
        d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// A dedicated, hard-to-miss surface for fired reminders — deliberately NOT
// styled like a normal chat bubble/session entry (see App.tsx's
// `onReminderFired` handler doc comment for why: landing a reminder as just
// another session was the exact complaint this exists to fix). Rendered by
// PipelinePanel above its tab content so it's visible regardless of which
// tab is currently active, and persists across tab switches until
// dismissed — the underlying `firedReminders` list lives in App.tsx, not
// here, so it also survives a collapse-to-pill/expand cycle.
//
// No new accent color (see the daimon-design-system skill: one accent only,
// no orange/amber) — urgency here comes from forced full-panel-width
// placement above everything else and a stronger glow, not a second hue.
export function ReminderAlert({
  reminders,
  onDismiss,
}: {
  reminders: ReminderFiredEvent[];
  onDismiss: (id: string) => void;
}) {
  if (reminders.length === 0) return null;

  return (
    <div className="flex shrink-0 flex-col gap-2 border-b border-white/[0.08] p-3">
      {reminders.map((reminder) => (
        <div
          key={reminder.id}
          className="animate-daimon-in liquid-glass rounded-2xl p-3 [border-color:rgba(79,141,255,0.45)] [box-shadow:0_0_24px_rgba(79,141,255,0.25)]"
        >
          <div className="flex items-start gap-2.5">
            <BellIcon />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold tracking-tight text-neutral-100">{reminder.name}</p>
              <p className="mt-0.5 text-sm leading-relaxed text-neutral-300">
                {reminder.result ?? (reminder.status === "error" ? "Something went wrong." : "Done.")}
              </p>
            </div>
            <button
              type="button"
              onClick={() => onDismiss(reminder.id)}
              title="dismiss"
              className="shrink-0 rounded-full px-1.5 text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90"
            >
              ×
            </button>
          </div>
          <button
            type="button"
            onClick={() => onDismiss(reminder.id)}
            className="mt-2.5 w-full rounded-full border px-3 py-1.5 text-xs font-medium text-neutral-200 transition [border-color:rgba(79,141,255,0.4)] hover:bg-[#4f8dff]/10 active:scale-95"
          >
            Got it
          </button>
        </div>
      ))}
    </div>
  );
}
