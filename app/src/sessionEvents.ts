import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AgentEvent,
  ChatMessage,
  SessionStatusPayload,
  Step,
  StepEvent,
} from "./types";

export const onSessionStatus = (
  handler: (payload: SessionStatusPayload) => void,
): Promise<UnlistenFn> => listen<SessionStatusPayload>("session-status", (e) => handler(e.payload));

function lastAssistant(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant") return i;
  }
  return -1;
}

function upsertStep(steps: Step[], ev: StepEvent): Step[] {
  const idx = steps.findIndex((s) => s.id === ev.id);
  if (idx < 0) return [...steps, { id: ev.id, label: ev.label, tool: ev.tool, status: ev.status }];
  return steps.map((s, i) => (i === idx ? { ...s, status: ev.status, label: ev.label } : s));
}

/**
 * Fold one agent event into the message list. Pure — returns a new list.
 * chat-only scope: ui_action / host_action / live_frame events are ignored.
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
    case "done":
      next = { ...next, content: event.result, thinking: false, error: undefined };
      break;
    case "error":
      next = { ...next, error: event.message, thinking: false };
      break;
    default:
      return messages; // ui_action / host_action / live_frame: ignored for now
  }
  return messages.map((m, i) => (i === idx ? next : m));
}
