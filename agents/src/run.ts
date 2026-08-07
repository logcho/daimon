import { randomUUID } from "node:crypto";
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { session } from "./agent.js";
import * as browser from "./browser.js";
import { runDemoTask } from "./demo.js";
import { recordTask } from "./memory.js";
import { setActiveEmit } from "./emitter.js";
import type { TaskEvent } from "./events.js";

// A companion to the daemon's own 90s stall timeout on the Rust side (see
// `src-tauri/src/session.rs`'s `STALL_TIMEOUT`) — deliberately shorter, so
// that on a genuine hang *this* timeout wins the race and can attribute the
// failure to "the agent produced nothing", with detail in the session's node
// log, rather than the daemon just seeing silence and guessing.
const INACTIVITY_TIMEOUT_MS = 60_000;

// How often a live screenshot of the background browser goes out while a turn
// is running — frequent enough to feel live for someone watching the in-app
// viewer, infrequent enough not to compete with the turn's real work.
const LIVE_FRAME_INTERVAL_MS = 1_500;

// Maps the harness's message stream onto Daimon's own `TaskEvent` NDJSON
// protocol, which the Rust daemon and the whole frontend already speak. Kept
// deliberately unchanged across the harness migration so the blast radius
// stopped at this file.
//
// Note the shapes: tool *calls* arrive as `tool_use` content blocks inside an
// assistant message, and tool *results* come back as `tool_result` blocks on
// a synthetic user message — there are no dedicated top-level SDK message
// types for either.
interface TurnState {
  text: string;
  // tool_use id -> the label already shown for it. The frontend's step
  // reducer *replaces* the whole step object on each event rather than
  // merging fields (see `applyToTurn` in src/lib/sessionEvents.ts), so the
  // completion event has to carry the same label the "running" event did —
  // otherwise every finished tool call renders as a blank line.
  toolLabels: Map<string, { label: string; tool: string }>;
}

function mapMessage(message: SDKMessage, emit: (event: TaskEvent) => void, state: TurnState): void {
  switch (message.type) {
    case "assistant": {
      for (const block of message.message.content) {
        if (block.type === "text") {
          // Each assistant turn may arrive as several messages; the last
          // non-empty text is what the user sees as the result.
          if (block.text.trim()) state.text = block.text;
        } else if (block.type === "tool_use") {
          // Strip the `mcp__daimon__` prefix — the chat shows tool names
          // directly and "mcp__daimon__open_url" is noise to a human.
          const label = block.name.replace(/^mcp__daimon__/, "");
          state.toolLabels.set(block.id, { label, tool: block.name });
          emit({ type: "step", id: block.id, label, status: "running", tool: block.name });
        }
      }
      break;
    }
    case "user": {
      const content = message.message.content;
      if (!Array.isArray(content)) break;
      for (const block of content) {
        if (typeof block === "object" && block !== null && block.type === "tool_result") {
          const known = state.toolLabels.get(block.tool_use_id);
          emit({
            type: "step",
            id: block.tool_use_id,
            label: known?.label ?? "tool",
            status: block.is_error ? "error" : "done",
            tool: known?.tool,
          });
        }
      }
      break;
    }
    default:
      // The union has ~35 members (hook lifecycle, task progress, rate
      // limits, plugin installs, ...). Everything Daimon's UI doesn't render
      // is deliberately dropped rather than forwarded as an unknown event.
      break;
  }
}

export async function runTurn(instruction: string, emit: (event: TaskEvent) => void): Promise<void> {
  // No credentials of any kind — fall back to the scripted demo so a fresh
  // install still does something visible before the user reaches Settings.
  if (!process.env.ANTHROPIC_API_KEY && process.env.DAIMON_AUTH_MODE !== "subscription") {
    console.error("[daimon-agent] no credentials configured, running demo task");
    await runDemoTask(instruction, emit);
    return;
  }

  await session.run(async (iterator, send) => {
    setActiveEmit(emit);

    const thinkingId = randomUUID();
    emit({ type: "step", id: thinkingId, label: "Thinking", status: "running" });

    // Periodic screenshots of the background browser for the in-app "watch it
    // work" viewer. Best-effort: a failed capture (mid-navigation, or no page
    // open yet) skips that tick silently rather than failing the turn — a
    // dropped frame is invisible to someone watching a live feed. Cleared in
    // the `finally` regardless of how the turn ends.
    const liveFrameTimer = setInterval(() => {
      browser
        .screenshotBase64()
        .then((data) => emit({ type: "live_frame", data }))
        .catch(() => {});
    }, LIVE_FRAME_INTERVAL_MS);

    const state: TurnState = { text: "", toolLabels: new Map() };
    try {
      // Must happen before the model can call any browser tool this turn —
      // see `resetRecordingForNewTurn`'s doc comment in browser.ts. Inside
      // the try, and tolerant of failure: its comment claims it never throws,
      // but it reaches PinchTab over HTTP and so raises `fetch failed`
      // whenever the browser sidecar isn't up. Outside the try that took down
      // the entire turn before the model was even asked anything — which is
      // wrong even in production (a turn that needs no browser at all
      // shouldn't fail because the browser is unhealthy), and made every turn
      // after the first fail outright when running the server standalone.
      await browser.resetRecordingForNewTurn().catch((err) => {
        console.error(`[daimon-agent] could not reset browser recording (continuing): ${err}`);
      });

      send(instruction);

      for (;;) {
        // The harness's iterator has no bound of its own — a hung model call
        // or wedged tool would otherwise leave this process waiting forever
        // with nothing surfacing anywhere. Racing each `next()` rather than
        // wrapping the whole loop means a long-but-progressing task is fine
        // as long as *something* keeps arriving; only true inactivity trips.
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
        // The session's stream ending mid-turn means the harness exited
        // without a result — a crash or an interrupt, not a completed turn.
        if (next.done) throw new Error("agent stream ended before the turn completed");

        const message = next.value;
        mapMessage(message, emit, state);

        // `result` terminates the turn — this is the boundary that lets one
        // long-lived query serve many separate NDJSON responses.
        if (message.type === "result") {
          if (message.subtype !== "success" || message.is_error) {
            const detail = "result" in message && message.result ? message.result : message.subtype;
            throw new Error(`agent turn failed: ${detail}`);
          }
          state.text = message.result || state.text;
          break;
        }
      }

      emit({ type: "step", id: thinkingId, label: "Thinking", status: "done" });
      // `.trim()` rather than a truthiness check — a whitespace-only result
      // is truthy in JS and would render as a completely blank turn, which is
      // indistinguishable from the agent never responding at all.
      const result = state.text.trim() || "Task complete.";
      console.error(`[daimon-agent] task done: ${result}`);
      emit({ type: "done", result });
      recordTask(instruction, result, "done");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`[daimon-agent] task error: ${message}`);
      emit({ type: "error", message });
      recordTask(instruction, message, "error");
    } finally {
      clearInterval(liveFrameTimer);
      setActiveEmit(null);
    }
  });
}
