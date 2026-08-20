// Real search-and-play against Spotify's own Web API — deliberately not
// AppleScript (which can only do transport control on whatever's already
// loaded, not search by name — see `music_control`'s own doc comment in
// tools.ts) and deliberately not the web player (which wouldn't play
// through the user's real desktop app, output device, etc.). Spotify
// Connect's device-targeted playback is what makes this land on the actual
// desktop app: `PUT /v1/me/player/play` with a `device_id` starts playback
// *on that device*, not in this process or a browser tab.
//
// `SPOTIFY_ACCESS_TOKEN` is short-lived by design — refreshed fresh at
// every session spawn (see `src-tauri/src/workspace.rs`'s
// `spawn_node_agent`) rather than this process ever holding a long-lived
// refresh token itself. Absent entirely (not empty) if no account is
// connected.
const ACCESS_TOKEN = process.env.SPOTIFY_ACCESS_TOKEN;

const API_BASE = "https://api.spotify.com/v1";

async function spotifyFetch<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  if (!ACCESS_TOKEN) {
    throw new Error("No Spotify account connected — connect one in Daimon's Settings first.");
  }
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${ACCESS_TOKEN}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15_000),
  });

  if (res.status === 204) return undefined as T;

  const raw = await res.text();
  let data: unknown = {};
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = raw;
    }
  }

  if (!res.ok) {
    const message =
      typeof data === "object" && data !== null && "error" in data
        ? JSON.stringify((data as { error: unknown }).error)
        : raw;
    throw new Error(`Spotify API ${method} ${path} failed (${res.status}): ${message}`);
  }
  return data as T;
}

interface DevicesResponse {
  devices: Array<{ id: string; name: string; type: string; is_active: boolean }>;
}

interface SearchResponse {
  tracks?: { items: Array<{ uri: string; name: string; artists: Array<{ name: string }> }> };
}

/**
 * Searches for `query` and starts it playing on the user's real desktop
 * Spotify app (via Spotify Connect device targeting), returning a
 * human-readable description of what started playing. Throws a clear error
 * if no account is connected, or if Spotify isn't open anywhere (no device
 * found) — the caller (tools.ts) surfaces that as guidance to open the app
 * first.
 */
export async function playMusicByName(query: string): Promise<string> {
  const devices = await spotifyFetch<DevicesResponse>("GET", "/me/player/devices");
  // Prefer an already-active device, otherwise just take whatever's
  // available — Spotify will make it active once we target it.
  const device = devices.devices.find((d) => d.is_active) ?? devices.devices[0];
  if (!device) {
    throw new Error(
      "No Spotify device found — the desktop app doesn't seem to be open. Open it first with open_application.",
    );
  }

  const search = await spotifyFetch<SearchResponse>("GET", `/search?type=track&limit=1&q=${encodeURIComponent(query)}`);
  const track = search.tracks?.items[0];
  if (!track) {
    throw new Error(`No track found matching "${query}".`);
  }

  await spotifyFetch("PUT", `/me/player/play?device_id=${encodeURIComponent(device.id)}`, {
    uris: [track.uri],
  });

  const artists = track.artists.map((a) => a.name).join(", ");
  return `${track.name} by ${artists} — now playing on ${device.name}`;
}
