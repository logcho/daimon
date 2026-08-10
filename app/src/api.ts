import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AgentStatus,
  DictationStatus,
  SkillFile,
  TerminalExitedPayload,
  TerminalOutputPayload,
  VaultFile,
  VoiceModelDownloadPayload,
  VoiceModelStatus,
} from "./types";

export const startChat = (): Promise<string> => invoke<string>("start_chat");

export const sendMessage = (sessionId: string, instruction: string): Promise<void> =>
  invoke<void>("send_message", { sessionId, instruction });

// --- Agent configuration (settings tab) ------------------------------------

/** Per-provider state. The two failure modes are different problems with
 *  different fixes: `installed` is a missing package (`uv sync --extra …`),
 *  `key_configured` is a missing key. */
export interface ProviderState {
  installed: boolean;
  key_configured: boolean;
}

export interface AgentConfig {
  model: string;
  flash_model: string;
  provider: string;
  providers: Record<string, ProviderState>;
  api_base: string;
  api_key_configured: boolean;
  workspace: string;
  vault: string;
  port: number;
  live_frames: boolean;
  reflect: boolean;
  compaction_chars: number;
  temperature: number;
  max_tokens: number;
  pinchtab_base: string;
  pinchtab_healthy: boolean;
}

export const fetchConfig = (): Promise<AgentConfig> =>
  invoke<AgentConfig>("get_config");

/** What a provider offers, and whether it can actually be used. `source` is
 *  "live" when the provider itself was asked, "catalog" when a built-in list
 *  was used instead (no key, or the network was unreachable). */
export interface ProviderModels {
  models: string[];
  source: "live" | "catalog";
  installed: boolean;
  key_configured: boolean;
}

export const listModels = (): Promise<Record<string, ProviderModels>> =>
  invoke<Record<string, ProviderModels>>("list_models");

/** Update config fields. `key` is provider-agnostic — the server routes it by
 *  prefix (sk-ant-… → Anthropic), so the UI never asks which box to use. */
export const updateConfig = (
  fields: { key?: string; model?: string; flash_model?: string },
): Promise<{ ok: boolean }> => invoke<{ ok: boolean }>("update_config", { fields });

export const agentStatus = (): Promise<AgentStatus> => invoke<AgentStatus>("agent_status");

export const closeAgent = (): Promise<void> => invoke<void>("close_agent");

export const setWindowVibrancy = (radius: number): Promise<void> =>
  invoke<void>("set_window_vibrancy", { radius });

// --- Embedded terminals -----------------------------------------------------

export const startTerminal = (id: string): Promise<void> => invoke<void>("start_terminal", { id });

/** Raw bytes as a string — never appends a newline; the caller decides what
 * to send (a staged command must NOT carry a trailing `\r`). */
export const writeToTerminal = (id: string, data: string): Promise<void> =>
  invoke<void>("write_to_terminal", { id, data });

export const resizeTerminal = (id: string, cols: number, rows: number): Promise<void> =>
  invoke<void>("resize_terminal", { id, cols, rows });

export const closeTerminal = (id: string): Promise<void> => invoke<void>("close_terminal", { id });

/** Base64-encoded pty output, matching the Rust side's framing. */
export const onTerminalOutput = (handler: (payload: TerminalOutputPayload) => void): Promise<UnlistenFn> =>
  listen<TerminalOutputPayload>("terminal-output", (e) => handler(e.payload));

export const onTerminalExited = (handler: (payload: TerminalExitedPayload) => void): Promise<UnlistenFn> =>
  listen<TerminalExitedPayload>("terminal-exited", (e) => handler(e.payload));

// --- Voice dictation --------------------------------------------------------

export const voiceModelStatus = (): Promise<VoiceModelStatus> => invoke<VoiceModelStatus>("voice_model_status");

export const downloadVoiceModel = (): Promise<void> => invoke<void>("download_voice_model");

export const startDictation = (): Promise<void> => invoke<void>("start_dictation");

export const stopDictation = (): Promise<void> => invoke<void>("stop_dictation");

export const accessibilityTrusted = (): Promise<boolean> => invoke<boolean>("accessibility_trusted");

export const onDictationStatus = (handler: (status: DictationStatus) => void): Promise<UnlistenFn> =>
  listen<DictationStatus>("dictation-status", (e) => handler(e.payload));

export const onVoiceModelDownload = (handler: (payload: VoiceModelDownloadPayload) => void): Promise<UnlistenFn> =>
  listen<VoiceModelDownloadPayload>("voice-model-download", (e) => handler(e.payload));

// --- Vault -------------------------------------------------------------------

export const listVaultFiles = (): Promise<VaultFile[]> => invoke<VaultFile[]>("list_vault_files");

export const readVaultFile = (name: string): Promise<string> => invoke<string>("read_vault_file", { name });

// --- Skills ------------------------------------------------------------------
// Read straight off the filesystem like the vault, not through the agent
// server — skills are plain files on the host.

export const listSkills = (): Promise<SkillFile[]> => invoke<SkillFile[]>("list_skills");

export const readSkill = (name: string): Promise<string> => invoke<string>("read_skill", { name });
