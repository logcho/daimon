import type { Task } from "../types";

const DOT_COLOR: Record<string, string> = {
  idle: "bg-neutral-500",
  pending: "bg-neutral-500",
  running: "bg-amber-400 animate-pulse",
  done: "bg-emerald-400",
  error: "bg-red-400",
};

function summarize(task: Task | null): { label: string; status: string } {
  if (!task) return { label: "Daimon", status: "idle" };
  if (task.error) return { label: task.error, status: "error" };
  if (task.result) return { label: task.result, status: "done" };
  const activeStep = task.steps.find((s) => s.status !== "done") ?? task.steps[task.steps.length - 1];
  return { label: activeStep?.label ?? task.instruction, status: activeStep?.status ?? "pending" };
}

export function Pill({ task, onExpand }: { task: Task | null; onExpand: () => void }) {
  const { label, status } = summarize(task);

  return (
    <button
      onClick={onExpand}
      className="flex h-full w-full items-center gap-3 rounded-full border border-white/10 bg-neutral-950/90 px-4 text-left backdrop-blur-xl transition hover:bg-neutral-900/90"
    >
      <span className={`h-2 w-2 shrink-0 rounded-full ${DOT_COLOR[status] ?? DOT_COLOR.idle}`} />
      <span className="truncate text-sm text-neutral-200">{label}</span>
    </button>
  );
}
