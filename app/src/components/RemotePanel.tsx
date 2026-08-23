import { useCallback, useEffect, useState } from "react";
import { remoteStatus, startRemote, stopRemote, type RemoteStatus } from "../api";

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

  const refresh = useCallback(async () => {
    try {
      const next = await remoteStatus();
      setStatus(next);
      setTerminals(next.terminals);
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
    setWorking(true);
    setError(null);
    try {
      setStatus(status?.running ? await stopRemote() : await startRemote(terminals, true));
    } catch (err) {
      setError(String(err));
    } finally {
      setWorking(false);
    }
  }

  async function pairAnother() {
    setWorking(true);
    try {
      await stopRemote();
      setStatus(await startRemote(terminals, true));
    } catch (err) {
      setError(String(err));
    } finally {
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

      {!running && (
        <label className="flex items-start gap-2.5 text-xs text-neutral-400">
          <input
            type="checkbox"
            checked={terminals}
            onChange={(e) => setTerminals(e.target.checked)}
            className="mt-0.5 accent-[#4f8dff]"
          />
          <span>
            Allow shells.
            <span className="text-neutral-500">
              {" "}A paired device gets a terminal on this machine, outside the
              workspace the agent is otherwise confined to.
            </span>
          </span>
        </label>
      )}

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

          {status?.pairingCode ? (
            <Field label="pairing code" value={status.pairingCode} mono />
          ) : (
            <button onClick={pairAnother} disabled={working} className="text-xs text-[#4f8dff]">
              pair another device
            </button>
          )}

          {status?.terminals && (
            <p className="text-xs text-amber-300/80">Shells are exposed to paired devices.</p>
          )}
        </div>
      )}

      {error && <p className="text-xs text-red-400">{error}</p>}
    </section>
  );
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
