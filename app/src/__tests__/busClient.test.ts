import { describe, expect, it } from "vitest";
import { BusClient, type ControlFrame } from "../lib/busClient";

/**
 * The resumption cursor.
 *
 * The server has always implemented its half — a monotonic per-session `seq`,
 * a `since` parameter on attach, a `truncated` flag, and a `resync` frame when
 * it has had to drop events to keep up. The client implemented none of it, so
 * every reconnect rebuilt the transcript from the last 800 events and a lagging
 * client silently rendered one with a hole in it.
 *
 * Exercised through `receive` directly: the interesting behaviour is the
 * bookkeeping, and standing up a socket to reach it would test aiohttp.
 */
function client() {
  const events: [string, number, unknown][] = [];
  const controls: ControlFrame[] = [];
  const bus = new BusClient(async () => "ws://unused", {
    onEvent: (session, seq, event) => events.push([session, seq, event]),
    onControl: (frame) => controls.push(frame),
  });
  // `receive` is the frame decoder; nothing about it needs a live socket.
  const feed = (frame: Record<string, unknown>) =>
    (bus as unknown as { receive(raw: string): void }).receive(JSON.stringify(frame));
  return { bus, feed, events, controls };
}

const event = (session: string, seq: number) => ({
  v: 1, kind: "event", session, seq, event: { type: "assistant_delta", text: `#${seq}` },
});

/** Send an `attach` with a stubbed socket and return the op that went out. */
async function attachOp(bus: BusClient, session: string): Promise<Record<string, unknown>> {
  const sent: string[] = [];
  const stub = bus as unknown as { ws: unknown; opening: Promise<void> };
  // Both halves of "already connected": `send` awaits `connect()` first, and
  // `connect()` short-circuits on a pending `opening`.
  stub.opening = Promise.resolve();
  stub.ws = { readyState: 1, send: (raw: string) => sent.push(raw) };
  void bus.send("attach", { session });
  await new Promise((resolve) => setTimeout(resolve, 0));
  return JSON.parse(sent[0]);
}

const snapshot = (session: string, seq: number, extra: Record<string, unknown> = {}) => ({
  v: 1, kind: "control", control: "snapshot", session, seq, events: [], ...extra,
});

describe("the resumption cursor", () => {
  it("drops events it has already seen", () => {
    // The server subscribes *before* it sends a snapshot, so an event
    // published in between arrives twice — and says so, by carrying a `seq`
    // the client already has. Nothing used to look.
    const { feed, events } = client();
    feed(event("s", 1));
    feed(event("s", 2));
    feed(event("s", 2));
    feed(event("s", 1));
    expect(events.map(([, seq]) => seq)).toEqual([1, 2]);
  });

  it("keeps a cursor per session", () => {
    const { feed, events } = client();
    feed(event("a", 5));
    feed(event("b", 1)); // behind a's cursor, but a different conversation
    expect(events.map(([session, seq]) => `${session}${seq}`)).toEqual(["a5", "b1"]);
  });

  it("asks for only what it missed on the next attach", async () => {
    const { bus, feed } = client();
    feed(event("s", 7));
    expect((await attachOp(bus, "s")).since).toBe(7);
  });

  it("does not ask to resume a session it has never seen", async () => {
    const { bus } = client();
    expect((await attachOp(bus, "fresh")).since).toBeUndefined();
  });
});

describe("when a snapshot can be appended to", () => {
  it("marks the first one as a rebuild", () => {
    const { feed, controls } = client();
    feed(snapshot("s", 12));
    expect(controls[0].reset).toBe(true);
  });

  it("marks a continuation as an append", () => {
    const { feed, controls } = client();
    feed(event("s", 3));
    feed(snapshot("s", 9));
    expect(controls[0].reset).toBe(false);
  });

  it("marks a truncated replay as a rebuild", () => {
    // The ring no longer reaches our cursor, so what it sent does not join up
    // with what we hold.
    const { feed, controls } = client();
    feed(event("s", 3));
    feed(snapshot("s", 900, { truncated: true }));
    expect(controls[0].reset).toBe(true);
  });

  it("marks a restarted session as a rebuild", () => {
    // The agent server respawned, so this is a fresh channel counting from
    // one. Appending would splice a new conversation onto an old one, and
    // resuming from the dead channel's number would show nothing at all.
    const { feed, controls } = client();
    feed(event("s", 400));
    feed(snapshot("s", 2));
    expect(controls[0].reset).toBe(true);
  });
});

describe("resync", () => {
  it("forgets the cursor so the next attach starts clean", async () => {
    // We fell far enough behind that the server dropped events — "a phone on a
    // train". The cursor now points into a stream with a hole in it.
    const { bus, feed, controls } = client();
    feed(event("s", 40));
    feed({ v: 1, kind: "control", control: "resync", session: "s" });
    expect(controls[0].control).toBe("resync"); // still delivered to the app
    expect((await attachOp(bus, "s")).since).toBeUndefined();
  });
});

describe("forget", () => {
  it("stops a closed conversation resuming from a cursor it no longer holds", async () => {
    const { bus, feed } = client();
    feed(event("s", 11));
    bus.forget("s");
    expect((await attachOp(bus, "s")).since).toBeUndefined();
  });
});
