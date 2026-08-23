import { useCallback, useEffect, useRef, useState } from "react";
import {
  pairRemote,
  remoteDevices,
  remoteStatus,
  revokeRemoteDevice,
  startRemote,
  stopRemote,
  type PairedDevice,
  type RemoteStatus,
} from "../api";

/**
 * Turning this machine on for other devices.
 *
 * Off by default, and stated plainly rather than buried: it is the one switch
 * here that makes anything reachable from outside this computer.
 */
export function RemotePanel() {
  const [status, setStatus] = useState<RemoteStatus | null>(null);
  const [terminals, setTerminals] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  // A poll that lands mid start/stop reports the world as it was a moment ago;
  // letting it win would undo the switch the user just flipped.
  const busy = useRef(false);

  const refresh = useCallback(async () => {
    if (busy.current) return;
    try {
      const next = await remoteStatus();
      if (busy.current) return;
      setStatus(next);
      // The gateway stopping on its own is news, and this poll is the only
      // thing that will ever notice it. Only ever *sets*: a port clash the
      // user just triggered has to stay on screen, not be cleared three
      // seconds later by a poll that has nothing to report.
      if (next.error) setError(next.error);
      // Only a running gateway knows whether shells are on. While it is off the
      // backend always says false, and adopting that would clear the checkbox
      // out from under someone who ticked it before switching remote on.
      if (next.running) setTerminals(next.terminals);
      setDevices(await remoteDevices());
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    // The pairing code appears a moment after the process starts, and expires
    // on its own — poll while the panel is open rather than showing a stale one.
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function toggle() {
    busy.current = true;
    setWorking(true);
    setError(null);
    try {
      setStatus(status?.running ? await stopRemote() : await startRemote(terminals, true));
    } catch (err) {
      setError(String(err));
    } finally {
      busy.current = false;
      setWorking(false);
    }
  }

  /** Add a device to the gateway that is already running.
   *
   *  This used to be stop-then-start: every phone already connected lost its
   *  socket, any turn one of them was watching went dark, and the tailnet
   *  mapping was torn down and rebuilt — all to mint eight characters the
   *  gateway produces from memory. It now asks the running process for a
   *  window instead. */
  async function pairAnother() {
    busy.current = true;
    setWorking(true);
    setError(null);
    try {
      setStatus(await pairRemote());
    } catch (err) {
      setError(String(err));
    } finally {
      busy.current = false;
      setWorking(false);
    }
  }

  async function revoke(id: string) {
    busy.current = true;
    setWorking(true);
    setError(null);
    try {
      setDevices(await revokeRemoteDevice(id));
    } catch (err) {
      setError(String(err));
    } finally {
      busy.current = false;
      setWorking(false);
    }
  }

  const running = status?.running ?? false;

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-neutral-100">Remote access</h3>
          <p className="mt-0.5 text-xs text-neutral-500">
            Reach these sessions and terminals from your phone.
          </p>
        </div>
        <button
          onClick={toggle}
          disabled={working}
          className={`shrink-0 rounded-full px-4 py-1.5 text-xs font-medium transition active:scale-95 disabled:opacity-50 ${
            running
              ? "bg-[#4f8dff] text-white"
              : "liquid-glass-subtle text-neutral-200 hover:text-[#4f8dff]"
          }`}
        >
          {working ? "…" : running ? "on" : "off"}
        </button>
      </div>

      {/* Shown whether or not the gateway is up. While it is running this is
          a readback of what the process actually started with — the banner it
          printed, not the flag we asked it for — and it is disabled because
          the flag is fixed at spawn. Hiding it entirely meant the one switch
          here that changes the security posture was invisible exactly when it
          mattered. */}
      <label
        className={`flex items-start gap-2.5 text-xs ${running ? "text-neutral-500" : "text-neutral-400"}`}
      >
        <input
          type="checkbox"
          checked={terminals}
          disabled={running}
          onChange={(e) => setTerminals(e.target.checked)}
          className="mt-0.5 accent-[#4f8dff]"
        />
        <span>
          Allow shells.
          <span className="text-neutral-500">
            {" "}A paired device gets a terminal on this machine, outside the
            workspace the agent is otherwise confined to.
            {running && " Switch remote access off and on again to change this."}
          </span>
        </span>
      </label>

      {running && (
        <div className="space-y-2 rounded-2xl border border-white/10 bg-white/5 p-3">
          {status?.url ? (
            <Field label="open on your phone" value={status.url} />
          ) : (
            <p className="text-xs text-neutral-400">
              Running, but not on your tailnet — so this machine is reachable
              from itself and nowhere else.
              <span className="mt-1 block text-neutral-500">
                Tailscale needs to be installed and signed in on this Mac. Once
                it is, switching this off and on again puts the gateway on it.
              </span>
            </p>
          )}

          {/* The code disappears on its own now — when it expires, and when
              it is used — because the gateway is asked whether the window is
              still open rather than the log being re-read for a line that
              never goes away. So both of these are reachable, which is what
              makes "pair another device" a button that exists. */}
          {status?.pairingCode ? (
            <Field label="pairing code" value={status.pairingCode} mono />
          ) : (
            <button
              onClick={pairAnother}
              disabled={working}
              className="text-xs text-[#4f8dff] disabled:opacity-50"
            >
              pair another device
            </button>
          )}

          {status?.terminals && (
            <p className="text-xs text-amber-300/80">Shells are exposed to paired devices.</p>
          )}
        </div>
      )}

      {/* Who can reach this machine. Pairing granted access and then the Mac
          forgot the device existed: no list, no last-seen, no revoke, and the
          only way to un-pair one was `daimon-remote --devices` in a terminal —
          which is the workflow this switch was built to replace. */}
      {devices.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[10px] uppercase tracking-wide text-neutral-500">
            paired devices
          </p>
          {devices.map((device) => (
            <div key={device.id} className="flex items-center gap-2 text-xs">
              <span className="min-w-0 flex-1 truncate text-neutral-300">{device.label}</span>
              <span className="shrink-0 text-neutral-600">{lastSeen(device.lastSeenAt)}</span>
              <button
                onClick={() => void revoke(device.id)}
                disabled={working || !running}
                title={running ? "un-pair this device" : "start remote access to un-pair"}
                className="shrink-0 rounded-full px-2 py-0.5 text-neutral-500 transition hover:bg-red-500/10 hover:text-red-300 disabled:opacity-40"
              >
                revoke
              </button>
            </div>
          ))}
        </div>
      )}

      {error && <p className="text-xs text-red-400">{error}</p>}
    </section>
  );
}

/** "never", or how long ago — the useful question about a paired device is
 *  whether it is still something you use. */
function lastSeen(at: number | null): string {
  if (!at) return "never used";
  const seconds = Date.now() / 1000 - at;
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        void navigator.clipboard.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="block w-full text-left"
    >
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">
        {copied ? "copied" : label}
      </div>
      <div className={`truncate text-xs text-neutral-200 ${mono ? "font-mono tracking-[0.2em]" : ""}`}>
        {value}
      </div>
    </button>
  );
}
