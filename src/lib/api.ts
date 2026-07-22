import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { Automation, ConnectedAccount, StepStatus, VaultFile, VaultPathStatus } from "../types";

export type WorkspaceEvent =
  | { type: "step"; id: string; label: string; status: StepStatus; tool?: string }
  | { type: "done"; result: string }
  | { type: "error"; message: string };

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
