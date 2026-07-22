import { randomUUID } from "node:crypto";
import { AIMessage, BaseMessage, HumanMessage, ToolMessage } from "@langchain/core/messages";
import { GraphRecursionError } from "@langchain/langgraph";
import { buildAgent } from "./graph.js";
import { runDemoTask } from "./demo.js";
import { formatMemoryContext, recordTask, searchNotes, searchSkills, searchTasks } from "./memory.js";
import type { TaskEvent } from "./events.js";

// A companion to the daemon's own 90s stall timeout on the Rust side (see
// `src-tauri/src/session.rs`'s `STALL_TIMEOUT`) — deliberately shorter, so
// that on a genuine hang *this* timeout wins the race. It can attribute the
// failure to "the agent loop itself produced nothing", with detail visible
// via `docker logs`, rather than the daemon just seeing silence and guessing.
const INACTIVITY_TIMEOUT_MS = 60_000;

// LangGraph's own default recursion limit (25 graph "steps", i.e. roughly
// alternating agent/tool turns) is easy for a legitimately multi-step task
// (a job application with several fields and a confirmation click can
// plausibly need 20+ tool calls) to bump into, while still being low enough
// that a genuinely stuck open->read->open->read loop wastes real, paid
// Anthropic API turns before it's caught. 40 gives real tasks headroom
// without leaving a stuck loop effectively unbounded.
const RECURSION_LIMIT = 40;

// Phase 8: each container is now dedicated to exactly one session for its
// whole lifetime (the daemon reuses the same container/port across every
// message in a session — see `workspace.rs`'s `SESSION_PORTS` cache), so a
// single module-level array is the right scope for that session's growing
// conversation history — no session-id keying needed at this layer. It grows
// across every call to `runTurn` made against this process.
let conversation: BaseMessage[] = [];

export async function runTurn(instruction: string, emit: (event: TaskEvent) => void): Promise<void> {
  if (!process.env.ANTHROPIC_API_KEY) {
    console.error("[daimon-agent] no ANTHROPIC_API_KEY set, running demo task");
    await runDemoTask(instruction, emit);
    return;
  }

  const thinkingId = randomUUID();
  emit({ type: "step", id: thinkingId, label: "Thinking", status: "running" });

  // Recorded so a failed turn can roll `conversation` back to exactly where
  // it stood before this turn's HumanMessage — otherwise an error here would
  // leave a dangling, unanswered HumanMessage in place, and the *next* turn
  // would push a second one right after it. Anthropic's API requires strict
  // user/assistant alternation, so two Human messages in a row would break
  // every subsequent turn in the session, not just this one.
  const lengthBeforeThisTurn = conversation.length;
  conversation.push(new HumanMessage(instruction));

  try {
    const memoryContext = formatMemoryContext(
      searchTasks(instruction, 3),
      searchSkills(instruction, 3),
      searchNotes(instruction, 3),
    );
    if (memoryContext) console.error(`[daimon-agent] memory context:\n${memoryContext}`);
    const agent = buildAgent(memoryContext);
    console.error(`[daimon-agent] agent built, starting stream for instruction: ${instruction}`);
    const stream = await agent.stream(
      { messages: conversation },
      { streamMode: "updates", recursionLimit: RECURSION_LIMIT },
    );

    let finalResult = "";
    const iterator = (stream as AsyncIterable<Record<string, { messages?: unknown[] }>>)[Symbol.asyncIterator]();

    for (;;) {
      // `agent.stream(...)`'s `for await` has no bound of its own — a hung
      // LLM call or a wedged tool step would otherwise leave this process
      // waiting forever with nothing surfacing anywhere. Racing each `next()`
      // against a timeout (rather than wrapping the whole loop once) means a
      // long-but-progressing multi-step task is fine as long as *some* chunk
      // keeps arriving; only true inactivity trips this.
      let timer: ReturnType<typeof setTimeout> | undefined;
      const next = await Promise.race([
        iterator.next(),
        new Promise<"timeout">((resolve) => {
          timer = setTimeout(() => resolve("timeout"), INACTIVITY_TIMEOUT_MS);
        }),
      ]);
      clearTimeout(timer);

      if (next === "timeout") {
        throw new Error(`agent produced no output for ${INACTIVITY_TIMEOUT_MS / 1000}s`);
      }
      if (next.done) break;
      const chunk = next.value;

      const agentUpdate = chunk.agent;
      if (agentUpdate?.messages) {
        for (const message of agentUpdate.messages as AIMessage[]) {
          for (const call of message.tool_calls ?? []) {
            console.error(`[daimon-agent] tool call: ${call.name}`);
            emit({
              type: "step",
              id: call.id ?? randomUUID(),
              label: call.name,
              status: "running",
              tool: call.name,
            });
          }
          if (typeof message.content === "string" && message.content) {
            console.error("[daimon-agent] final message chunk received");
            finalResult = message.content;
          }
          // Pushed in encounter order, same order this loop already
          // processes updates in — a tool-calling AIMessage lands here
          // before its corresponding ToolMessage(s) below, which is exactly
          // the shape the *next* turn's `agent.stream` call needs to see a
          // coherent prior conversation.
          conversation.push(message);
        }
      }

      const toolsUpdate = chunk.tools;
      if (toolsUpdate?.messages) {
        for (const message of toolsUpdate.messages as ToolMessage[]) {
          console.error(`[daimon-agent] tool result: ${message.name ?? "tool"}`);
          emit({
            type: "step",
            id: message.tool_call_id ?? randomUUID(),
            label: message.name ?? "tool",
            status: "done",
            tool: message.name,
          });
          conversation.push(message);
        }
      }
    }

    emit({ type: "step", id: thinkingId, label: "Thinking", status: "done" });
    const result = finalResult || "Task complete.";
    console.error(`[daimon-agent] task done: ${result}`);
    emit({ type: "done", result });
    recordTask(instruction, result, "done");
  } catch (err) {
    const message = err instanceof GraphRecursionError
      ? "Daimon took too many steps without finishing (possible loop) — try breaking this " +
        "into a smaller instruction."
      : err instanceof Error ? err.message : String(err);
    console.error(`[daimon-agent] task error: ${message}`);
    // Roll back to before this turn's HumanMessage — see the comment above
    // `lengthBeforeThisTurn` for why an unanswered trailing HumanMessage
    // would corrupt every later turn in this session, not just this one.
    conversation.length = lengthBeforeThisTurn;
    emit({ type: "error", message });
    recordTask(instruction, message, "error");
  }
}
