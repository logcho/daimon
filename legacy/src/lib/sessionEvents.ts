import type { Session, TaskStep, Turn } from "../types";
import type { WorkspaceEvent } from "./api";

function applyToTurn(turn: Turn, event: WorkspaceEvent): Turn {
  if (event.type === "step") {
    const steps = [...turn.steps];
    const index = steps.findIndex((s) => s.id === event.id);
    const step: TaskStep = { id: event.id, label: event.label, status: event.status, tool: event.tool };
    if (index >= 0) steps[index] = step;
    else steps.push(step);
    return { ...turn, steps };
  }
  // Both `done` and `error` clear `liveFrame` — the turn is over, so there's
  // nothing left to show "live"; the frontend falls back to the recordings
  // list (see ScreenPanel.tsx) once this turn's last frame stops updating.
  if (event.type === "done") return { ...turn, result: event.result, liveFrame: undefined };
  if (event.type === "error") return { ...turn, error: event.message, liveFrame: undefined };
  if (event.type === "live_frame") return { ...turn, liveFrame: event.data };
  return turn; // "ui_action" — handled as a side effect in App.tsx, not turn/chat content
}

/// Applies an event to the session's most recently started turn — i.e. the
/// last element of `turns`. A session always has at least one turn once it
/// exists, so this assumes non-empty and is a no-op otherwise.
export function applySessionEvent(session: Session, event: WorkspaceEvent): Session {
  if (session.turns.length === 0) return session;
  const turns = [...session.turns];
  const lastIndex = turns.length - 1;
  turns[lastIndex] = applyToTurn(turns[lastIndex], event);
  return { ...session, turns };
}
