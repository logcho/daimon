export type StepStatus = "pending" | "running" | "done" | "error";

export interface TaskStep {
  id: string;
  label: string;
  status: StepStatus;
}

export interface Task {
  id: string;
  instruction: string;
  steps: TaskStep[];
}
