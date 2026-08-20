import type { TaskEvent } from "./events.js";

// The MCP tool server (tools.ts) is built exactly once, at module load, and
// lives for the whole process — but several of its tools work by pushing a
// `ui_action`/`host_action` event out to the daemon rather than returning a
// value to the model, and the NDJSON response stream those events go down
// belongs to *one turn*. Under LangGraph this was solved by rebuilding the
// whole tool array every turn closed over that turn's `emit` (the old
// `buildDaimonTools(emit)` factory); the SDK registers its MCP servers once
// at query construction, so that no longer works.
//
// Instead the active turn's sink lives here and the tools read it at call
// time. Safe because turns are serialized per session — `Session.runTurn`
// (agent.ts) chains every turn onto the previous one's promise, so exactly
// one turn is ever in flight in this process.
let activeEmit: ((event: TaskEvent) => void) | null = null;

export function setActiveEmit(emit: ((event: TaskEvent) => void) | null): void {
  activeEmit = emit;
}

// Deliberately a silent no-op rather than a throw when no turn is active. A
// tool call can land here after its turn's HTTP response has already been
// closed (an aborted turn whose in-flight browser action still resolves),
// and losing a stray `host_action` on a turn nobody is listening to is
// correct — crashing the process over it is not.
export function emitTaskEvent(event: TaskEvent): void {
  activeEmit?.(event);
}
