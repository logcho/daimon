import { useEffect, useState } from "react";
import type { ConnectedAccount } from "../../types";
import { BUTTON_CLASS, SettingRow, StatusLine, TextSetting } from "./primitives";

/**
 * The full "connect a third-party account" flow: a client-id form shown until
 * one is configured, then a connect/disconnect row.
 *
 * Gmail and Spotify were previously two near-verbatim copies of this — same
 * four state variables, same three handlers, same markup, differing only in
 * labels, help text, and which four api.ts functions they called. Spotify's
 * copy also silently drifted (its client-id form used a `mt-4` status line
 * where Google's used `mt-1`). One component, two call sites.
 */
export function OAuthAccount({
  label,
  clientIdPlaceholder,
  clientIdHelp,
  connectLabel,
  getClientIdStatus,
  setClientId,
  getAccount,
  connectAccount,
  disconnectAccount,
}: {
  label: string;
  clientIdPlaceholder: string;
  clientIdHelp: React.ReactNode;
  connectLabel: string;
  getClientIdStatus: () => Promise<boolean>;
  setClientId: (id: string) => Promise<void>;
  getAccount: () => Promise<ConnectedAccount | null>;
  connectAccount: () => Promise<ConnectedAccount>;
  disconnectAccount: () => Promise<void>;
}) {
  const [hasClientId, setHasClientId] = useState<boolean | null>(null);
  // undefined = still checking, null = configured but not connected yet.
  const [account, setAccount] = useState<ConnectedAccount | null | undefined>(undefined);
  const [busy, setBusy] = useState<"idle" | "connecting" | "disconnecting">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    getClientIdStatus()
      .then(setHasClientId)
      .catch(() => setHasClientId(false));
  }, [getClientIdStatus]);

  useEffect(() => {
    if (!hasClientId) return;
    getAccount()
      .then(setAccount)
      .catch(() => setAccount(null));
  }, [hasClientId, getAccount]);

  async function run(action: () => Promise<void>, phase: "connecting" | "disconnecting") {
    setBusy(phase);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("idle");
    }
  }

  if (hasClientId === null) return <StatusLine state="checking" />;

  if (!hasClientId) {
    return (
      <>
        <StatusLine state="unset">no {label} client id configured — needed before connecting</StatusLine>
        <TextSetting
          placeholder={clientIdPlaceholder}
          onSave={async (id) => {
            await setClientId(id);
            setHasClientId(true);
          }}
        />
        <p className="mt-3 text-xs leading-relaxed text-neutral-600">{clientIdHelp}</p>
      </>
    );
  }

  return (
    <SettingRow
      label={label}
      action={
        account ? (
          <button
            type="button"
            onClick={() => run(async () => { await disconnectAccount(); setAccount(null); }, "disconnecting")}
            disabled={busy !== "idle"}
            className={BUTTON_CLASS}
          >
            {busy === "disconnecting" ? "disconnecting…" : "disconnect"}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => run(async () => setAccount(await connectAccount()), "connecting")}
            disabled={busy !== "idle" || account === undefined}
            className={BUTTON_CLASS}
          >
            {busy === "connecting" ? `waiting for ${label} sign-in…` : connectLabel}
          </button>
        )
      }
    >
      {account === undefined && <p className="text-xs text-neutral-500">checking…</p>}
      {account === null && <p className="text-xs text-neutral-500">○ not connected</p>}
      {account && <p className="text-xs text-emerald-400">● {account.email}</p>}
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </SettingRow>
  );
}
