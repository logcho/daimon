import { useEffect, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  connectGmailAccount,
  disconnectGmailAccount,
  downloadVoiceModel,
  getAccessibilityTrustStatus,
  getApiKeyStatus,
  getClaudeCliStatus,
  getGmailAccount,
  getGoogleClientIdStatus,
  getVaultPathStatus,
  getVoiceModelStatus,
  setApiKey,
  setGoogleClientId,
  setVaultPath,
} from "../lib/api";
import type { ConnectedAccount, VaultPathStatus, VoiceModelStatus } from "../types";

type SaveState = "idle" | "saving" | "saved" | "error";
type ConnectState = "idle" | "connecting" | "error";
type DownloadState = "idle" | "downloading" | "error";

export function Settings() {
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [draft, setDraft] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [errorMessage, setErrorMessage] = useState("");

  const [hasGoogleClientId, setHasGoogleClientId] = useState<boolean | null>(null);
  const [clientIdDraft, setClientIdDraft] = useState("");
  const [clientIdSaveState, setClientIdSaveState] = useState<SaveState>("idle");
  const [clientIdErrorMessage, setClientIdErrorMessage] = useState("");

  // undefined = still checking, null = configured but no account connected yet
  const [gmailAccount, setGmailAccount] = useState<ConnectedAccount | null | undefined>(undefined);
  const [connectState, setConnectState] = useState<ConnectState>("idle");
  const [connectErrorMessage, setConnectErrorMessage] = useState("");
  const [disconnecting, setDisconnecting] = useState(false);

  const [vaultStatus, setVaultStatus] = useState<VaultPathStatus | null>(null);
  const [vaultPathDraft, setVaultPathDraft] = useState("");
  const [vaultSaveState, setVaultSaveState] = useState<SaveState>("idle");
  const [vaultErrorMessage, setVaultErrorMessage] = useState("");

  const [voiceStatus, setVoiceStatus] = useState<VoiceModelStatus | null>(null);
  const [downloadState, setDownloadState] = useState<DownloadState>("idle");
  const [downloadErrorMessage, setDownloadErrorMessage] = useState("");

  // null = still checking. Only meaningful for the Fn-key trigger — the
  // Cmd+Shift+D hotkey doesn't need Accessibility permission at all.
  const [accessibilityTrusted, setAccessibilityTrusted] = useState<boolean | null>(null);

  // null = still checking. Purely informational — the terminal tab always
  // opens and spawns a plain shell regardless of this; it's setup guidance,
  // not a gate.
  const [claudeCliFound, setClaudeCliFound] = useState<boolean | null>(null);

  function refreshVaultStatus() {
    return getVaultPathStatus()
      .then(setVaultStatus)
      .catch((err) => {
        setVaultErrorMessage(err instanceof Error ? err.message : String(err));
        setVaultSaveState("error");
      });
  }

  useEffect(() => {
    refreshVaultStatus();
  }, []);

  useEffect(() => {
    getApiKeyStatus()
      .then(setHasKey)
      .catch(() => setHasKey(false));
  }, []);

  useEffect(() => {
    getGoogleClientIdStatus()
      .then(setHasGoogleClientId)
      .catch(() => setHasGoogleClientId(false));
  }, []);

  useEffect(() => {
    if (!hasGoogleClientId) return;
    getGmailAccount()
      .then(setGmailAccount)
      .catch(() => setGmailAccount(null));
  }, [hasGoogleClientId]);

  useEffect(() => {
    getVoiceModelStatus()
      .then(setVoiceStatus)
      .catch((err) => {
        setDownloadErrorMessage(err instanceof Error ? err.message : String(err));
        setDownloadState("error");
      });
  }, []);

  // Re-checked every time Settings mounts (not just once at app startup) —
  // the user grants this in System Settings, outside the app entirely, so
  // the only way to reflect a just-granted permission without a full
  // restart is to check again whenever they come back to look.
  useEffect(() => {
    getAccessibilityTrustStatus()
      .then(setAccessibilityTrusted)
      .catch(() => setAccessibilityTrusted(false));
  }, []);

  // Re-checked every mount, same reasoning as accessibility trust above —
  // installing the CLI happens outside the app entirely, so the only way to
  // reflect a just-installed binary without a restart is to check again
  // whenever the user comes back to look.
  useEffect(() => {
    getClaudeCliStatus()
      .then(setClaudeCliFound)
      .catch(() => setClaudeCliFound(false));
  }, []);

  async function handleDownloadVoiceModel() {
    setDownloadState("downloading");
    setDownloadErrorMessage("");
    try {
      await downloadVoiceModel();
      const status = await getVoiceModelStatus();
      setVoiceStatus(status);
      setDownloadState("idle");
    } catch (err) {
      setDownloadErrorMessage(err instanceof Error ? err.message : String(err));
      setDownloadState("error");
    }
  }

  async function handleSaveClientId(e: React.FormEvent) {
    e.preventDefault();
    const clientId = clientIdDraft.trim();
    if (!clientId) return;

    setClientIdSaveState("saving");
    try {
      await setGoogleClientId(clientId);
      setHasGoogleClientId(true);
      setClientIdDraft("");
      setClientIdSaveState("saved");
      setTimeout(() => setClientIdSaveState("idle"), 2500);
    } catch (err) {
      setClientIdErrorMessage(err instanceof Error ? err.message : String(err));
      setClientIdSaveState("error");
    }
  }

  async function handleConnectGmail() {
    setConnectState("connecting");
    setConnectErrorMessage("");
    try {
      const account = await connectGmailAccount();
      setGmailAccount(account);
      setConnectState("idle");
    } catch (err) {
      setConnectErrorMessage(err instanceof Error ? err.message : String(err));
      setConnectState("error");
    }
  }

  async function handleDisconnectGmail() {
    setDisconnecting(true);
    try {
      await disconnectGmailAccount();
      setGmailAccount(null);
      setConnectState("idle");
      setConnectErrorMessage("");
    } catch (err) {
      setConnectErrorMessage(err instanceof Error ? err.message : String(err));
      setConnectState("error");
    } finally {
      setDisconnecting(false);
    }
  }

  async function handleSaveVaultPath(e: React.FormEvent) {
    e.preventDefault();
    const path = vaultPathDraft.trim();
    if (!path) return;

    setVaultSaveState("saving");
    try {
      await setVaultPath(path);
      setVaultPathDraft("");
      await refreshVaultStatus();
      setVaultSaveState("saved");
      setTimeout(() => setVaultSaveState("idle"), 2500);
    } catch (err) {
      setVaultErrorMessage(err instanceof Error ? err.message : String(err));
      setVaultSaveState("error");
    }
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    const key = draft.trim();
    if (!key) return;

    setSaveState("saving");
    try {
      await setApiKey(key);
      setHasKey(true);
      setDraft("");
      setSaveState("saved");
      setTimeout(() => setSaveState("idle"), 2500);
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err));
      setSaveState("error");
    }
  }

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <h3 className="font-mono text-sm text-neutral-100">anthropic_api_key</h3>
      <p className="mt-1 font-mono text-xs text-neutral-500">
        {hasKey === null && "checking…"}
        {hasKey === true && <span className="text-emerald-400">● configured</span>}
        {hasKey === false && (
          <span>○ not set — tasks run a scripted demo instead of the real agent</span>
        )}
      </p>

      <form onSubmit={handleSave} className="mt-4 space-y-2">
        <input
          type="password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="sk-ant-..."
          className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
        />
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={!draft.trim() || saveState === "saving"}
            className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
          >
            {saveState === "saving" ? "saving…" : "save"}
          </button>
          {saveState === "saved" && (
            <span className="font-mono text-xs text-emerald-400">saved — applies to the next task</span>
          )}
          {saveState === "error" && <span className="font-mono text-xs text-red-400">{errorMessage}</span>}
        </div>
      </form>

      <p className="mt-6 font-mono text-xs leading-relaxed text-neutral-600">
        get a key from{" "}
        <button
          type="button"
          onClick={() => openUrl("https://console.anthropic.com/settings/keys")}
          className="text-[#4f8dff] hover:underline"
        >
          console.anthropic.com
        </button>
        . stored locally in .env, only ever sent into the background workspace at task time.
      </p>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="font-mono text-sm text-neutral-100">connected_accounts</h3>

        {hasGoogleClientId === false && (
          <>
            <p className="mt-1 font-mono text-xs text-neutral-500">
              ○ no google oauth client configured — needed before connecting gmail
            </p>
            <form onSubmit={handleSaveClientId} className="mt-4 space-y-2">
              <input
                type="text"
                value={clientIdDraft}
                onChange={(e) => setClientIdDraft(e.target.value)}
                placeholder="xxxxxxxxxxxx.apps.googleusercontent.com"
                className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
              />
              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={!clientIdDraft.trim() || clientIdSaveState === "saving"}
                  className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
                >
                  {clientIdSaveState === "saving" ? "saving…" : "save"}
                </button>
                {clientIdSaveState === "saved" && (
                  <span className="font-mono text-xs text-emerald-400">saved</span>
                )}
                {clientIdSaveState === "error" && (
                  <span className="font-mono text-xs text-red-400">{clientIdErrorMessage}</span>
                )}
              </div>
            </form>
            <p className="mt-3 font-mono text-xs leading-relaxed text-neutral-600">
              create one in{" "}
              <button
                type="button"
                onClick={() => openUrl("https://console.cloud.google.com/apis/credentials")}
                className="text-[#4f8dff] hover:underline"
              >
                console.cloud.google.com
              </button>{" "}
              → apis &amp; services → credentials → oauth client id, type "desktop app".
            </p>
          </>
        )}

        {hasGoogleClientId === null && (
          <p className="mt-1 font-mono text-xs text-neutral-500">checking…</p>
        )}

        {hasGoogleClientId === true && (
          <div className="mt-3 flex items-center justify-between rounded-lg border border-white/10 bg-white/5 px-3 py-2.5">
            <div>
              <p className="font-mono text-sm text-neutral-100">gmail</p>
              {gmailAccount === undefined && (
                <p className="font-mono text-xs text-neutral-500">checking…</p>
              )}
              {gmailAccount === null && (
                <p className="font-mono text-xs text-neutral-500">○ not connected</p>
              )}
              {gmailAccount && (
                <p className="font-mono text-xs text-emerald-400">● {gmailAccount.email}</p>
              )}
              {connectState === "error" && (
                <p className="mt-1 font-mono text-xs text-red-400">{connectErrorMessage}</p>
              )}
            </div>

            {gmailAccount ? (
              <button
                type="button"
                onClick={handleDisconnectGmail}
                disabled={disconnecting}
                className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
              >
                {disconnecting ? "disconnecting…" : "disconnect"}
              </button>
            ) : (
              <button
                type="button"
                onClick={handleConnectGmail}
                disabled={connectState === "connecting" || gmailAccount === undefined}
                className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
              >
                {connectState === "connecting" ? "waiting for google sign-in…" : "connect gmail"}
              </button>
            )}
          </div>
        )}
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="font-mono text-sm text-neutral-100">vault</h3>
        <p className="mt-1 font-mono text-xs text-neutral-500">
          {vaultStatus === null && vaultSaveState !== "error" && "checking…"}
          {vaultStatus && vaultStatus.isDefault && (
            <span className="text-emerald-400">● using default vault at {vaultStatus.path}</span>
          )}
          {vaultStatus && !vaultStatus.isDefault && (
            <span className="text-emerald-400">● using custom vault at {vaultStatus.path}</span>
          )}
          {vaultStatus === null && vaultSaveState === "error" && (
            <span className="text-red-400">could not read vault status</span>
          )}
        </p>

        <form onSubmit={handleSaveVaultPath} className="mt-4 space-y-2">
          <input
            type="text"
            value={vaultPathDraft}
            onChange={(e) => setVaultPathDraft(e.target.value)}
            placeholder="/Users/you/Documents/my-obsidian-vault"
            className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!vaultPathDraft.trim() || vaultSaveState === "saving"}
              className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
            >
              {vaultSaveState === "saving" ? "saving…" : "save"}
            </button>
            {vaultSaveState === "saved" && (
              <span className="font-mono text-xs text-emerald-400">saved</span>
            )}
            {vaultSaveState === "error" && (
              <span className="font-mono text-xs text-red-400">{vaultErrorMessage}</span>
            )}
          </div>
        </form>

        <p className="mt-3 font-mono text-xs leading-relaxed text-neutral-600">
          notes here are searchable context daimon reads while planning tasks. point this at a real
          obsidian vault and the agent's notes show up there directly, alongside your own.
        </p>
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="font-mono text-sm text-neutral-100">voice</h3>
        <p className="mt-1 font-mono text-xs text-neutral-500">
          {voiceStatus === null && downloadState !== "error" && "checking…"}
          {voiceStatus?.downloaded && (
            <span className="text-emerald-400">● {voiceStatus.modelName} downloaded</span>
          )}
          {voiceStatus && !voiceStatus.downloaded && downloadState !== "downloading" && (
            <span>○ {voiceStatus.modelName} not downloaded yet — required before dictation works</span>
          )}
          {downloadState === "downloading" && (
            <span className="text-[#4f8dff]">downloading… this may take a minute</span>
          )}
          {voiceStatus === null && downloadState === "error" && (
            <span className="text-red-400">could not read voice model status</span>
          )}
        </p>

        {voiceStatus && !voiceStatus.downloaded && (
          <div className="mt-4 flex items-center gap-3">
            <button
              type="button"
              onClick={handleDownloadVoiceModel}
              disabled={downloadState === "downloading"}
              className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
            >
              {downloadState === "downloading" ? "downloading…" : "download model"}
            </button>
            {downloadState === "error" && (
              <span className="font-mono text-xs text-red-400">{downloadErrorMessage}</span>
            )}
          </div>
        )}

        <p className="mt-3 font-mono text-xs leading-relaxed text-neutral-600">
          once downloaded, press <span className="text-neutral-400">⌘⇧D</span> (or ctrl+shift+d) anywhere to
          start dictating — press it again to stop and transcribe. runs fully on-device; nothing you say
          leaves this machine.
        </p>

        <div className="mt-4 flex items-center justify-between rounded-lg border border-white/10 bg-white/5 px-3 py-2.5">
          <div>
            <p className="font-mono text-sm text-neutral-100">fn key trigger</p>
            <p className="mt-0.5 font-mono text-xs text-neutral-500">
              {accessibilityTrusted === null && "checking…"}
              {accessibilityTrusted === true && <span className="text-emerald-400">● accessibility permission granted</span>}
              {accessibilityTrusted === false && (
                <span>○ accessibility permission required — the fn key won't trigger dictation without it</span>
              )}
            </p>
          </div>
          {accessibilityTrusted === false && (
            <button
              type="button"
              onClick={() => openUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")}
              className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95"
            >
              open settings
            </button>
          )}
        </div>
        <p className="mt-3 font-mono text-xs leading-relaxed text-neutral-600">
          the fn key is an additional trigger alongside ⌘⇧D — grant accessibility permission above, then
          restart daimon, and a single press of fn toggles dictation the same way.
        </p>
        <p className="mt-2 font-mono text-xs leading-relaxed text-neutral-600">
          macos itself also opens the character viewer on a fn tap by default — daimon can only observe
          the keypress, not suppress that. turn it off in{" "}
          <span className="text-neutral-400">system settings → keyboard → "press 🌐 key to" → do nothing</span>{" "}
          so fn only triggers dictation.
        </p>
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="font-mono text-sm text-neutral-100">claude code</h3>
        <p className="mt-1 font-mono text-xs text-neutral-500">
          {claudeCliFound === null && "checking…"}
          {claudeCliFound === true && <span className="text-emerald-400">● claude cli found on PATH</span>}
          {claudeCliFound === false && <span>○ claude cli not found on PATH</span>}
        </p>

        {claudeCliFound === false && (
          <p className="mt-3 font-mono text-xs leading-relaxed text-neutral-600">
            install it with{" "}
            <span className="rounded bg-white/5 px-1 py-0.5 text-neutral-400">
              npm install -g @anthropic-ai/claude-code
            </span>
            .
          </p>
        )}

        <p className="mt-3 font-mono text-xs leading-relaxed text-neutral-600">
          the terminal tab opens a real shell on this machine either way — once installed, just type{" "}
          <span className="text-neutral-400">claude</span> inside it to start a session against your real project
          files.
        </p>
      </div>
    </div>
  );
}
