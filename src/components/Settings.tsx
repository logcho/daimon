import { useCallback, useEffect, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  connectGmailAccount,
  connectSpotifyAccount,
  disconnectGmailAccount,
  disconnectSpotifyAccount,
  downloadVoiceModel,
  finishBrowserLogin,
  getAccessibilityTrustStatus,
  getAgentAuthMode,
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
  setAgentAuthMode,
  setApiKey,
  setGoogleClientId,
  setSpotifyClientId,
  setVaultPath,
  type AgentAuthMode,
} from "../lib/api";
import type { VaultPathStatus, VoiceModelStatus } from "../types";
import { OAuthAccount } from "./settings/OAuthAccount";
import { BUTTON_CLASS, Section, SettingRow, StatusLine, TextSetting } from "./settings/primitives";

export function Settings() {
  const [authMode, setAuthMode] = useState<AgentAuthMode | null>(null);
  const [authError, setAuthError] = useState("");
  const [hasKey, setHasKey] = useState<boolean | null>(null);

  const [loginStatus, setLoginStatus] = useState<boolean | null>(null);
  const [loginState, setLoginState] = useState<"idle" | "opening" | "open" | "finishing" | "error">("idle");
  const [loginError, setLoginError] = useState("");

  const [vaultStatus, setVaultStatus] = useState<VaultPathStatus | null>(null);
  const [voiceStatus, setVoiceStatus] = useState<VoiceModelStatus | null>(null);
  const [downloadState, setDownloadState] = useState<"idle" | "downloading" | "error">("idle");
  const [downloadError, setDownloadError] = useState("");

  // Both re-checked on every mount (i.e. every time this tab is opened),
  // because both are granted/installed outside the app and can change while
  // Daimon is running.
  const [accessibilityTrusted, setAccessibilityTrusted] = useState<boolean | null>(null);
  const [claudeCliFound, setClaudeCliFound] = useState<boolean | null>(null);

  const refreshVault = useCallback(
    () => getVaultPathStatus().then(setVaultStatus).catch(() => setVaultStatus(null)),
    [],
  );

  useEffect(() => {
    getAgentAuthMode().then(setAuthMode).catch(() => setAuthMode("api_key"));
    getApiKeyStatus().then(setHasKey).catch(() => setHasKey(false));
    getClaudeCliStatus().then(setClaudeCliFound).catch(() => setClaudeCliFound(false));
    getBrowserLoginStatus().then(setLoginStatus).catch(() => setLoginStatus(false));
    getVoiceModelStatus().then(setVoiceStatus).catch(() => setVoiceStatus(null));
    getAccessibilityTrustStatus().then(setAccessibilityTrusted).catch(() => setAccessibilityTrusted(false));
    refreshVault();
  }, [refreshVault]);

  async function chooseAuthMode(mode: AgentAuthMode) {
    const previous = authMode;
    setAuthMode(mode);
    setAuthError("");
    try {
      await setAgentAuthMode(mode);
    } catch (err) {
      // Rolled back rather than left showing a selection the daemon rejected
      // — most often "Claude Code isn't installed", which set_agent_auth_mode
      // checks before persisting.
      setAuthMode(previous);
      setAuthError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleDownloadVoiceModel() {
    setDownloadState("downloading");
    setDownloadError("");
    try {
      await downloadVoiceModel();
      setVoiceStatus(await getVoiceModelStatus());
      setDownloadState("idle");
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : String(err));
      setDownloadState("error");
    }
  }

  async function handleStartLogin() {
    setLoginState("opening");
    setLoginError("");
    try {
      await loginBrowserProfile();
      setLoginState("open");
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : String(err));
      setLoginState("error");
    }
  }

  async function handleFinishLogin() {
    setLoginState("finishing");
    try {
      await finishBrowserLogin();
      setLoginStatus(await getBrowserLoginStatus());
      setLoginState("idle");
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : String(err));
      setLoginState("error");
    }
  }

  const subscription = authMode === "subscription";

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      {/* Merged from what used to be two separate sections at opposite ends
          of this page — `anthropic_api_key` at the top and a purely
          informational `claude code` block at the bottom. They were always
          the same decision: how the agent authenticates. */}
      <Section title="agent" divider={false}>
        <p className="mt-1 text-xs leading-relaxed text-neutral-500">
          how daimon's agent authenticates. changing this applies to newly started sessions — an
          open session keeps whatever it launched with.
        </p>

        <div className="mt-4 flex gap-2">
          {(
            [
              ["subscription", "claude code subscription"],
              ["api_key", "anthropic api key"],
            ] as const
          ).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              onClick={() => chooseAuthMode(mode)}
              disabled={authMode === null}
              className={`flex-1 rounded-full border px-3 py-2 text-xs font-medium transition duration-200 active:scale-95 disabled:opacity-40 ${
                authMode === mode
                  ? "border-[#4f8dff]/45 bg-[#4f8dff]/[0.18] text-neutral-50 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)]"
                  : "border-white/10 bg-white/5 text-neutral-400 hover:text-neutral-100"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {authError && <p className="mt-2 text-xs text-red-400">{authError}</p>}

        {subscription ? (
          <>
            {claudeCliFound === null && <StatusLine state="checking" />}
            {claudeCliFound === true && <StatusLine state="ok">claude code found on PATH</StatusLine>}
            {claudeCliFound === false && (
              <StatusLine state="unset">claude code not found on PATH</StatusLine>
            )}
            {claudeCliFound === false ? (
              <p className="mt-3 text-xs leading-relaxed text-neutral-600">
                install it with{" "}
                <span className="rounded bg-white/5 px-1 py-0.5 font-mono text-neutral-400">
                  npm install -g @anthropic-ai/claude-code
                </span>{" "}
                and run <span className="font-mono text-neutral-400">claude login</span>.
              </p>
            ) : (
              <p className="mt-3 text-xs leading-relaxed text-neutral-600">
                turns run against your claude subscription. if daimon reports an auth error, run{" "}
                <span className="font-mono text-neutral-400">claude login</span> in the terminal tab
                once — daimon never sees or stores those credentials.
              </p>
            )}
          </>
        ) : (
          <>
            {hasKey === null && <StatusLine state="checking" />}
            {hasKey === true && <StatusLine state="ok">key configured</StatusLine>}
            {hasKey === false && (
              <StatusLine state="unset">
                not set — tasks run a scripted demo instead of the real agent
              </StatusLine>
            )}
            <TextSetting
              placeholder="sk-ant-..."
              password
              savedLabel="saved — applies to the next session"
              onSave={async (key) => {
                await setApiKey(key);
                setHasKey(true);
              }}
            />
            <p className="mt-4 text-xs leading-relaxed text-neutral-600">
              get a key from{" "}
              <button
                type="button"
                onClick={() => openUrl("https://console.anthropic.com/settings/keys")}
                className="text-[#4f8dff] hover:underline"
              >
                console.anthropic.com
              </button>
              . stored locally in .env, only ever passed into the background workspace at task time.
            </p>
          </>
        )}
      </Section>

      <Section title="connected_accounts">
        <OAuthAccount
          label="gmail"
          connectLabel="connect gmail"
          clientIdPlaceholder="xxxxxxxxxxxx.apps.googleusercontent.com"
          clientIdHelp={
            <>
              create one in{" "}
              <button
                type="button"
                onClick={() => openUrl("https://console.cloud.google.com/apis/credentials")}
                className="text-[#4f8dff] hover:underline"
              >
                console.cloud.google.com
              </button>{" "}
              → apis &amp; services → credentials → oauth client id, type "desktop app".
            </>
          }
          getClientIdStatus={getGoogleClientIdStatus}
          setClientId={setGoogleClientId}
          getAccount={getGmailAccount}
          connectAccount={connectGmailAccount}
          disconnectAccount={disconnectGmailAccount}
        />
        <OAuthAccount
          label="spotify"
          connectLabel="connect spotify"
          clientIdPlaceholder="spotify client id"
          clientIdHelp={
            <>
              create one in{" "}
              <button
                type="button"
                onClick={() => openUrl("https://developer.spotify.com/dashboard")}
                className="text-[#4f8dff] hover:underline"
              >
                developer.spotify.com/dashboard
              </button>{" "}
              → create app → add this exact redirect URI (spotify requires an exact match, unlike
              google): <code className="text-neutral-400">http://127.0.0.1:38214/callback</code>
            </>
          }
          getClientIdStatus={getSpotifyClientIdStatus}
          setClientId={setSpotifyClientId}
          getAccount={getSpotifyAccount}
          connectAccount={connectSpotifyAccount}
          disconnectAccount={disconnectSpotifyAccount}
        />
      </Section>

      <Section title="browser_login">
        <p className="mt-1 text-xs leading-relaxed text-neutral-500">
          log in once in a real browser window, and daimon's background browser reuses that login
          (cookies, session) for every task after — without this, every task starts logged out.
        </p>
        <SettingRow
          label="websites"
          action={
            loginState === "open" ? (
              <button type="button" onClick={handleFinishLogin} className={BUTTON_CLASS}>
                done
              </button>
            ) : (
              <button
                type="button"
                onClick={handleStartLogin}
                disabled={loginState === "opening" || loginState === "finishing"}
                className={BUTTON_CLASS}
              >
                {loginState === "opening"
                  ? "opening…"
                  : loginState === "finishing"
                    ? "saving…"
                    : loginStatus
                      ? "log in again"
                      : "log in"}
              </button>
            )
          }
        >
          {loginState === "idle" && loginStatus === null && (
            <p className="text-xs text-neutral-500">checking…</p>
          )}
          {loginState === "idle" && loginStatus === false && (
            <p className="text-xs text-neutral-500">○ not logged in anywhere yet</p>
          )}
          {loginState === "idle" && loginStatus === true && (
            <p className="text-xs text-emerald-400">● logged in</p>
          )}
          {loginState === "open" && (
            <p className="text-xs text-[#4f8dff]">
              a real browser window is open — log in, then click done
            </p>
          )}
          {loginState === "error" && <p className="mt-1 text-xs text-red-400">{loginError}</p>}
        </SettingRow>
      </Section>

      <Section title="vault">
        {vaultStatus === null ? (
          <StatusLine state="checking" />
        ) : (
          <StatusLine state="ok">
            using {vaultStatus.isDefault ? "default" : "custom"} vault at {vaultStatus.path}
          </StatusLine>
        )}
        <TextSetting
          placeholder="/Users/you/Documents/my-obsidian-vault"
          onSave={async (path) => {
            await setVaultPath(path);
            await refreshVault();
          }}
        />
        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          the vault is the agent's working directory — everything it reads and writes lives here and
          nowhere else on your disk. notes are searchable context it reads while planning, and
          skills it learns land in <span className="font-mono text-neutral-400">skills/</span>. point
          this at a real obsidian vault and all of it shows up there alongside your own notes.
        </p>
      </Section>

      <Section title="voice">
        {voiceStatus === null && downloadState !== "error" && <StatusLine state="checking" />}
        {voiceStatus?.downloaded && <StatusLine state="ok">{voiceStatus.modelName} downloaded</StatusLine>}
        {voiceStatus && !voiceStatus.downloaded && downloadState !== "downloading" && (
          <StatusLine state="unset">
            {voiceStatus.modelName} not downloaded yet — required before dictation works
          </StatusLine>
        )}
        {downloadState === "downloading" && (
          <p className="mt-1 text-xs text-[#4f8dff]">downloading… this may take a minute</p>
        )}

        {voiceStatus && !voiceStatus.downloaded && (
          <div className="mt-4 flex items-center gap-3">
            <button
              type="button"
              onClick={handleDownloadVoiceModel}
              disabled={downloadState === "downloading"}
              className={BUTTON_CLASS}
            >
              {downloadState === "downloading" ? "downloading…" : "download model"}
            </button>
            {downloadState === "error" && <span className="text-xs text-red-400">{downloadError}</span>}
          </div>
        )}

        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          once downloaded, press <span className="text-neutral-400">⌘⇧D</span> (or ctrl+shift+d)
          anywhere to start dictating — press it again to stop and transcribe. runs fully on-device;
          nothing you say leaves this machine.
        </p>

        <SettingRow
          label="fn key trigger"
          action={
            accessibilityTrusted === false ? (
              <button
                type="button"
                onClick={() =>
                  openUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
                }
                className={BUTTON_CLASS}
              >
                open settings
              </button>
            ) : undefined
          }
        >
          {accessibilityTrusted === null && <p className="mt-0.5 text-xs text-neutral-500">checking…</p>}
          {accessibilityTrusted === true && (
            <p className="mt-0.5 text-xs text-emerald-400">● accessibility permission granted</p>
          )}
          {accessibilityTrusted === false && (
            <p className="mt-0.5 text-xs text-neutral-500">
              ○ accessibility permission required — the fn key won't trigger dictation without it
            </p>
          )}
        </SettingRow>
        <p className="mt-3 text-xs leading-relaxed text-neutral-600">
          the fn key is an additional trigger alongside ⌘⇧D — grant accessibility permission above,
          then restart daimon, and a single press of fn toggles dictation the same way.
        </p>
        <p className="mt-2 text-xs leading-relaxed text-neutral-600">
          macos itself also opens the character viewer on a fn tap by default — daimon can only
          observe the keypress, not suppress that. turn it off in{" "}
          <span className="text-neutral-400">
            system settings → keyboard → "press 🌐 key to" → do nothing
          </span>{" "}
          so fn only triggers dictation.
        </p>
      </Section>
    </div>
  );
}
