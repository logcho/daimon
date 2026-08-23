import { describe, expect, it } from "vitest";
import { applyEvent, applyTodoEvent, isSessionBusy, reconcileBusy, sessionTotals } from "../sessionEvents";
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


describe("questions", () => {
  it("keeps an ask out of the transcript", () => {
    // A question is session state — something the session is *waiting on* —
    // not a line of history. Both clients render it above the composer.
    const before = fold([{ type: "user", text: "do the thing" }]);
    const after = applyEvent(before, ev({ type: "ask", id: "a1", kind: "plan", question: "ok?", options: [] }));
    expect(after).toBe(before);
  });

  it("keeps ask_resolved out of the transcript too", () => {
    const before = fold([{ type: "user", text: "do the thing" }]);
    const after = applyEvent(before, ev({ type: "ask_resolved", id: "a1", answer: "yes" }));
    expect(after).toBe(before);
  });

  it("lets the answered turn carry on in the same exchange", () => {
    // Resuming continues the turn the question interrupted; it must not open
    // a second one.
    const messages = fold([
      { type: "user", text: "do the thing" },
      { type: "ask", id: "a1", kind: "plan", question: "ok?", options: [] },
      { type: "ask_resolved", id: "a1", answer: "Go ahead" },
      { type: "assistant_delta", text: "doing it" },
      { type: "done", result: "done it" },
    ]);
    expect(messages).toHaveLength(2);
    expect(messages[1].content).toBe("done it");
  });
});

describe("retry notices", () => {
  it("replaces a trailing retry rather than stacking under it", () => {
    // A flaky provider produces a run of these. Five identical lines say
    // nothing the newest one doesn't, while pushing the transcript off screen.
    const messages = fold([
      { type: "user", text: "go" },
      { type: "retry", attempt: 1, max_attempts: 5, reason: "eof" },
      { type: "retry", attempt: 2, max_attempts: 5, reason: "eof" },
      { type: "retry", attempt: 3, max_attempts: 5, reason: "eof" },
    ]);
    const notices = messages[1].notices ?? [];
    expect(notices).toHaveLength(1);
    expect(notices[0].text).toContain("(3/5)");
  });

  it("keeps a retry that happened before real progress", () => {
    // Only a *trailing* retry is replaced: one that preceded actual work is
    // part of the record of what happened.
    const messages = fold([
      { type: "user", text: "go" },
      { type: "retry", attempt: 1, max_attempts: 5, reason: "eof" },
      { type: "compaction", before_tokens: 100, after_tokens: 40, dropped: 3 },
      { type: "retry", attempt: 1, max_attempts: 5, reason: "eof" },
    ]);
    const notices = messages[1].notices ?? [];
    expect(notices.map((n) => n.kind)).toEqual(["retry", "compaction", "retry"]);
  });
});

describe("reconcileBusy", () => {
  it("closes out a turn the server says is no longer running", () => {
    // `thinking` is only ever cleared by done/error, and neither is guaranteed
    // to arrive — the socket can die mid-turn. The server counts its own
    // active turns, so it is the authority on this.
    const open = fold([{ type: "user", text: "go" }, { type: "step", id: "t", label: "Thinking", status: "running" }]);
    expect(isSessionBusy(open)).toBe(true);

    const settled = reconcileBusy(open, false);
    expect(isSessionBusy(settled)).toBe(false);
    expect(settled[1].error).toBeTruthy();
  });

  it("leaves a genuinely running turn alone", () => {
    const open = fold([{ type: "user", text: "go" }, { type: "step", id: "t", label: "Thinking", status: "running" }]);
    expect(reconcileBusy(open, true)).toBe(open);
  });

  it("does not re-open a turn that already finished", () => {
    // Deliberately one-way. The events are what say where text goes; a server
    // reporting busy must never reanimate a settled bubble.
    const done = fold([
      { type: "user", text: "go" },
      { type: "step", id: "t", label: "Thinking", status: "running" },
      { type: "done", result: "finished" },
    ]);
    expect(reconcileBusy(done, true)).toBe(done);
    expect(reconcileBusy(done, false)).toBe(done);
  });

  it("keeps the text of a turn that produced one before dying", () => {
    const partial = fold([
      { type: "user", text: "go" },
      { type: "step", id: "t", label: "Thinking", status: "running" },
      { type: "assistant_delta", text: "here is what I found" },
    ]);
    const settled = reconcileBusy(partial, false);
    expect(settled[1].content).toBe("here is what I found");
    expect(settled[1].error).toBeUndefined();
  });
});
