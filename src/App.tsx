import { useCallback, useState } from "react";
import { Pill } from "./components/Pill";
import { PipelinePanel } from "./components/PipelinePanel";
import { useHotkeyToggle } from "./hooks/useHotkeyToggle";
import { collapseToPill, expandToPanel } from "./lib/window";
import type { Task } from "./types";

function App() {
  const [expanded, setExpanded] = useState(false);
  const [task, setTask] = useState<Task | null>(null);

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

  const submitInstruction = useCallback((instruction: string) => {
    setTask({
      id: crypto.randomUUID(),
      instruction,
      steps: [{ id: crypto.randomUUID(), label: "Waiting for background workspace", status: "pending" }],
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
