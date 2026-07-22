import { useEffect, useState } from "react";
import type { Session } from "../types";

const DOT_COLOR: Record<string, string> = {
  idle: "bg-neutral-600",
  pending: "bg-neutral-500",
  running: "bg-[#4f8dff]",
  done: "bg-emerald-400",
  error: "bg-red-400",
};

function summarizeOne(session: Session): { label: string; status: string } {
  const turn = session.turns[session.turns.length - 1];
  if (!turn) return { label: "", status: "pending" };
  if (turn.error) return { label: turn.error, status: "error" };
  if (turn.result) return { label: turn.result, status: "done" };
  const activeStep = turn.steps.find((s) => s.status !== "done") ?? turn.steps[turn.steps.length - 1];
  return { label: activeStep?.label ?? turn.instruction, status: activeStep?.status ?? "pending" };
}

/// "running" if anything is still in flight, else "error" if anything failed,
/// else "done" — worst-status-wins, so the badge never reads calmer than the
/// truth.
function aggregateStatus(sessions: Session[]): string {
  const latest = sessions.map((s) => s.turns[s.turns.length - 1]);
  if (latest.some((t) => t && !t.result && !t.error)) return "running";
  if (latest.some((t) => t && t.error)) return "error";
  return "done";
}

export function Pill({ sessions, onExpand }: { sessions: Session[]; onExpand: () => void }) {
  const [cycleIndex, setCycleIndex] = useState(0);

  // With more than one session there's no single label to show, so cycle the
  // tooltip through them rather than picking one arbitrarily.
  useEffect(() => {
    if (sessions.length < 2) {
      setCycleIndex(0);
      return;
    }
    const interval = setInterval(() => setCycleIndex((i) => (i + 1) % sessions.length), 2600);
    return () => clearInterval(interval);
  }, [sessions.length]);

  const multiple = sessions.length > 1;
  const { label, status } =
    sessions.length === 0
      ? { label: "Daimon — ready when you are", status: "idle" }
      : summarizeOne(sessions[multiple ? cycleIndex % sessions.length : 0]);

  return (
    <div className="flex h-full w-full items-center justify-center p-5">
      <button
        onClick={onExpand}
        title={label}
        className="animate-daimon-in group relative flex h-full w-full items-center justify-center rounded-full border border-white/[0.06] bg-neutral-950/70 opacity-70 shadow-[0_0_24px_-8px_rgba(79,141,255,0.35)] backdrop-blur-2xl transition hover:scale-105 hover:border-white/20 hover:bg-neutral-900/90 hover:opacity-100 active:scale-95"
      >
        <img src="/logo.svg" alt="Daimon" className="h-8 w-8 opacity-70 invert transition group-hover:opacity-90" />
        {multiple ? (
          <span
            className={`absolute right-0.5 top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full border border-neutral-950 px-1 font-mono text-[9px] font-medium text-neutral-950 ${
              DOT_COLOR[aggregateStatus(sessions)] ?? DOT_COLOR.idle
            } ${aggregateStatus(sessions) === "running" ? "animate-daimon-pulse" : ""}`}
          >
            {sessions.length}
          </span>
        ) : (
          <span
            className={`absolute right-1 top-1 h-2.5 w-2.5 rounded-full border border-neutral-950 ${
              DOT_COLOR[status] ?? DOT_COLOR.idle
            } ${status === "running" ? "animate-daimon-pulse" : ""}`}
          />
        )}
      </button>
    </div>
  );
}
