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

export type TaskEvent = StepEvent | DoneEvent | ErrorEvent;
