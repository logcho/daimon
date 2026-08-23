import { fmtTokens } from "./lib/format";
import {
  emptyUsage,
  type AgentEvent,
  type ChatMessage,
  type Step,
  type StepEvent,
  type TodoItem,
  type TurnUsage,
} from "./types";

// Deliberately transport-free: this file is the fold from the agent's event
// stream to what a UI renders, and it is exactly the same fold whether those
// events arrived over Tauri's event channel, an NDJSON body, or a WebSocket
// from the other side of a tailnet. `onSessionStatus` used to live here and
// pulled in @tauri-apps for one line, which made the whole module
// desktop-only for no reason; it now sits with the rest of the Tauri
// bindings in api.ts.

function lastAssistant(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant") return i;
  }
  return -1;
}

/**
 * True while the session's last assistant message still has an open Thinking
 * step — a turn is in flight. Derived per session (the chat chips pulse per
 * session; the pill pulses when any session is busy).
 */
export function isSessionBusy(messages: ChatMessage[]): boolean {
  const idx = lastAssistant(messages);
  return idx >= 0 && messages[idx].thinking;
}

/**
 * True when the session has a completed turn (result delivered, not thinking,
 * no error) — the chip dot shows green instead of grey.
 */
export function hasCompletedTurn(messages: ChatMessage[]): boolean {
  const idx = lastAssistant(messages);
  if (idx < 0) return false;
  const msg = messages[idx];
  return !!msg.content && !msg.thinking && !msg.error;
}

/**
 * Fold a todo event into a session's checklist.
 *
 * Deliberately separate from `applyEvent`: todos are session state, not
 * message state. Attaching them to the last assistant message meant a new turn
 * started with an empty list, so the checklist vanished at exactly the point
 * the work got long enough to want one. `cli/live.py` never had this problem —
 * `LiveState.todos` is turn state that never enters the transcript.
 *
 * The event carries the whole list every time, so a client that missed one
 * still converges.
 */
export function applyTodoEvent(todos: TodoItem[], event: AgentEvent): TodoItem[] {
  return event.type === "todo" ? event.items : todos;
}

function upsertStep(steps: Step[], ev: StepEvent): Step[] {
  const next: Step = {
    id: ev.id,
    label: ev.label,
    tool: ev.tool,
    status: ev.status,
    parent_step_id: ev.parent_step_id,
    subagent_query: ev.subagent_query,
    detail: ev.detail,
    elapsed_ms: ev.elapsed_ms,
    agent_id: ev.agent_id,
    agent_label: ev.agent_label,
  };
  const idx = steps.findIndex((s) => s.id === ev.id);
  if (idx < 0) return [...steps, next];
  // Merge rather than replace: a `done` event carries the timing but a client
  // shouldn't lose the detail the `running` event brought.
  return steps.map((s, i) => (i === idx ? { ...s, ...next, detail: next.detail ?? s.detail } : s));
}

function addUsage(usage: TurnUsage | undefined, ev: Extract<AgentEvent, { type: "usage" }>): TurnUsage {
  const base = usage ?? emptyUsage();
  return {
    inputTokens: base.inputTokens + (ev.input_tokens ?? 0),
    outputTokens: base.outputTokens + (ev.output_tokens ?? 0),
    cacheReadTokens: base.cacheReadTokens + (ev.cache_read_tokens ?? 0),
    // One unpriced call and the whole total is unknown — the alternative is a
    // number that looks complete and isn't.
    costUsd:
      ev.cost_usd === undefined || base.costUsd === null
        ? null
        : base.costUsd + ev.cost_usd,
    // Context is the *current* prompt, not the sum of every prompt, and only
    // the main agent's — a sub-agent's context isn't yours.
    contextTokens: ev.role === "pro" ? (ev.input_tokens ?? 0) : base.contextTokens,
  };
}

/**
 * Fold one agent event into the message list. Pure — returns a new list.
 *
 * chat-only scope: ui_action / host_action / live_frame are handled elsewhere
 * (App.tsx routes ui_action to a terminal tab) and ignored here, and `todo`
 * goes to `applyTodoEvent` because it is session state rather than message
 * state. `ask` never reaches the app: the server only offers the ask tools to
 * clients that advertise the capability on /task.
 */
