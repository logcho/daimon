/**
 * Talking to the remote gateway from a phone.
 *
 * Wraps the same `BusClient` the desktop uses — the protocol is identical, and
 * that is the whole design — with the two things only a remote client needs:
 * a durable token to hold, and a fresh single-use ticket for every socket,
 * because a browser cannot put an Authorization header on a WS handshake.
 *
 * Ops carry a `workspace` here; the desktop's do not, because the desktop
 * talks to one server directly and the gateway fronts several.
 */

import { BusClient, type BusHandlers } from "../lib/busClient";

const TOKEN_KEY = "daimon-remote-token";

/** Same origin as the page: the gateway serves this client itself, which is
 *  also why there is no CORS anywhere in the gateway. */
const base = () => window.location.origin;

export function storedToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // private browsing, or site data blocked
  }
}

export function storeToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Not fatal: the session keeps working, it just won't survive a reload.
  }
}

export function forgetToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing to do */
  }
}

export interface WorkspaceInfo {
  key: string;
  name: string;
  path: string;
}

export interface SessionInfo {
  session_id: string;
  title: string | null;
  origin?: string | null;
  last_result?: string | null;
  last_active_at?: number | null;
  turns?: number;
  busy: boolean;
  pending_ask: boolean;
}

export interface TerminalInfo {
  id: string;
  cwd: string;
  cols: number;
  rows: number;
  pid: number | null;
  exited: boolean;
  clients: number;
}

async function authed(path: string, init: RequestInit = {}): Promise<Response> {
  const token = storedToken();
  return fetch(`${base()}${path}`, {
    ...init,
    headers: {
      ...(init.headers || {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
}

/** Exchange a pairing code for a durable token. The code is single-use and the
 *  token comes back exactly once — the host only ever stores its hash. */
export async function pair(code: string, label: string): Promise<{ ok: boolean; error?: string }> {
  const resp = await fetch(`${base()}/pair`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ code, label }),
  });
  if (!resp.ok) return { ok: false, error: "that code didn't work" };
  const body = await resp.json();
  storeToken(body.token);
  return { ok: true };
}

export async function fetchWorkspaces(): Promise<WorkspaceInfo[]> {
  const resp = await authed("/workspaces");
  if (!resp.ok) throw new Error(resp.status === 401 ? "unauthorized" : "could not list workspaces");
  return (await resp.json()).workspaces;
}

/** A ticket is minted per connect and dies in seconds, so it is safe in the
 *  query string in a way the durable token would not be. */
async function ticketUrl(): Promise<string> {
  const resp = await authed("/ticket", { method: "POST" });
  if (!resp.ok) throw new Error("unauthorized");
  const { ticket } = await resp.json();
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`;
}

export function connect(handlers: BusHandlers): BusClient {
  return new BusClient(ticketUrl, handlers);
}
