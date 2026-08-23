/**
 * The app's single connection to its agent server's event bus.
 *
 * One socket for the whole app, not one per terminal tab: the protocol
 * multiplexes by id already, and a socket per tab would mean N reconnect
 * loops racing each other after a server restart.
 *
 * This is the only file that knows the desktop app reaches the bus over
 * loopback. `busClient.ts` is deliberately transport-free so the phone can
 * reuse it against the remote gateway instead.
 */

import { agentStatus } from "../api";
import { BusClient, type BusHandlers } from "./busClient";

let client: BusClient | null = null;
const handlers: BusHandlers = {};
const termData = new Map<string, (bytes: Uint8Array) => void>();
const sessionEventListeners = new Set<(session: string, seq: number, event: unknown) => void>();
const controlListeners = new Set<(frame: Record<string, unknown>) => void>();
const reconnectListeners = new Set<() => void>();
const statusListeners = new Set<(connected: boolean) => void>();
let connected = false;

handlers.onTermData = (id, bytes) => termData.get(id)?.(bytes);
handlers.onEvent = (session, seq, event) =>
  sessionEventListeners.forEach((fn) => fn(session, seq, event));
handlers.onControl = (frame) => controlListeners.forEach((fn) => fn(frame));
handlers.onReconnect = () => reconnectListeners.forEach((fn) => fn());
handlers.onStatus = (up) => {
  connected = up;
  statusListeners.forEach((fn) => fn(up));
};

/** `agentStatus` also ensures the server is up, so this doubles as "wait for
 *  the agent to be ready" — the same guarantee `send_message` relies on. */
async function busUrl(): Promise<string> {
  const status = await agentStatus();
  return `ws://127.0.0.1:${status.port}/bus/ws`;
}

export function bus(): BusClient {
  if (!client) client = new BusClient(busUrl, handlers);
  return client;
}

/** Route one terminal's output. Returns an unsubscribe. */
export function onTermData(id: string, handler: (bytes: Uint8Array) => void): () => void {
  termData.set(id, handler);
  return () => {
    if (termData.get(id) === handler) termData.delete(id);
  };
}

/** Every agent event, for every session this client is attached to.
 *
 *  Chat used to arrive down the NDJSON body of the request that started it, so
 *  this window saw only the turns it had started itself. On the bus a turn
 *  belongs to the session, which is what lets a message sent from a phone
 *  stream in here. */
export function onSessionEvent(
  handler: (session: string, seq: number, event: unknown) => void,
): () => void {
  sessionEventListeners.add(handler);
  return () => sessionEventListeners.delete(handler);
}

export function onBusControl(handler: (frame: Record<string, unknown>) => void): () => void {
  controlListeners.add(handler);
  return () => controlListeners.delete(handler);
}

/** The socket came back. Whatever was attached before is not attached now —
 *  re-attach and take a fresh snapshot rather than assuming continuity. */
export function onBusReconnect(handler: () => void): () => void {
  reconnectListeners.add(handler);
  return () => reconnectListeners.delete(handler);
}

/** Whether the socket is up right now.
 *
 *  The phone has always tracked this (`mobile/Remote.tsx`'s `connected`); the
 *  desktop threw it away, which is part of why a turn whose stream died went
 *  on claiming to be in flight. Returns an unsubscribe, and reports the
 *  current state immediately so a late subscriber isn't left guessing.
 */
export function onBusStatus(handler: (connected: boolean) => void): () => void {
  statusListeners.add(handler);
  handler(connected);
  return () => statusListeners.delete(handler);
}
