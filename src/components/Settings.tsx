import { useEffect, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  connectGmailAccount,
  connectSpotifyAccount,
  disconnectGmailAccount,
  disconnectSpotifyAccount,
  downloadVoiceModel,
  finishBrowserLogin,
  getAccessibilityTrustStatus,
  getApiKeyStatus,
  getBrowserLoginStatus,
  getClaudeCliStatus,
  getGmailAccount,
  getGoogleClientIdStatus,
  getSpotifyAccount,
  getSpotifyClientIdStatus,
  getVaultPathStatus,
  getVoiceModelStatus,
  loginBrowserProfile,
  setApiKey,
  setGoogleClientId,
  setSpotifyClientId,
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

  // Same shape as the Google client id / Gmail account state above.
  const [hasSpotifyClientId, setHasSpotifyClientId] = useState<boolean | null>(null);
  const [spotifyClientIdDraft, setSpotifyClientIdDraft] = useState("");
  const [spotifyClientIdSaveState, setSpotifyClientIdSaveState] = useState<SaveState>("idle");
  const [spotifyClientIdErrorMessage, setSpotifyClientIdErrorMessage] = useState("");
  const [spotifyAccount, setSpotifyAccount] = useState<ConnectedAccount | null | undefined>(undefined);
  const [spotifyConnectState, setSpotifyConnectState] = useState<ConnectState>("idle");
  const [spotifyConnectErrorMessage, setSpotifyConnectErrorMessage] = useState("");
  const [spotifyDisconnecting, setSpotifyDisconnecting] = useState(false);

  // null = still checking. true only once a real login has actually been
  // captured (a non-empty cookie jar), not just whether the flow's been
  // opened before — see get_browser_login_status's own doc comment.
  const [loginStatus, setLoginStatus] = useState<boolean | null>(null);
  const [loginState, setLoginState] = useState<"idle" | "opening" | "open" | "finishing" | "error">("idle");
  const [loginErrorMessage, setLoginErrorMessage] = useState("");

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
    getBrowserLoginStatus()
      .then(setLoginStatus)
      .catch(() => setLoginStatus(false));
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
    getSpotifyClientIdStatus()
      .then(setHasSpotifyClientId)
      .catch(() => setHasSpotifyClientId(false));
  }, []);

  useEffect(() => {
    if (!hasSpotifyClientId) return;
    getSpotifyAccount()
      .then(setSpotifyAccount)
      .catch(() => setSpotifyAccount(null));
  }, [hasSpotifyClientId]);

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

  async function handleSaveSpotifyClientId(e: React.FormEvent) {
    e.preventDefault();
    const clientId = spotifyClientIdDraft.trim();
    if (!clientId) return;

    setSpotifyClientIdSaveState("saving");
    try {
      await setSpotifyClientId(clientId);
      setHasSpotifyClientId(true);
      setSpotifyClientIdDraft("");
      setSpotifyClientIdSaveState("saved");
      setTimeout(() => setSpotifyClientIdSaveState("idle"), 2500);
    } catch (err) {
      setSpotifyClientIdErrorMessage(err instanceof Error ? err.message : String(err));
      setSpotifyClientIdSaveState("error");
    }
  }

  async function handleConnectSpotify() {
    setSpotifyConnectState("connecting");
    setSpotifyConnectErrorMessage("");
    try {
      const account = await connectSpotifyAccount();
      setSpotifyAccount(account);
      setSpotifyConnectState("idle");
    } catch (err) {
      setSpotifyConnectErrorMessage(err instanceof Error ? err.message : String(err));
      setSpotifyConnectState("error");
    }
  }

  async function handleDisconnectSpotify() {
    setSpotifyDisconnecting(true);
    try {
      await disconnectSpotifyAccount();
      setSpotifyAccount(null);
      setSpotifyConnectState("idle");
      setSpotifyConnectErrorMessage("");
    } catch (err) {
      setSpotifyConnectErrorMessage(err instanceof Error ? err.message : String(err));
      setSpotifyConnectState("error");
    } finally {
      setSpotifyDisconnecting(false);
    }
  }

  async function handleStartLogin() {
    setLoginState("opening");
    setLoginErrorMessage("");
    try {
      await loginBrowserProfile();
      setLoginState("open");
    } catch (err) {
      setLoginErrorMessage(err instanceof Error ? err.message : String(err));
      setLoginState("error");
    }
  }

  async function handleFinishLogin() {
    setLoginState("finishing");
    try {
      await finishBrowserLogin();
      const status = await getBrowserLoginStatus();
      setLoginStatus(status);
      setLoginState("idle");
    } catch (err) {
      setLoginErrorMessage(err instanceof Error ? err.message : String(err));
      setLoginState("error");
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
      <h3 className="text-sm font-semibold tracking-tight text-neutral-100">anthropic_api_key</h3>
      <p className="mt-1 text-xs text-neutral-500">
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
            className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
          >
            {saveState === "saving" ? "saving…" : "save"}
          </button>
          {saveState === "saved" && (
            <span className="text-xs text-emerald-400">saved — applies to the next task</span>
          )}
          {saveState === "error" && <span className="text-xs text-red-400">{errorMessage}</span>}
        </div>
      </form>

      <p className="mt-6 text-xs leading-relaxed text-neutral-600">
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
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">connected_accounts</h3>

        {hasGoogleClientId === false && (
          <>
            <p className="mt-1 text-xs text-neutral-500">
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
                  className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
                >
                  {clientIdSaveState === "saving" ? "saving…" : "save"}
                </button>
                {clientIdSaveState === "saved" && (
                  <span className="text-xs text-emerald-400">saved</span>
                )}
                {clientIdSaveState === "error" && (
                  <span className="text-xs text-red-400">{clientIdErrorMessage}</span>
                )}
              </div>
            </form>
            <p className="mt-3 text-xs leading-relaxed text-neutral-600">
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
          <p className="mt-1 text-xs text-neutral-500">checking…</p>
        )}

        {hasGoogleClientId === true && (
          <div className="mt-3 flex items-center justify-between liquid-glass-subtle rounded-xl px-3 py-2.5">
            <div>
              <p className="text-sm font-medium text-neutral-100">gmail</p>
              {gmailAccount === undefined && (
                <p className="text-xs text-neutral-500">checking…</p>
              )}
              {gmailAccount === null && (
                <p className="text-xs text-neutral-500">○ not connected</p>
              )}
              {gmailAccount && (
                <p className="text-xs text-emerald-400">● {gmailAccount.email}</p>
              )}
              {connectState === "error" && (
                <p className="mt-1 text-xs text-red-400">{connectErrorMessage}</p>
              )}
            </div>

            {gmailAccount ? (
              <button
                type="button"
                onClick={handleDisconnectGmail}
                disabled={disconnecting}
                className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
              >
                {disconnecting ? "disconnecting…" : "disconnect"}
              </button>
            ) : (
              <button
                type="button"
                onClick={handleConnectGmail}
                disabled={connectState === "connecting" || gmailAccount === undefined}
                className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
              >
                {connectState === "connecting" ? "waiting for google sign-in…" : "connect gmail"}
              </button>
            )}
          </div>
        )}

        {hasSpotifyClientId === false && (
          <>
            <p className="mt-4 text-xs text-neutral-500">
              ○ no spotify client id configured — needed before connecting spotify (used by
              play_music_by_name to search and play a specific song/artist through your real
              desktop app)
            </p>
            <form onSubmit={handleSaveSpotifyClientId} className="mt-4 space-y-2">
              <input
                type="text"
                value={spotifyClientIdDraft}
                onChange={(e) => setSpotifyClientIdDraft(e.target.value)}
                placeholder="spotify client id"
                className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
              />
              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={!spotifyClientIdDraft.trim() || spotifyClientIdSaveState === "saving"}
                  className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
                >
                  {spotifyClientIdSaveState === "saving" ? "saving…" : "save"}
                </button>
                {spotifyClientIdSaveState === "saved" && (
                  <span className="text-xs text-emerald-400">saved</span>
                )}
                {spotifyClientIdSaveState === "error" && (
                  <span className="text-xs text-red-400">{spotifyClientIdErrorMessage}</span>
                )}
              </div>
            </form>
            <p className="mt-3 text-xs leading-relaxed text-neutral-600">
              create one in{" "}
              <button
                type="button"
                onClick={() => openUrl("https://developer.spotify.com/dashboard")}
                className="text-[#4f8dff] hover:underline"
              >
                developer.spotify.com/dashboard
              </button>{" "}
              → create app → add this exact redirect URI (Spotify requires an exact match, unlike
              Google):{" "}
              <code className="text-neutral-400">http://127.0.0.1:38214/callback</code>
            </p>
          </>
        )}

        {hasSpotifyClientId === null && (
          <p className="mt-4 text-xs text-neutral-500">checking…</p>
        )}

        {hasSpotifyClientId === true && (
          <div className="mt-3 flex items-center justify-between liquid-glass-subtle rounded-xl px-3 py-2.5">
            <div>
              <p className="text-sm font-medium text-neutral-100">spotify</p>
              {spotifyAccount === undefined && (
                <p className="text-xs text-neutral-500">checking…</p>
              )}
              {spotifyAccount === null && (
                <p className="text-xs text-neutral-500">○ not connected</p>
              )}
              {spotifyAccount && (
                <p className="text-xs text-emerald-400">● {spotifyAccount.email}</p>
              )}
              {spotifyConnectState === "error" && (
                <p className="mt-1 text-xs text-red-400">{spotifyConnectErrorMessage}</p>
              )}
            </div>

            {spotifyAccount ? (
              <button
                type="button"
                onClick={handleDisconnectSpotify}
                disabled={spotifyDisconnecting}
                className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
              >
                {spotifyDisconnecting ? "disconnecting…" : "disconnect"}
              </button>
            ) : (
              <button
                type="button"
                onClick={handleConnectSpotify}
                disabled={spotifyConnectState === "connecting" || spotifyAccount === undefined}
                className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
              >
                {spotifyConnectState === "connecting" ? "waiting for spotify sign-in…" : "connect spotify"}
              </button>
            )}
          </div>
        )}
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">browser_login</h3>
        <p className="mt-1 text-xs leading-relaxed text-neutral-500">
          log in once in a real browser window, and daimon's own background browser reuses that
          login (cookies, session) for every task after — without this, every task starts logged
          out, e.g. daimon can't act on linkedin as you until you do this.
        </p>

        <div className="mt-3 flex items-center justify-between liquid-glass-subtle rounded-xl px-3 py-2.5">
          <div>
            <p className="text-sm font-medium text-neutral-100">websites</p>
            {loginStatus === null && loginState === "idle" && (
              <p className="text-xs text-neutral-500">checking…</p>
            )}
            {loginStatus === false && loginState === "idle" && (
              <p className="text-xs text-neutral-500">○ not logged in anywhere yet</p>
            )}
            {loginStatus === true && loginState === "idle" && (
              <p className="text-xs text-emerald-400">● logged in</p>
            )}
            {loginState === "open" && (
              <p className="text-xs text-[#4f8dff]">a real browser window is open — log in, then click done</p>
            )}
            {loginState === "error" && <p className="mt-1 text-xs text-red-400">{loginErrorMessage}</p>}
          </div>

          {loginState === "open" ? (
            <button
              type="button"
              onClick={handleFinishLogin}
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95"
            >
              done
            </button>
          ) : (
            <button
              type="button"
              onClick={handleStartLogin}
              disabled={loginState === "opening" || loginState === "finishing"}
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
            >
              {loginState === "opening"
                ? "opening…"
                : loginState === "finishing"
                  ? "saving…"
                  : loginStatus
                    ? "log in again"
                    : "log in"}
            </button>
          )}
        </div>
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">vault</h3>
        <p className="mt-1 text-xs text-neutral-500">
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
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
            >
              {vaultSaveState === "saving" ? "saving…" : "save"}
            </button>
            {vaultSaveState === "saved" && (
              <span className="text-xs text-emerald-400">saved</span>
            )}
            {vaultSaveState === "error" && (
              <span className="text-xs text-red-400">{vaultErrorMessage}</span>
            )}
          </div>
        </form>

        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          notes here are searchable context daimon reads while planning tasks. point this at a real
          obsidian vault and the agent's notes show up there directly, alongside your own.
        </p>
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">voice</h3>
        <p className="mt-1 text-xs text-neutral-500">
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
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95 disabled:opacity-40"
            >
              {downloadState === "downloading" ? "downloading…" : "download model"}
            </button>
            {downloadState === "error" && (
              <span className="text-xs text-red-400">{downloadErrorMessage}</span>
            )}
          </div>
        )}

        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          once downloaded, press <span className="text-neutral-400">⌘⇧D</span> (or ctrl+shift+d) anywhere to
          start dictating — press it again to stop and transcribe. runs fully on-device; nothing you say
          leaves this machine.
        </p>

        <div className="mt-4 flex items-center justify-between liquid-glass-subtle rounded-xl px-3 py-2.5">
          <div>
            <p className="text-sm font-medium text-neutral-100">fn key trigger</p>
            <p className="mt-0.5 text-xs text-neutral-500">
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
              className="liquid-glass-subtle rounded-full px-4 py-1.5 text-xs font-medium text-neutral-100 transition duration-200 hover:text-white hover:[border-color:rgba(255,255,255,0.25)] active:scale-95"
            >
              open settings
            </button>
          )}
        </div>
        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          the fn key is an additional trigger alongside ⌘⇧D — grant accessibility permission above, then
          restart daimon, and a single press of fn toggles dictation the same way.
        </p>
        <p className="mt-2 text-xs leading-relaxed text-neutral-600">
          macos itself also opens the character viewer on a fn tap by default — daimon can only observe
          the keypress, not suppress that. turn it off in{" "}
          <span className="text-neutral-400">system settings → keyboard → "press 🌐 key to" → do nothing</span>{" "}
          so fn only triggers dictation.
        </p>
      </div>

      <div className="mt-8 border-t border-white/5 pt-6">
        <h3 className="text-sm font-semibold tracking-tight text-neutral-100">claude code</h3>
        <p className="mt-1 text-xs text-neutral-500">
          {claudeCliFound === null && "checking…"}
          {claudeCliFound === true && <span className="text-emerald-400">● claude cli found on PATH</span>}
          {claudeCliFound === false && <span>○ claude cli not found on PATH</span>}
        </p>

        {claudeCliFound === false && (
          <p className="mt-3 text-xs leading-relaxed text-neutral-600">
            install it with{" "}
            <span className="rounded bg-white/5 px-1 py-0.5 font-mono text-neutral-400">
              npm install -g @anthropic-ai/claude-code
            </span>
            .
          </p>
        )}

        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          the terminal tab opens a real shell on this machine either way — once installed, just type{" "}
          <span className="font-mono text-neutral-400">claude</span> inside it to start a session against your real project
          files.
        </p>
      </div>
    </div>
  );
}
