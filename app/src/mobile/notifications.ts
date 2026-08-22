/**
 * Turning on "tell me when the agent is waiting".
 *
 * Worth knowing about iOS: Web Push only works for a page added to the home
 * screen, and only after an explicit gesture. A prompt on load would be
 * refused and then never askable again, so this is only ever called from a
 * button the user pressed.
 */

const KEY = "daimon-push-enabled";

export type PushState = "unsupported" | "needs-home-screen" | "off" | "on" | "denied";

function supported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/** iOS refuses push for a tab; the page has to have been added to the home
 *  screen. Nothing else distinguishes the two at runtime. */
function standalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    (window.navigator as { standalone?: boolean }).standalone === true
  );
}

const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent);

export function pushState(): PushState {
  if (!supported()) return "unsupported";
  if (isIOS() && !standalone()) return "needs-home-screen";
  if (Notification.permission === "denied") return "denied";
  try {
    return window.localStorage.getItem(KEY) === "1" ? "on" : "off";
  } catch {
    return "off";
  }
}

function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const raw = atob(padded);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

async function authed(path: string, init: RequestInit = {}): Promise<Response> {
  let token: string | null = null;
  try {
    token = window.localStorage.getItem("daimon-remote-token");
  } catch {
    /* nothing stored */
  }
  return fetch(path, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init.headers || {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
}

export async function enablePush(): Promise<{ ok: boolean; error?: string }> {
  if (!supported()) return { ok: false, error: "this browser can't do notifications" };

  const permission = await Notification.requestPermission();
  if (permission !== "granted") return { ok: false, error: "notifications were declined" };

  const keyResp = await authed("/push/key");
  if (!keyResp.ok) {
    const body = await keyResp.json().catch(() => ({}));
    return { ok: false, error: body.error ?? "the server can't send notifications" };
  }
  const { key } = await keyResp.json();

  const registration = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.subscribe({
    // Required by every browser: a push nobody can read is not allowed to be
    // silent, so it must carry a payload.
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(key),
  });

  const resp = await authed("/push/subscribe", {
    method: "POST",
    body: JSON.stringify(subscription.toJSON()),
  });
  if (!resp.ok) return { ok: false, error: "the server refused the subscription" };

  try {
    window.localStorage.setItem(KEY, "1");
  } catch {
    /* it still works this session */
  }
  return { ok: true };
}

export async function disablePush(): Promise<void> {
  try {
    const registration = await navigator.serviceWorker.getRegistration();
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription) {
      await authed("/push/unsubscribe", {
        method: "POST",
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      });
      await subscription.unsubscribe();
    }
  } catch {
    // Best effort: the local flag going off is what the user sees, and the
    // server drops a dead endpoint on its next send anyway.
  }
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
}
