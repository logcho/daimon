import { useState } from "react";
import type { Task, StepStatus } from "../types";

const STATUS_LABEL: Record<StepStatus, string> = {
  pending: "Queued",
  running: "Running",
  done: "Done",
  error: "Error",
};

const STATUS_COLOR: Record<StepStatus, string> = {
  pending: "text-neutral-500",
  running: "text-amber-400",
  done: "text-emerald-400",
  error: "text-red-400",
};

export function PipelinePanel({
  task,
  onCollapse,
  onSubmit,
}: {
  task: Task | null;
  onCollapse: () => void;
  onSubmit: (instruction: string) => void;
}) {
  const [draft, setDraft] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const instruction = draft.trim();
    if (!instruction) return;
    onSubmit(instruction);
    setDraft("");
  }

  return (
    <div className="flex h-full w-full flex-col rounded-2xl border border-white/10 bg-neutral-950/95 text-neutral-200 backdrop-blur-xl">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
        <span className="text-sm font-medium text-neutral-100">Daimon</span>
        <button
          onClick={onCollapse}
          className="rounded-md px-2 py-1 text-xs text-neutral-500 hover:bg-white/5 hover:text-neutral-200"
        >
          Collapse
        </button>
      </div>

      <form onSubmit={handleSubmit} className="border-b border-white/10 p-3">
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Tell Daimon what to do..."
          className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500 focus:border-white/20 focus:outline-none"
        />
      </form>

      <div className="flex-1 overflow-y-auto p-3">
        {!task && <p className="text-sm text-neutral-500">No active task. Give Daimon something to do.</p>}
        {task && (
          <div className="space-y-3">
            <p className="text-sm text-neutral-300">{task.instruction}</p>
            <ul className="space-y-2">
              {task.steps.map((step) => (
                <li key={step.id} className="flex items-center justify-between text-sm">
                  <span className="text-neutral-300">{step.label}</span>
                  <span className={STATUS_COLOR[step.status]}>{STATUS_LABEL[step.status]}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
