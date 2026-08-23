/**
 * Client for the agent server's `/bus/ws`.
 *
 * Deliberately transport-free and Tauri-free: the desktop app reaches the
 * server over loopback, the phone reaches it through the remote gateway, and
 * both speak exactly this protocol. Anything that knows about `invoke` or
 * `listen` belongs in api.ts, not here.
 *
 * Two things it owns beyond framing:
 *
 * - **Request/response over a stream.** Every op carries an `op_id` the server
 *   echoes in its ack, so callers can `await` an op the way they awaited a
 *   Tauri command. Without it, "did my keystroke land?" is unanswerable after
 *   a reconnect.
 * - **Reconnect.** A phone's socket dies whenever the screen locks. Callers
 *   subscribe once and stay subscribed; this reopens underneath them with
 *   backoff and replays the subscriptions, and tells them so via `onReconnect`
 *   so they can re-snapshot rather than splice onto a stream with a hole.
 */

export interface AgentEventFrame {
  v: number;
  kind: "event";
  session: string;
  seq: number;
  event: unknown;
}

export interface TermFrame {
  v: number;
  kind: "term";
  id: string;
  /** Base64 raw pty bytes — arbitrary binary, not text. */
  data: string;
}

export interface ControlFrame {
  v: number;
  kind: "control";
  control: string;
  [key: string]: unknown;
}

type Frame = AgentEventFrame | TermFrame | ControlFrame | { kind: "ack"; op_id?: string; ok: boolean; [k: string]: unknown };

export interface BusHandlers {
  onEvent?: (session: string, seq: number, event: unknown) => void;
  onTermData?: (id: string, bytes: Uint8Array) => void;
  onControl?: (frame: ControlFrame) => void;
  /** The socket came back. Anything cached from before it went away is now
   *  suspect: re-attach and take a fresh snapshot. */
  onReconnect?: () => void;
  onStatus?: (connected: boolean) => void;
}

export interface AckResult {
  ok: boolean;
  error?: string;
  [key: string]: unknown;
}

const RECONNECT_BASE_MS = 250;
const RECONNECT_MAX_MS = 10_000;
/** An op that never gets an ack must not leave a promise pending forever —
 *  the socket can die between send and reply. */
const OP_TIMEOUT_MS = 15_000;

export function decodeBase64(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function encodeBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

export class BusClient {
  private ws: WebSocket | null = null;
  private pending = new Map<string, { resolve: (r: AckResult) => void; timer: ReturnType<typeof setTimeout> }>();
  private nextOpId = 1;
  private attempt = 0;
  private closed = false;
  private opening: Promise<void> | null = null;

  /**
   * `resolveUrl` is called on every connect rather than once, because the
   * address can move underneath us: the agent server takes a freshly allocated
   * port each time it spawns, so a client that cached the URL would reconnect
   * forever to a port nothing is listening on.
   */
  constructor(
    private resolveUrl: () => Promise<string>,
    private handlers: BusHandlers = {},
  ) {}

  connect(): Promise<void> {
    if (this.opening) return this.opening;
    this.closed = false;
    this.opening = (async () => {
      const url = await this.resolveUrl();
      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(url);
        this.ws = ws;

        ws.onopen = () => {
          const reconnected = this.attempt > 0;
          this.attempt = 0;
          this.handlers.onStatus?.(true);
          resolve();
          // After resolve, so a caller's first op isn't racing the replay.
          if (reconnected) this.handlers.onReconnect?.();
        };
        ws.onmessage = (msg) => this.receive(msg.data);
        ws.onerror = () => {
          if (this.ws === ws && this.attempt === 0) reject(new Error("bus connection failed"));
        };
        ws.onclose = () => {
          if (this.ws !== ws) return;
          this.ws = null;
          this.opening = null;
          this.handlers.onStatus?.(false);
          // Nothing is coming back for these; failing them beats hanging.
          for (const [, entry] of this.pending) {
            clearTimeout(entry.timer);
            entry.resolve({ ok: false, error: "disconnected" });
          }
          this.pending.clear();
          if (!this.closed) this.scheduleReconnect();
        };
      });
    })();
    this.opening.catch(() => {
      this.opening = null;
      if (!this.closed) this.scheduleReconnect();
    });
    return this.opening;
  }

  private scheduleReconnect(): void {
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.attempt, RECONNECT_MAX_MS);
    this.attempt += 1;
    setTimeout(() => {
      if (!this.closed) void this.connect().catch(() => {});
    }, delay);
  }

  private receive(raw: unknown): void {
    if (typeof raw !== "string") return;
    let frame: Frame;
    try {
      frame = JSON.parse(raw);
    } catch {
      return; // a frame we cannot parse is not worth dropping the socket over
    }
    if (frame.kind === "ack") {
      const id = frame.op_id;
      const entry = id ? this.pending.get(id) : undefined;
      if (entry && id) {
        this.pending.delete(id);
        clearTimeout(entry.timer);
        entry.resolve(frame as AckResult);
      }
      return;
    }
    if (frame.kind === "event") {
      this.handlers.onEvent?.(frame.session, frame.seq, frame.event);
    } else if (frame.kind === "term") {
      this.handlers.onTermData?.(frame.id, decodeBase64(frame.data));
    } else if (frame.kind === "control") {
      this.handlers.onControl?.(frame);
    }
  }

  /** Send an op and wait for its ack. Never rejects — a transport failure
   *  comes back as `{ok:false}`, because every caller has to handle that
   *  anyway and a mix of both is worse. */
  async send(op: string, fields: Record<string, unknown> = {}): Promise<AckResult> {
    await this.connect().catch(() => {});
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return { ok: false, error: "not connected" };

    const opId = `op-${this.nextOpId++}`;
    return new Promise<AckResult>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(opId);
        resolve({ ok: false, error: "timed out" });
      }, OP_TIMEOUT_MS);
      this.pending.set(opId, { resolve, timer });
      try {
        ws.send(JSON.stringify({ op, op_id: opId, ...fields }));
      } catch (err) {
        this.pending.delete(opId);
        clearTimeout(timer);
        resolve({ ok: false, error: String(err) });
      }
    });
  }

  /** Fire and forget, for input where the round trip would add latency to
   *  every keystroke. */
  post(op: string, fields: Record<string, unknown> = {}): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify({ op, ...fields }));
    } catch {
      // The close handler will reconnect; a dropped keystroke is recoverable.
    }
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
    this.ws = null;
    this.opening = null;
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
