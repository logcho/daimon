import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AgentStatus,
  DictationStatus,
  SkillFile,
  VaultFile,
  VoiceModelDownloadPayload,
  VoiceModelStatus,
} from "./types";

export const startChat = (): Promise<string> => invoke<string>("start_chat");

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
  /** Denominator for the status bar's ctx% — reported so both surfaces
   *  divide by the same number. */
  context_window: number;
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

// --- Voice dictation --------------------------------------------------------

export const voiceModelStatus = (): Promise<VoiceModelStatus> => invoke<VoiceModelStatus>("voice_model_status");

export const downloadVoiceModel = (): Promise<void> => invoke<void>("download_voice_model");

export const startDictation = (): Promise<void> => invoke<void>("start_dictation");

export const stopDictation = (): Promise<void> => invoke<void>("stop_dictation");

export const accessibilityTrusted = (): Promise<boolean> => invoke<boolean>("accessibility_trusted");

export const onDictationStatus = (handler: (status: DictationStatus) => void): Promise<UnlistenFn> =>
  listen<DictationStatus>("dictation-status", (e) => handler(e.payload));

// Rust asking the UI to do something to itself. Today the only action is
// "expand" (a tap of Fn while collapsed — see fn_key.rs); the payload is an
// object rather than a bare string so adding a second action doesn't break the
// channel's shape.
export const onUiCommand = (handler: (action: string) => void): Promise<UnlistenFn> =>
  listen<{ action: string }>("ui-command", (e) => handler(e.payload.action));

export const onVoiceModelDownload = (handler: (payload: VoiceModelDownloadPayload) => void): Promise<UnlistenFn> =>
  listen<VoiceModelDownloadPayload>("voice-model-download", (e) => handler(e.payload));

// --- Vault -------------------------------------------------------------------

// Through the agent server, which owns the vault. The app used to resolve the
// path itself and listed an entirely different directory.

export const listVaultFiles = (): Promise<VaultFile[]> => invoke<VaultFile[]>("list_notes");

export const readVaultFile = (name: string): Promise<string> =>
  invoke<{ content: string }>("read_note", { name }).then((n) => n.content);

export const deleteVaultFile = (name: string): Promise<{ ok: boolean }> =>
  invoke<{ ok: boolean }>("delete_note", { name });

/** Create or overwrite a note. One call for both — a new note is just a write
 *  to a name that doesn't exist yet. Returns the note's fresh size/mtime so a
 *  caller can update its listing without refetching everything. */
export const writeVaultFile = (name: string, content: string): Promise<VaultFile> =>
  invoke<VaultFile>("write_note", { name, content });

/** Folders, including empty ones — the note listing can't show a folder you
 *  just created and haven't written into yet. */
export const listVaultFolders = (): Promise<string[]> => invoke<string[]>("list_folders");

export const createVaultFolder = (path: string): Promise<{ ok: boolean; path: string }> =>
  invoke<{ ok: boolean; path: string }>("create_folder", { path });

/** `recursive` is required by the server once the folder holds notes, so the
 *  caller has to have decided about those notes before it can succeed. */
export const deleteVaultFolder = (
  path: string,
  recursive: boolean,
): Promise<{ ok: boolean; notes: number }> =>
  invoke<{ ok: boolean; notes: number }>("delete_folder", { path, recursive });

/** Rename a note, or move it into another folder — the same call either way. */
export const moveVaultFile = (from: string, to: string): Promise<VaultFile> =>
  invoke<VaultFile>("move_note", { from, to });

/** Move a whole folder (with everything under it). Same endpoint as a note
 *  move — the server tells them apart by what's on disk — but it answers with
 *  the new path and a note count rather than one file's metadata. */
export const moveVaultFolder = (
  from: string,
  to: string,
): Promise<{ ok: boolean; path: string; notes: number }> =>
  invoke<{ ok: boolean; path: string; notes: number }>("move_note", { from, to });

// --- Skills ------------------------------------------------------------------

/** Both from the agent server, which owns where skills live — the app used to
 *  resolve the path itself and looked in a different directory entirely. */
export const listSkills = (): Promise<SkillFile[]> => invoke<SkillFile[]>("list_skills");

export interface SkillDetail extends SkillFile {
  path: string;
  content: string;
}

export const readSkill = (name: string): Promise<SkillDetail> =>
  invoke<SkillDetail>("read_skill", { name });

export const deleteSkill = (name: string): Promise<{ ok: boolean }> =>
  invoke<{ ok: boolean }>("delete_skill", { name });
