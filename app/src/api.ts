import { invoke } from "@tauri-apps/api/core";
import type { AgentStatus } from "./types";

export const startChat = (): Promise<string> => invoke<string>("start_chat");

export const sendMessage = (sessionId: string, instruction: string): Promise<void> =>
  invoke<void>("send_message", { sessionId, instruction });

export const agentStatus = (): Promise<AgentStatus> => invoke<AgentStatus>("agent_status");

export const closeAgent = (): Promise<void> => invoke<void>("close_agent");
