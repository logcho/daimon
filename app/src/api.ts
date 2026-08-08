import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AgentStatus,
  DictationStatus,
  TerminalExitedPayload,
  TerminalOutputPayload,
  VoiceModelDownloadPayload,
  VoiceModelStatus,
} from "./types";

export const startChat = (): Promise<string> => invoke<string>("start_chat");

export const sendMessage = (sessionId: string, instruction: string): Promise<void> =>
  invoke<void>("send_message", { sessionId, instruction });

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
