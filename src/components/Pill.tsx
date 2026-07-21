import type { Task } from "../types";

const DOT_COLOR: Record<string, string> = {
  idle: "bg-neutral-600",
  pending: "bg-neutral-500",
  running: "bg-[#4f8dff]",
  done: "bg-emerald-400",
  error: "bg-red-400",
};

function summarize(task: Task | null): { label: string; status: string } {
  if (!task) return { label: "Daimon — ready when you are", status: "idle" };
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
      title={label}
      className="animate-daimon-in relative flex h-full w-full items-center justify-center rounded-full border border-white/10 bg-neutral-950/85 shadow-[0_0_24px_-8px_rgba(79,141,255,0.35)] backdrop-blur-2xl transition hover:border-white/20 hover:bg-neutral-900/90"
    >
      <img src="/logo.svg" alt="Daimon" className="h-8 w-8 opacity-90" />
      <span
        className={`absolute right-1 top-1 h-2.5 w-2.5 rounded-full border border-neutral-950 ${
          DOT_COLOR[status] ?? DOT_COLOR.idle
        } ${status === "running" ? "animate-daimon-pulse" : ""}`}
      />
    </button>
  );
}