export function applyEvent(messages: ChatMessage[], event: AgentEvent): ChatMessage[] {
  const idx = lastAssistant(messages);
  if (idx < 0) {
    // No assistant message yet — only done/error can meaningfully start one.
    if (event.type === "done") {
      return [...messages, { id: crypto.randomUUID(), role: "assistant", content: event.result, steps: [], thinking: false }];
    }
    if (event.type === "error") {
      return [...messages, { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: false, error: event.message }];
    }
    return messages;
  }

  let next: ChatMessage = messages[idx];

  switch (event.type) {
    case "step":
      if (event.label === "Thinking") {
        next = { ...next, thinking: event.status === "running" };
      } else {
        next = { ...next, steps: upsertStep(next.steps, event) };
      }
      break;
    case "assistant_delta":
      // The model's own reasoning trace is not the answer; a sub-agent's
      // narration isn't the user's transcript either.
      if (event.channel === "reasoning" || event.agent_id) break;
      next = { ...next, content: next.content + event.text };
      break;
    case "usage":
      next = { ...next, usage: addUsage(next.usage, event) };
      break;
    case "continuation":
      next = {
        ...next,
        notices: [
          ...(next.notices ?? []),
          {
            id: crypto.randomUUID(),
            kind: "continuation",
            text: `continuing · step ${event.steps}${event.tokens ? ` · ${fmtTokens(event.tokens)} tokens` : ""}`,
          },
        ],
      };
      break;
    case "compaction":
      next = {
        ...next,
        notices: [
          ...(next.notices ?? []),
          {
            id: crypto.randomUUID(),
            kind: "compaction",
            text: `compacted context ${fmtTokens(event.before_tokens)} → ${fmtTokens(event.after_tokens)} tokens`,
          },
        ],
      };
      break;
    case "retry":
      next = {
        ...next,
        notices: [
          ...(next.notices ?? []),
          {
            id: crypto.randomUUID(),
            kind: "retry",
            text: `connection lost — retrying (${event.attempt}/${event.max_attempts})`,
          },
        ],
      };
      break;
    case "done":
      next = {
        ...next,
        // Replaces rather than appends: the deltas already built this text, but
        // `done` is authoritative if any were missed.
        content: event.result,
        thinking: false,
        error: undefined,
        usage: event.usage
          ? {
              inputTokens: event.usage.input_tokens,
              outputTokens: event.usage.output_tokens,
              cacheReadTokens: event.usage.cache_read_tokens,
              costUsd: event.usage.cost_usd ?? null,
              contextTokens: next.usage?.contextTokens ?? 0,
            }
          : next.usage,
        elapsedMs: next.startedAt ? Date.now() - next.startedAt : next.elapsedMs,
      };
      break;
    case "error":
      if (next.content) return messages; // result already delivered — ignore straggler stream errors
      next = { ...next, error: event.message, thinking: false };
      break;
    default:
      return messages; // ui_action / host_action / live_frame: handled elsewhere
  }
  return messages.map((m, i) => (i === idx ? next : m));
}

/** Session-wide totals for the status bar — the bar spans the session, not one
 *  turn, so this sums what each turn reported. */
export function sessionTotals(messages: ChatMessage[]): {
  turns: number;
  usage: TurnUsage;
  lastElapsedMs?: number;
} {
  let turns = 0;
  let lastElapsedMs: number | undefined;
  const usage = emptyUsage();
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    if (message.content && !message.thinking) turns++;
    if (message.elapsedMs) lastElapsedMs = message.elapsedMs;
    if (!message.usage) continue;
    usage.inputTokens += message.usage.inputTokens;
    usage.outputTokens += message.usage.outputTokens;
    usage.cacheReadTokens += message.usage.cacheReadTokens;
    usage.costUsd =
      message.usage.costUsd === null || usage.costUsd === null
        ? null
        : usage.costUsd + message.usage.costUsd;
    // The newest turn's context is the current one.
    if (message.usage.contextTokens) usage.contextTokens = message.usage.contextTokens;
  }
  return { turns, usage, lastElapsedMs };
}


/**
 * Rebuild a transcript from a recorded event stream.
 *
 * `applyEvent` folds events into the *current* turn; this folds a whole
 * session's history, which differs in one place: a `user` event opens a new
 * exchange rather than being ignored. That event exists precisely so a client
 * replaying a session it never saw gets both halves of the conversation
 * instead of a monologue.
 */
export function foldSnapshot(messages: ChatMessage[], event: AgentEvent): ChatMessage[] {
  if ((event as { type: string }).type === "user") {
    const text = String((event as unknown as { text?: string }).text ?? "");
    return [
      ...messages,
      { id: crypto.randomUUID(), role: "user", content: text, steps: [], thinking: false },
      { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: false },
    ];
  }
  return applyEvent(messages, event);
}
