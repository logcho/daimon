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

export type TaskEvent = StepEvent | DoneEvent | ErrorEvent | UiActionEvent;
