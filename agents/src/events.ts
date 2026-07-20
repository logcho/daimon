export type StepStatus = "pending" | "running" | "done" | "error";

export interface StepEvent {
  type: "step";
  id: string;
  label: string;
  status: StepStatus;
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
