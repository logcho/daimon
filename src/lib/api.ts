import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { StepStatus } from "../types";

export type WorkspaceEvent =
  | { type: "step"; id: string; label: string; status: StepStatus }
  | { type: "done"; result: string }
  | { type: "error"; message: string };

interface TaskStatusPayload {
  task_id: string;
  event: WorkspaceEvent;
}

export function startTask(instruction: string): Promise<string> {
  return invoke<string>("start_task", { instruction });
}

export function onTaskStatus(taskId: string, handler: (event: WorkspaceEvent) => void): Promise<UnlistenFn> {
  return listen<TaskStatusPayload>("task-status", (e) => {
    if (e.payload.task_id === taskId) handler(e.payload.event);
  });
}
