export type StepStatus = "pending" | "running" | "done" | "error";

export interface StepEvent {
  type: "step";
  id: string;
  label: string;
  status: StepStatus;
  /** Tool name (e.g. "open_url"), when this step represents a real tool call. */
  tool?: string;
}

export interface DoneEvent {
  type: "done";
  result: string;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

// Emitted by the open_terminal_with_command tool (tools.ts) as a pure UI
// side-effect alongside its normal string return to the LLM — it carries no
// "done"/"error" status of its own because staging text into a terminal tab
// can't itself fail or complete, it's fire-and-forget for the frontend to
// react to. Deliberately "stage, don't execute": the daemon/agent never
// presses Enter on the user's behalf, matching the same principle already
// applied to voice-dictated text landing in an input without auto-submitting.
export interface UiActionEvent {
  type: "ui_action";
  action: "open_terminal_with_command";
  command: string;
}

// Emitted by the open_application tool (tools.ts) — unlike UiActionEvent
// above, this isn't something the *frontend* reacts to; it's a signal the
// Rust daemon itself intercepts as this NDJSON stream passes through
// `session.rs`'s `run_and_stream` and acts on directly (spawning a real
// `open -a <name>` process on the user's actual machine), since launching a
// native app has no UI-visible component the frontend would otherwise need
// to render. Fire-and-forget from here same as UiActionEvent — this
// container has no reverse channel to learn whether the launch actually
// succeeded (see automation.rs's module doc for the same "no callback path"
// constraint elsewhere in this architecture), so the tool's own return
// value to the model is necessarily optimistic. A discriminated union (not
// one flat interface with optional fields) so each action's exact payload
// shape is checked at compile time on both the emitting (tools.ts) and
// documenting (this file) side — `music_control`'s AppleScript surface is
// deliberately a short, fixed list of commands (see tools.ts's own comment
// on why), not a free-form string, to keep it from growing into a general
// "run arbitrary script" capability.
export type HostActionEvent =
  | { type: "host_action"; action: "open_application"; name: string }
  | { type: "host_action"; action: "close_application"; name: string }
  | {
      type: "host_action";
      action: "music_control";
      app: "Spotify" | "Music";
      command: "play" | "pause" | "next" | "previous";
    }
  // Opens a real file (e.g. one write_spreadsheet just generated) with
  // whatever app macOS has registered as the default for it — unlike
  // open_application, `path` names a file, not an app.
  | { type: "host_action"; action: "open_file"; path: string };

// Emitted periodically (see run.ts's LIVE_FRAME_INTERVAL_MS) for the
// duration of a turn — a base64-encoded PNG screenshot of whatever the
// background browser's content page currently shows, so the frontend can
// render a live-updating "watch it work" view instead of only a finished
// recording after the fact. Best-effort: a single failed screenshot (e.g.
// mid-navigation) just skips that tick rather than erroring the turn.
export interface LiveFrameEvent {
  type: "live_frame";
  data: string;
}

export type TaskEvent = StepEvent | DoneEvent | ErrorEvent | UiActionEvent | HostActionEvent | LiveFrameEvent;
