import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  Automation,
  ConnectedAccount,
  DictationEvent,
  RecordingFile,
  StepStatus,
  VaultFile,
  VaultPathStatus,
  VoiceModelStatus,
} from "../types";

export type WorkspaceEvent =
  | { type: "step"; id: string; label: string; status: StepStatus; tool?: string }
  | { type: "done"; result: string }
  | { type: "error"; message: string }
  | { type: "ui_action"; action: "open_terminal_with_command"; command: string }
  // A base64-encoded PNG screenshot of the background browser, emitted
  // periodically while a turn is running — see agents/src/run.ts's
  // LIVE_FRAME_INTERVAL_MS. Powers the "screen" tab's live view.
  | { type: "live_frame"; data: string };

interface SessionStatusPayload {
  session_id: string;
  event: WorkspaceEvent;
}

export function startSession(instruction: string): Promise<string> {
  return invoke<string>("start_session", { instruction });
}

export function sendMessage(sessionId: string, instruction: string): Promise<void> {
  return invoke<void>("send_message", { sessionId, instruction });
}

export function endSession(sessionId: string): Promise<void> {
  return invoke<void>("end_session", { sessionId });
}

/// One subscription routes every running session's events, keyed by the
/// session id on each payload — a per-session listener would have to be torn
/// down and re-subscribed on every new `startSession`/`sendMessage`, which
/// would silently orphan any session still in flight when the next one
/// starts.
export function onSessionStatus(handler: (sessionId: string, event: WorkspaceEvent) => void): Promise<UnlistenFn> {
  return listen<SessionStatusPayload>("session-status", (e) => handler(e.payload.session_id, e.payload.event));
}

export function getApiKeyStatus(): Promise<boolean> {
  return invoke<boolean>("get_api_key_status");
}

export function setApiKey(key: string): Promise<void> {
  return invoke<void>("set_api_key", { key });
}

export function getGoogleClientIdStatus(): Promise<boolean> {
  return invoke<boolean>("get_google_client_id_status");
}

export function setGoogleClientId(clientId: string): Promise<void> {
  return invoke<void>("set_google_client_id", { clientId });
}

export function connectGmailAccount(): Promise<ConnectedAccount> {
  return invoke<ConnectedAccount>("connect_gmail_account");
}

export function getGmailAccount(): Promise<ConnectedAccount | null> {
  return invoke<ConnectedAccount | null>("get_gmail_account");
}

export function disconnectGmailAccount(): Promise<void> {
  return invoke<void>("disconnect_gmail_account");
}

export function getVaultPathStatus(): Promise<VaultPathStatus> {
  return invoke<VaultPathStatus>("get_vault_path_status");
}

export function setVaultPath(path: string): Promise<void> {
  return invoke<void>("set_vault_path", { path });
}

export function listVaultFiles(): Promise<VaultFile[]> {
  return invoke<VaultFile[]>("list_vault_files");
}

export function readVaultFile(name: string): Promise<string> {
  return invoke<string>("read_vault_file", { name });
}

export function listRecordings(): Promise<RecordingFile[]> {
  return invoke<RecordingFile[]>("list_recordings");
}

// Returns the video's raw bytes, base64-encoded — decode to a Uint8Array and
// wrap in a Blob for a <video> element's src, same "just base64 it over IPC"
// approach already used for the terminal's PTY output.
export function readRecordingFile(name: string): Promise<string> {
  return invoke<string>("read_recording_file", { name });
}

export function listAutomations(): Promise<Automation[]> {
  return invoke<Automation[]>("list_automations");
}

export function createAutomation(name: string, instruction: string, schedule: string): Promise<Automation> {
  return invoke<Automation>("create_automation", { name, instruction, schedule });
}

export function setAutomationEnabled(id: string, enabled: boolean): Promise<void> {
  return invoke<void>("set_automation_enabled", { id, enabled });
}

export function deleteAutomation(id: string): Promise<void> {
  return invoke<void>("delete_automation", { id });
}

export function getVoiceModelStatus(): Promise<VoiceModelStatus> {
  return invoke<VoiceModelStatus>("get_voice_model_status");
}

// Downloads a real (large) model file — can take a while. The promise only
// resolves once the download is fully complete; callers should show an
// indefinite in-progress state rather than assuming this returns quickly.
export function downloadVoiceModel(): Promise<void> {
  return invoke<void>("download_voice_model");
}

// Resolves once the toggle itself is kicked off (recording started, or
// recording stopped and transcription queued) — not once transcription
// finishes. The actual "recording" / "transcribing" / "result" / "error"
// progression arrives asynchronously via onDictationStatus below.
export function toggleDictation(): Promise<void> {
  return invoke<void>("toggle_dictation");
}

