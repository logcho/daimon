// The agent's NDJSON event contract (mirrors agents/src/daimon_agent/events.py).

export type StepStatus = "pending" | "running" | "done" | "error";

export interface StepEvent {
  type: "step";
  id: string;
  label: string;
  status: StepStatus;
  tool: string | null;
  /** Set when this step came from a research sub-agent — id of the spawn step. */
  parent_step_id?: string;
  /** Research query text that spawned this sub-agent. */
  subagent_query?: string;
  /** Short summary of the call's arguments (a path, a query) for display. */
  detail?: string;
  elapsed_ms?: number;
  /** Sub-agent this step belongs to, so concurrent agents group instead of interleave. */
  agent_id?: string;
  agent_label?: string;
}

/** A chunk of assistant text as the model writes it. The full text still
 *  arrives on `done`, so ignoring these loses nothing. */
export interface AssistantDeltaEvent {
  type: "assistant_delta";
  text: string;
  /** Absent means the answer itself; "reasoning" is the model's thinking trace. */
  channel?: "reasoning";
  agent_id?: string;
}

/** Token usage for one model call. `cost_usd` is absent for an unpriced model. */
export interface UsageEvent {
  type: "usage";
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd?: number;
  role?: string;
  agent_id?: string;
}

export type TodoStatus = "pending" | "in_progress" | "done";

export interface TodoItem {
  id: string;
  text: string;
  status: TodoStatus;
}

/** The whole checklist every time — a snapshot, not a patch. */
export interface TodoEvent {
  type: "todo";
  items: TodoItem[];
}

export interface CompactionEvent {
  type: "compaction";
  before_tokens: number;
  after_tokens: number;
  dropped: number;
}

export interface UsageTotals {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd?: number;
}

export interface DoneEvent {
  type: "done";
  result: string;
  usage?: UsageTotals;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface AskOption {
  label: string;
  description: string;
}

/** Terminal event: the turn suspended on an interrupt and is awaiting an
 *  answer. Reply with POST /resume carrying this `id`; the turn continues from
 *  where it paused. */
export interface AskEvent {
  type: "ask";
  id: string;
  kind: "question" | "plan";
  question: string;
  options: AskOption[];
  multi_select: boolean;
  plan?: string;
  header?: string;
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
  | AssistantDeltaEvent
  | UsageEvent
  | TodoEvent
  | CompactionEvent
  | DoneEvent
  | ErrorEvent
  | AskEvent
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
  /** Set when this step came from a research sub-agent — id of the spawn step. */
  parent_step_id?: string;
  /** Research query text that spawned this sub-agent. */
  subagent_query?: string;
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

export type View = "chat" | "terminal" | "vault" | "skills" | "settings";

// --- Vault ------------------------------------------------------------------

export interface VaultFile {
  name: string;
  sizeBytes: number;
  modifiedAt: string;
}

// --- Skills -----------------------------------------------------------------

/** A reusable procedure the agent can follow. `source` is "vault" (the user's
 *  library, which follows them between projects) or "project" (stored with the
 *  repo in .daimon/skills). A project skill shadows a vault one of the same
 *  name — the same precedence the agent applies. */
export interface SkillFile {
  name: string;
  description: string;
  source: "vault" | "project";
}

// --- Voice dictation -------------------------------------------------------

export interface VoiceModelStatus {
  downloaded: boolean;
  modelName: string;
}

export interface VoiceModelDownloadPayload {
  downloadedBytes: number;
  totalBytes: number | null;
  done: boolean;
}

export type DictationStatus =
  | { type: "idle" }
  | { type: "recording"; locked: boolean }
  | { type: "transcribing" }
  | { type: "result"; text: string }
  | { type: "no_speech" }
  | { type: "error"; message: string }
  | { type: "locked_updated"; locked: boolean };

/** Insert target: which input receives dictated text. */
export type InsertTarget =
  | { kind: "chat"; setText: (text: string) => void }
  | { kind: "terminal"; paste: (text: string) => void }
  | null;
