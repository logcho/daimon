import { useCallback, useEffect, useRef, useState } from "react";
import { Pill } from "./components/Pill";
import { PipelinePanel } from "./components/PipelinePanel";
import { useHotkeyToggle } from "./hooks/useHotkeyToggle";
import { collapseToPill, expandToPanel } from "./lib/window";
import { onTaskStatus, startTask } from "./lib/api";
import type { Task, TaskStep } from "./types";
import type { UnlistenFn } from "@tauri-apps/api/event";

function App() {
  const [expanded, setExpanded] = useState(false);
  const [task, setTask] = useState<Task | null>(null);
  const unlistenRef = useRef<UnlistenFn | null>(null);

  useEffect(() => {
    collapseToPill();
    return () => unlistenRef.current?.();
  }, []);

  const expand = useCallback(() => {
    setExpanded(true);
    expandToPanel();
  }, []);

  const collapse = useCallback(() => {
    setExpanded(false);
    collapseToPill();
  }, []);

  const toggle = useCallback(() => {
    setExpanded((prev) => {
      const next = !prev;
      if (next) expandToPanel();
      else collapseToPill();
      return next;
    });
  }, []);

  useHotkeyToggle(toggle);

  const submitInstruction = useCallback(async (instruction: string) => {
    unlistenRef.current?.();
    unlistenRef.current = null;

    const taskId = await startTask(instruction);
    setTask({ id: taskId, instruction, steps: [] });

    unlistenRef.current = await onTaskStatus(taskId, (event) => {
      setTask((prev) => {
        if (!prev || prev.id !== taskId) return prev;

        if (event.type === "step") {
          const steps = [...prev.steps];
          const index = steps.findIndex((s) => s.id === event.id);
          const step: TaskStep = { id: event.id, label: event.label, status: event.status };
          if (index >= 0) steps[index] = step;
          else steps.push(step);
          return { ...prev, steps };
        }
        if (event.type === "done") return { ...prev, result: event.result };
        return { ...prev, error: event.message };
      });

      if (event.type === "done" || event.type === "error") {
        unlistenRef.current?.();
        unlistenRef.current = null;
      }
    });
  }, []);

  return (
    <main className="h-full w-full bg-transparent p-0">
      {expanded ? (
        <PipelinePanel task={task} onCollapse={collapse} onSubmit={submitInstruction} />
      ) : (
        <Pill task={task} onExpand={expand} />
      )}
    </main>
  );
}

export default App;