export function onDictationStatus(handler: (event: DictationEvent) => void): Promise<UnlistenFn> {
  return listen<DictationEvent>("dictation-status", (e) => handler(e.payload));
}

// Whether this process currently holds macOS Accessibility trust — a
// prerequisite for the global Fn-key dictation trigger to receive any
// events at all. Doesn't trigger the system permission prompt; Settings
// pairs this with a button that deep-links to System Settings instead.
export function getAccessibilityTrustStatus(): Promise<boolean> {
  return invoke<boolean>("get_accessibility_trust_status");
}

// `id` identifies one terminal tab (the frontend mints one per open tab,
// mirroring how session ids already work for chat) — Daimon supports
// multiple concurrent terminals, each its own real shell process. Idempotent
// per id — spawns that id's host shell PTY only if one isn't already
// running, a no-op otherwise. Call once when that terminal tab first mounts;
// safe to call again on a "process exited" restart since the previous PTY
// for that id is already gone by then.
export function startTerminal(id: string): Promise<void> {
  return invoke<void>("start_terminal", { id });
}

// Raw bytes straight to the given terminal's PTY, no newline appended — used
// for both live keystrokes (xterm's onData) and routing a dictated result
// into the terminal without auto-submitting it on the user's behalf.
export function writeToTerminal(id: string, data: string): Promise<void> {
  return invoke<void>("write_to_terminal", { id, data });
}

// Tells the given terminal's PTY the new visible grid size — call after
// every fit-addon resize (panel expand/collapse, manual resize-drag, tab
// becoming visible again), not just on window resize, since none of those
// go through a window-level resize event.
export function resizeTerminal(id: string, cols: number, rows: number): Promise<void> {
  return invoke<void>("resize_terminal", { id, cols, rows });
}

// Explicitly ends one terminal tab's shell process — call when the user
// closes that tab (its "×"), not just when the whole app quits, now that
// terminals are multiple/independent rather than a single app-lifetime
// singleton.
export function closeTerminal(id: string): Promise<void> {
  return invoke<void>("close_terminal", { id });
}

// Whether the `claude` CLI actually resolves on PATH — informational only
// (a Settings hint), never a gate on opening a terminal tab itself.
export function getClaudeCliStatus(): Promise<boolean> {
  return invoke<boolean>("get_claude_cli_status");
}

// One subscription routes every open terminal's output, keyed by the `id` on
// each payload — same reasoning as `onSessionStatus` above, so a new
// terminal tab doesn't need its own fresh listener torn down/re-subscribed
// on every open/close. `data` arrives base64-encoded on the wire
// specifically so a multi-byte UTF-8 sequence split across two PTY read
// chunks doesn't get corrupted by a lossy per-chunk string conversion on the
// Rust side — decode it to bytes and feed those straight to xterm.js, which
// has its own streaming ANSI/UTF-8 parser built to handle exactly this kind
// of cross-chunk split.
export function onTerminalOutput(handler: (id: string, dataBase64: string) => void): Promise<UnlistenFn> {
  return listen<{ id: string; data: string }>("terminal-output", (e) => handler(e.payload.id, e.payload.data));
}

export function onTerminalExited(handler: (id: string, code: number | null) => void): Promise<UnlistenFn> {
  return listen<{ id: string; code: number | null }>("terminal-exited", (e) => handler(e.payload.id, e.payload.code));
}

// Forces real OS-level keyboard focus onto Daimon's pill/panel window — see
// `src-tauri/src/window_focus.rs`'s module doc for why the plain
// `getCurrentWindow().setFocus()` JS API alone isn't reliable while Daimon
// runs with `ActivationPolicy::Accessory` (no Dock icon/Cmd+Tab entry) on
// macOS. Use this instead of (or in addition to) `setFocus()` wherever a
// programmatic path needs the very next keystroke to actually reach Daimon.
export function activateAndFocusWindow(): Promise<void> {
  return invoke<void>("activate_and_focus_window");
}

// Applies the native macOS vibrancy material (the real, dynamic desktop-blur
// "liquid glass") behind the window with the given corner radius — see
// `src-tauri/src/vibrancy.rs`. 28px is a circle at the collapsed pill size and
// the panel's rounded corner when expanded, so one value serves both states.
// Called once on mount; a no-op on non-macOS platforms.
export function setWindowVibrancy(radius: number): Promise<void> {
  return invoke<void>("set_window_vibrancy", { radius });
}
