// The agent's NDJSON event contract (mirrors agents/src/daimon_agent/events.py).

export type StepStatus = "pending" | "running" | "done" | "error";

export interface StepEvent {
  type: "step";
  id: string;
  label: string;
  status: StepStatus;
  tool: string | null;
}

export interface DoneEvent {
  type: "done";
  result: string;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface UiActionEvent {
  type: "ui_action";
  action: string;
  command?: string;
}

export interface HostActionEvent {
  type: "host_action";
  action: string;
  [key: string]: unknown;
}

export interface LiveFrameEvent {
  type: "live_frame";
  data: string;
}

export type AgentEvent =
  | StepEvent
  | DoneEvent
  | ErrorEvent
  | UiActionEvent
  | HostActionEvent
  | LiveFrameEvent;

export interface SessionStatusPayload {
  session_id: string;
  event: AgentEvent;
}

export interface Step {
  id: string;
  label: string;
  tool: string | null;
  status: StepStatus;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  steps: Step[];
  /** Assistant only: the Thinking step is open (turn in flight). */
  thinking: boolean;
  error?: string;
}

export interface AgentStatus {
  running: boolean;
  adopted: boolean;
  port: number;
  busy: boolean;
  active_turns: number;
  sessions: string[];
}

/** Rust → webview terminal events (not part of the agent's NDJSON contract). */
export interface TerminalOutputPayload {
  id: string;
  /** Base64-encoded raw pty bytes. */
  data: string;
}

export interface TerminalExitedPayload {
  id: string;
  /** Exit code, or null when the shell was killed by a signal. */
  code: number | null;
}

export type View = "chat" | "terminal";
