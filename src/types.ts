export type StepStatus = "pending" | "running" | "done" | "error";

export interface TaskStep {
  id: string;
  label: string;
  status: StepStatus;
  tool?: string;
}

export interface Turn {
  id: string;
  instruction: string;
  steps: TaskStep[];
  result?: string;
  error?: string;
  // The latest base64-encoded screenshot of the background browser, while
  // this turn is actively running — see `agents/src/run.ts`'s periodic
  // `live_frame` events. Cleared once the turn finishes (done or error),
  // since there's nothing left to show "live."
  liveFrame?: string;
}

export interface Session {
  id: string;
  turns: Turn[];
}

export interface ConnectedAccount {
  email: string;
  connectedAt: string;
}

export interface VaultPathStatus {
  path: string;
  isDefault: boolean;
}

export interface VaultFile {
  name: string;
  sizeBytes: number;
  modifiedAt: string;
}

export interface RecordingFile {
  name: string;
  sizeBytes: number;
  modifiedAt: string;
}

export interface Automation {
  id: string;
  name: string;
  instruction: string;
  // Recurring cron expression — empty/unused when `onceAt` is set instead.
  schedule: string;
  // A one-time reminder's fire time (ISO datetime) instead of a recurring
  // schedule — mutually exclusive with `schedule` being meaningful. See
  // `src-tauri/src/automation.rs`'s `Automation::once_at` doc comment.
  onceAt: string | null;
  enabled: boolean;
  createdAt: string;
  lastRunAt: string | null;
  lastRunStatus: "done" | "error" | null;
  lastRunResult: string | null;
}

// Emitted once, over its own dedicated `reminder-fired` event channel (see
// `src-tauri/src/automation.rs`'s `emit_reminder_fired`), the moment a
// one-shot reminder finishes running — separate from the generic
// `session-status` channel that same run also streams over, since landing a
// fired reminder as just another entry in the ordinary session list turned
// out to be too easy to miss. `id` matches the underlying `Automation.id`,
// used to key React lists and to let a user dismiss one specific reminder
// out of several pending ones.
export interface ReminderFiredEvent {
  id: string;
  name: string;
  status: "done" | "error";
  result: string | null;
}

// A reusable procedure the agent decided to remember on its own
// (save_skill), or one the user authored directly (SkillsPanel.tsx) — both
// go through the same list/create/delete commands and the same retrieval
// path, so nothing distinguishes the two once saved. See
// `src-tauri/src/skills.rs`'s module doc comment for the underlying
// storage.
export interface Skill {
  id: string;
  name: string;
  description: string;
  // Milliseconds since epoch, not an ISO string — matches how
  // `agents/src/memory.ts` originally wrote this column.
  createdAt: number;
}

export interface VoiceModelStatus {
  downloaded: boolean;
  modelName: string;
}

export type DictationEvent =
  | { type: "recording"; locked: boolean }
  | { type: "transcribing" }
  | { type: "result"; text: string }
  // A recording produced nothing meaningful (silence, a brief accidental
  // trigger, background noise) — no text to show, just a signal that this
  // turn is over so the UI doesn't stay stuck showing "transcribing…"
  // indefinitely with nothing telling it otherwise.
  | { type: "no_speech" }
  | { type: "error"; message: string };

// Recording/transcribing are transient, backend-driven states surfaced on
// the pill (which can be visible on its own, collapsed, while dictation is
// active) and mirrored in the panel header when it's open. "error" persists
// briefly then self-clears since there's no dedicated surface to dismiss it
// from when the panel is collapsed.
export type DictationState = "idle" | "recording" | "transcribing" | "error";

// A dictation result needs to land in the chat input for review — tagged
// with a fresh id on every event so repeating the same phrase twice in a row
// is still treated as a new value the panel's effect should react to.
export interface PendingDraft {
  text: string;
  id: string;
}

// Same shape/reasoning as PendingDraft, but for the terminal tab: a
// dictation result that should be typed into the live PTY instead of the
// chat input when the terminal tab is the one currently active.
export interface PendingInput {
  text: string;
  id: string;
}

// Which of PipelinePanel's tabs is currently showing. Lives here (not as a
// PipelinePanel-local useState) and is owned by App.tsx specifically so it
// survives a collapse-to-pill cycle — PipelinePanel fully unmounts whenever
// the panel collapses, so any state that should persist across that
// (like the terminal tabs/session list before it) has to live one level up.
// Without this, collapsing while on e.g. the terminal tab and re-expanding
// would always land back on "chat" instead of wherever the user actually
// left off.
export type View = "chat" | "vault" | "automations" | "skills" | "terminal" | "screen" | "settings";
