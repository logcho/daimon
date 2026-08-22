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

/** The whole checklist every time — a snapshot, not a patch.
 *
 *  Todos belong to the *session*, not to any one message: they describe what
 *  the agent is doing now, and a checklist that scrolls away with the turn
 *  that created it disappears exactly when the work gets long enough to need
 *  one. See `applyTodoEvent` and the pinned strip above the composer — the
 *  same split `cli/live.py` makes, where todos live in LiveState and never
 *  enter the transcript. */
export interface TodoEvent {
  type: "todo";
  items: TodoItem[];
}

/** The turn hit the graph's step cap and is carrying on from the checkpoint.
 *  Not an error and not a pause — progress worth showing. */
export interface ContinuationEvent {
  type: "continuation";
  steps: number;
  max_steps: number;
  tokens: number;
}

/** A model call died mid-stream and is being restarted. Not an error — the
 *  turn is still running — but silence and a retry look identical from the
 *  outside, and the retry is the one not to worry about. */
export interface RetryEvent {
  type: "retry";
  attempt: number;
  max_attempts: number;
  reason: string;
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
  | ContinuationEvent
  | CompactionEvent
  | RetryEvent
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

/** A stored step. Mirrors StepEvent — they drifted once, and the display is
 *  only as good as what it keeps. */
export interface Step {
  id: string;
  label: string;
  tool: string | null;
  status: StepStatus;
  /** Set when this step came from a sub-agent — id of the spawn step. */
  parent_step_id?: string;
  /** The query text that spawned this sub-agent. */
  subagent_query?: string;
  /** Short summary of the call's arguments (a path, a query). */
  detail?: string;
  elapsed_ms?: number;
  /** Which sub-agent this belongs to, so concurrent ones group. */
  agent_id?: string;
  agent_label?: string;
}

/** Running totals for one assistant turn. `costUsd` goes null the moment any
 *  call used a model with no known price — a partial total reads as a complete
 *  one, which is worse than admitting it's unknown. */
export interface TurnUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  costUsd: number | null;
  /** Input tokens of the most recent main-agent call — the live context size. */
  contextTokens: number;
}

export const emptyUsage = (): TurnUsage => ({
  inputTokens: 0,
  outputTokens: 0,
  cacheReadTokens: 0,
  costUsd: 0,
  contextTokens: 0,
});

/** A one-line notice in the transcript — continuing past the step cap,
 *  compacting the context, retrying a dropped connection. Not an error, and
 *  not a tool call. */
export interface Notice {
  id: string;
  kind: "continuation" | "compaction" | "retry";
  text: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  steps: Step[];
  /** Assistant only: the Thinking step is open (turn in flight). */
  thinking: boolean;
  error?: string;
  /** Assistant only: tokens and cost for this turn. */
  usage?: TurnUsage;
  notices?: Notice[];
  /** Wall-clock start, for the elapsed readout. */
  startedAt?: number;
  elapsedMs?: number;
}

export interface AgentStatus {
  running: boolean;
  port: number;
  busy: boolean;
  active_turns: number;
  sessions: string[];
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
