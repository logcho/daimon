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

export interface Automation {
  id: string;
  name: string;
  instruction: string;
  schedule: string;
  enabled: boolean;
  createdAt: string;
  lastRunAt: string | null;
  lastRunStatus: "done" | "error" | null;
  lastRunResult: string | null;
}
