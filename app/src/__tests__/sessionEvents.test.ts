import { describe, expect, it } from "vitest";
import { applyEvent, applyTodoEvent, isSessionBusy, sessionTotals } from "../sessionEvents";
import type { AgentEvent, ChatMessage } from "../types";

/**
 * The fold from the agent's event stream to what a UI renders.
 *
 * Worth testing on its own because two clients depend on it — the desktop app
 * and the phone — and because it is pure, so the interesting cases are cheap
 * to state and impossible to reproduce by clicking.
 */

const ev = (e: Record<string, unknown>) => e as unknown as AgentEvent;
const fold = (events: Record<string, unknown>[]) =>
  events.map(ev).reduce(applyEvent, [] as ChatMessage[]);

describe("applyEvent", () => {
  it("opens a new exchange for each prompt", () => {
    const messages = fold([
      { type: "user", text: "first question" },
      { type: "assistant_delta", text: "first answer" },
      { type: "done", result: "first answer" },
      { type: "user", text: "second question" },
      { type: "assistant_delta", text: "second answer" },
      { type: "done", result: "second answer" },
    ]);

    expect(messages.map((m) => m.role)).toEqual(["user", "assistant", "user", "assistant"]);
    expect(messages.map((m) => m.content)).toEqual([
      "first question",
      "first answer",
      "second question",
      "second answer",
    ]);
  });

  it("does not let a later reply overwrite an earlier one", () => {
    // The bug this guards: with no `user` case the prompt was dropped and the
    // deltas that followed folded into the *previous* turn's bubble, so a
    // message sent from another device appeared to rewrite an old answer.
    const messages = fold([
      { type: "user", text: "from the desktop" },
      { type: "done", result: "answered the desktop" },
      { type: "user", text: "from the phone" },
      { type: "assistant_delta", text: "answering the phone" },
    ]);

    expect(messages).toHaveLength(4);
    expect(messages[1].content).toBe("answered the desktop");
    expect(messages[3].content).toBe("answering the phone");
  });

  it("streams deltas into the open reply", () => {
    const messages = fold([
      { type: "user", text: "hello" },
      { type: "assistant_delta", text: "par" },
      { type: "assistant_delta", text: "tial" },
    ]);
    expect(messages[1].content).toBe("partial");
  });

  it("keeps the model's reasoning out of the answer", () => {
    const messages = fold([
      { type: "user", text: "hello" },
      { type: "assistant_delta", text: "hmm", channel: "reasoning" },
      { type: "assistant_delta", text: "the answer" },
    ]);
    expect(messages[1].content).toBe("the answer");
  });

  it("keeps a sub-agent's narration out of the transcript", () => {
    const messages = fold([
      { type: "user", text: "hello" },
      { type: "assistant_delta", text: "sub-agent chatter", agent_id: "a1" },
      { type: "assistant_delta", text: "the answer" },
    ]);
    expect(messages[1].content).toBe("the answer");
  });

  it("lets done be authoritative over the deltas", () => {
    const messages = fold([
      { type: "user", text: "hello" },
      { type: "assistant_delta", text: "par" },
      { type: "done", result: "the whole answer" },
    ]);
    expect(messages[1].content).toBe("the whole answer");
    expect(messages[1].thinking).toBe(false);
  });

  it("ignores a straggling error once the result is in", () => {
    const messages = fold([
      { type: "user", text: "hello" },
      { type: "done", result: "finished" },
      { type: "error", message: "a late transport hiccup" },
    ]);
    expect(messages[1].error).toBeUndefined();
    expect(messages[1].content).toBe("finished");
  });

  it("tracks whether a turn is in flight", () => {
    const running = fold([
      { type: "user", text: "hello" },
      { type: "step", id: "t", label: "Thinking", status: "running" },
    ]);
    expect(isSessionBusy(running)).toBe(true);
    expect(isSessionBusy(applyEvent(running, ev({ type: "done", result: "x" })))).toBe(false);
  });

  it("merges a step's detail with its later timing", () => {
    const messages = fold([
      { type: "user", text: "hello" },
      { type: "step", id: "s1", label: "read_file", status: "running", detail: "App.tsx" },
      { type: "step", id: "s1", label: "read_file", status: "done", elapsed_ms: 12 },
    ]);
    const [step] = messages[1].steps;
    expect(step).toMatchObject({ status: "done", detail: "App.tsx", elapsed_ms: 12 });
  });

  it("ignores events that belong to other surfaces", () => {
    const before = fold([{ type: "user", text: "hello" }]);
    const after = applyEvent(before, ev({ type: "ui_action", action: "open_terminal_with_command" }));
    expect(after).toBe(before);
  });
});

describe("applyTodoEvent", () => {
  it("replaces the list wholesale, so a missed event still converges", () => {
    const first = applyTodoEvent([], ev({ type: "todo", items: [{ text: "a", status: "pending" }] }));
    const second = applyTodoEvent(first, ev({ type: "todo", items: [{ text: "a", status: "done" }] }));
    expect(second).toEqual([{ text: "a", status: "done" }]);
  });
});

describe("sessionTotals", () => {
  it("sums usage across turns and keeps the newest context size", () => {
    const messages = fold([
      { type: "user", text: "one" },
      { type: "usage", model: "m", input_tokens: 100, output_tokens: 10, cache_read_tokens: 0, cost_usd: 0.01, role: "pro" },
      { type: "done", result: "a" },
      { type: "user", text: "two" },
      { type: "usage", model: "m", input_tokens: 300, output_tokens: 20, cache_read_tokens: 0, cost_usd: 0.02, role: "pro" },
      { type: "done", result: "b" },
    ]);

    const totals = sessionTotals(messages);
    expect(totals.turns).toBe(2);
    expect(totals.usage.inputTokens).toBe(400);
    expect(totals.usage.costUsd).toBeCloseTo(0.03);
    // Context is the current prompt, not the sum of every prompt.
    expect(totals.usage.contextTokens).toBe(300);
  });

  it("reports an unknown cost rather than a total that looks complete", () => {
    const messages = fold([
      { type: "user", text: "one" },
      { type: "usage", model: "m", input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, role: "pro" },
      { type: "done", result: "a" },
    ]);
    expect(sessionTotals(messages).usage.costUsd).toBeNull();
  });
});
