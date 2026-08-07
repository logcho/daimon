import { useState } from "react";

// Settings.tsx had grown to 757 lines with 32 useState hooks and no
// abstraction: every provider section (Anthropic key, Google client id,
// Spotify client id, vault path) reimplemented the same
// draft/saveState/errorMessage triplet and the same
// try/catch/setTimeout(2500) save handler, and the Spotify account block was
// a near-verbatim copy of the Google one. These are the four shapes that were
// being copied.

export type SaveState = "idle" | "saving" | "saved" | "error";

/** Shared button styling — glass pill with press feedback, per the design system. */
export const BUTTON_CLASS =
  "liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 " +
  "transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] " +
  "active:scale-95 disabled:opacity-40";

const INPUT_CLASS =
  "w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm " +
  "text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none " +
  "focus:ring-2 focus:ring-[#4f8dff]/20";

/**
 * One settings section. `divider` is false only for the first section on the
 * page, which has nothing above it to separate from.
 */
export function Section({
  title,
  divider = true,
  children,
}: {
  title: string;
  divider?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={divider ? "mt-8 border-t border-white/5 pt-6" : ""}>
      <h3 className="text-sm font-semibold tracking-tight text-neutral-100">{title}</h3>
      {children}
    </div>
  );
}

/** The `● configured` / `○ not set` / `checking…` status line every section opens with. */
export function StatusLine({
  state,
  children,
}: {
  state: "checking" | "ok" | "unset" | "error";
  children?: React.ReactNode;
}) {
  if (state === "checking") return <p className="mt-1 text-xs text-neutral-500">checking…</p>;
  const tone =
    state === "ok" ? "text-emerald-400" : state === "error" ? "text-red-400" : "text-neutral-500";
  const marker = state === "ok" ? "● " : state === "error" ? "" : "○ ";
  return (
    <p className={`mt-1 text-xs leading-relaxed ${tone}`}>
      {marker}
      {children}
    </p>
  );
}

/**
 * A single-value text/password setting: input, save button, transient "saved"
 * flash, inline error. Replaces four hand-rolled copies of the same form.
 */
export function TextSetting({
  placeholder,
  password = false,
  savedLabel = "saved",
  onSave,
}: {
  placeholder: string;
  password?: boolean;
  savedLabel?: string;
  onSave: (value: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState("");
  const [state, setState] = useState<SaveState>("idle");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const value = draft.trim();
    if (!value) return;
    setState("saving");
    try {
      await onSave(value);
      setDraft("");
      setState("saved");
      setTimeout(() => setState("idle"), 2500);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setState("error");
    }
  }

  return (
    <form onSubmit={submit} className="mt-4 space-y-2">
      <input
        type={password ? "password" : "text"}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={placeholder}
        className={INPUT_CLASS}
      />
      <div className="flex items-center gap-3">
        <button type="submit" disabled={!draft.trim() || state === "saving"} className={BUTTON_CLASS}>
          {state === "saving" ? "saving…" : "save"}
        </button>
        {state === "saved" && <span className="text-xs text-emerald-400">{savedLabel}</span>}
        {state === "error" && <span className="text-xs text-red-400">{error}</span>}
      </div>
    </form>
  );
}

/**
 * The glass row used for anything with a connect/disconnect or action button
 * on the right — Gmail, Spotify, browser login, the fn-key permission prompt.
 */
export function SettingRow({
  label,
  children,
  action,
}: {
  label: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="liquid-glass-subtle mt-3 flex items-center justify-between rounded-xl px-3 py-2.5">
      <div>
        <p className="text-sm font-medium text-neutral-100">{label}</p>
        {children}
      </div>
      {action}
    </div>
  );
}
