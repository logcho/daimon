import { useEffect, useRef, useState } from "react";
import type { Task, StepStatus } from "../types";

function StepIcon({ status }: { status: StepStatus }) {
  if (status === "done") {
    return <span className="text-sm text-emerald-400">✓</span>;
  }
  if (status === "error") {
    return <span className="text-sm text-red-400">✕</span>;
  }
  if (status === "running") {
    return (
      <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
    );
  }
  return <span className="block h-2 w-2 rounded-full border border-neutral-600" />;
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
          <span className="text-sm font-medium text-neutral-100">Daimon</span>
          <div className="flex-1" />
          <button
            onClick={onCollapse}
            className="rounded-md px-2 py-1 text-xs text-neutral-500 transition hover:bg-white/5 hover:text-neutral-200 active:scale-90"
          >
            Collapse
          </button>
        </div>

        <div ref={historyRef} className="themed-scroll flex-1 space-y-3 overflow-y-auto p-4">
          {!task && (
            <p className="text-sm text-neutral-500">No active task. Give Daimon something to do.</p>
          )}
          {task && (
            <>
              <div className="flex justify-end">
                <div className="max-w-[85%] rounded-2xl rounded-br-sm border border-[#4f8dff]/20 bg-[#4f8dff]/15 px-3 py-2 text-sm text-neutral-100">
                  {task.instruction}
                </div>
              </div>

              <div className="flex justify-start">
                <div className="max-w-[85%] space-y-2 rounded-2xl rounded-bl-sm border border-white/10 bg-white/5 px-3 py-2">
                  <ul className="space-y-1.5">
                    {task.steps.map((step) => (
                      <li key={step.id} className="flex items-center gap-2 text-sm">
                        <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                          <StepIcon status={step.status} />
                        </span>
                        <span className="text-neutral-300">{step.label}</span>
                      </li>
                    ))}
                  </ul>
                  {task.result && <p className="text-sm text-emerald-400">{task.result}</p>}
                  {task.error && <p className="text-sm text-red-400">{task.error}</p>}
                </div>
              </div>
            </>
          )}
        </div>

        <form onSubmit={handleSubmit} className="border-t border-white/10 p-3">
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Tell Daimon what to do..."
            className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500 transition focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
          />
        </form>
      </div>
    </div>
  );
}
