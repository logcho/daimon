import { useEffect, useRef, useState } from "react";
import type { Task, TaskStep } from "../types";
import { ThinkingIndicator } from "./ThinkingIndicator";

function StepLine({ step }: { step: TaskStep }) {
  if (step.label === "Thinking" && step.status === "running") {
    return <ThinkingIndicator />;
  }

  const name = step.tool ?? step.label;

  if (step.status === "done") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="text-emerald-400">✓</span>
        <span className="text-neutral-400">{name}</span>
      </span>
    );
  }
  if (step.status === "error") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="text-red-400">✕</span>
        <span className="text-neutral-400">{name}</span>
      </span>
    );
  }
  if (step.status === "running") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        <span className="text-[#4f8dff]">{name}</span>
      </span>
    );
  }
  return (
    <span className="flex items-center gap-2 font-mono text-sm">
      <span className="block h-2 w-2 rounded-full border border-neutral-600" />
      <span className="text-neutral-500">{name}</span>
    </span>
  );
}

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
  const historyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    historyRef.current?.scrollTo({ top: historyRef.current.scrollHeight, behavior: "smooth" });
  }, [task?.steps.length, task?.result, task?.error]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const instruction = draft.trim();
    if (!instruction) return;
    onSubmit(instruction);
    setDraft("");
  }

  return (
    <div className="flex h-full w-full items-center justify-center p-6">
      <div className="animate-daimon-in flex h-full w-full flex-col rounded-[28px] border border-white/10 bg-neutral-950/95 text-neutral-200 shadow-[0_0_40px_-12px_rgba(79,141,255,0.25)] backdrop-blur-2xl">
        <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
          <img src="/logo.svg" alt="" className="h-5 w-5 invert" />
          <span className="font-mono text-sm font-medium text-neutral-100">daimon</span>
          <span className="h-3.5 w-1.5 animate-pulse bg-[#4f8dff]/70" />
          <div className="flex-1" />
          <button
            onClick={onCollapse}
            className="rounded-md px-2 py-1 font-mono text-xs text-neutral-500 transition hover:bg-white/5 hover:text-neutral-200 active:scale-90"
          >
            collapse
          </button>
        </div>

        <div ref={historyRef} className="themed-scroll flex-1 space-y-3 overflow-y-auto p-4">
          {!task && (
            <p className="font-mono text-sm text-neutral-500">no active task — give daimon something to do.</p>
          )}
          {task && (
            <>
              <div className="flex justify-end">
                <div className="max-w-[85%] rounded-2xl rounded-br-sm border border-[#4f8dff]/20 bg-[#4f8dff]/15 px-3 py-2 font-mono text-sm text-neutral-100">
                  <span className="text-[#4f8dff]">{">"}</span> {task.instruction}
                </div>
              </div>

              <div className="flex justify-start">
                <div className="max-w-[85%] space-y-2 rounded-2xl rounded-bl-sm border border-white/10 bg-white/5 px-3 py-2">
                  <ul className="space-y-1.5">
                    {task.steps.map((step) => (
                      <li key={step.id}>
                        <StepLine step={step} />
                      </li>
                    ))}
                  </ul>
                  {task.result && <p className="font-mono text-sm text-emerald-400">{task.result}</p>}
                  {task.error && <p className="font-mono text-sm text-red-400">{task.error}</p>}
                </div>
              </div>
            </>
          )}
        </div>

        <form onSubmit={handleSubmit} className="flex items-center gap-2 border-t border-white/10 p-3">
          <span className="pl-1 font-mono text-sm text-[#4f8dff]">{">"}</span>
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="tell daimon what to do..."
            className="w-full bg-transparent font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:outline-none"
          />
        </form>
      </div>
    </div>
  );
}
