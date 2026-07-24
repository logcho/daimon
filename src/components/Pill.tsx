import { useEffect, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { DictationState, Session } from "../types";
import { SoundWave } from "./SoundWave";

// Beyond this many screen pixels of movement between mousedown and the
// resulting click, treat it as a drag (the user repositioning the pill) —
// not a tap — and don't expand. Without this, `startDragging()` still lets
// a plain click through afterward (that's what makes click-to-expand keep
// working at all), but a real drag-to-move ends in a mouseup on the same
// element too, which would otherwise *also* fire onClick and expand the
// window right after the user just finished moving it.
const DRAG_CLICK_SUPPRESS_THRESHOLD = 4;

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

export function Pill({
  sessions,
  onExpand,
  dictationState = "idle",
  dictationLocked = false,
  dictationMessage,
}: {
  sessions: Session[];
  onExpand: () => void;
  dictationState?: DictationState;
  /** Whether a double-tap has engaged hands-free recording — the next Fn
   * press stops it, rather than needing to hold the key down. Only
   * meaningful while `dictationState === "recording"`. */
  dictationLocked?: boolean;
  dictationMessage?: string;
}) {
  const [cycleIndex, setCycleIndex] = useState(0);
  const mouseDownAt = useRef<{ x: number; y: number } | null>(null);

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

  // Dictation can be toggled while the pill is collapsed (the whole point of
  // a hotkey-triggered feature), so it needs its own always-visible affordance
  // here rather than only showing up once the panel is expanded — a
  // recording session with zero feedback on the pill itself would be silent
  // and confusing. Takes over the tooltip and adds a glow + corner badge
  // distinct from the existing session-status dot (opposite corner).
  const dictationActive = dictationState === "recording" || dictationState === "transcribing";
  const dictationLabel =
    dictationState === "recording"
      ? dictationLocked
        ? "listening (hands-free — press fn to stop)…"
        : "listening…"
      : dictationState === "transcribing"
      ? "transcribing…"
      : dictationState === "error"
      ? dictationMessage || "dictation error"
      : null;

  return (
    <div className="flex h-full w-full items-center justify-center p-5">
      <button
        onClick={(e) => {
          const start = mouseDownAt.current;
          const moved = start && Math.hypot(e.screenX - start.x, e.screenY - start.y) > DRAG_CLICK_SUPPRESS_THRESHOLD;
          if (moved) return;
          onExpand();
        }}
        onMouseDown={(e) => {
          // Lets the pill double as its own drag handle — a plain click
          // (no movement between mousedown and mouseup) still expands
          // normally (see onClick above), so this only adds drag-to-move on
          // top of that, it doesn't replace clicking.
          mouseDownAt.current = { x: e.screenX, y: e.screenY };
          getCurrentWindow().startDragging();
        }}
        title={dictationLabel ?? label}
        className={`animate-daimon-in group relative flex h-full w-full items-center justify-center rounded-full backdrop-blur-2xl transition hover:scale-105 active:scale-95 ${
          dictationState === "error"
            ? "border border-red-400/40 bg-neutral-950/70 opacity-100 shadow-[0_0_24px_-8px_rgba(248,113,113,0.45)]"
            : dictationActive
            ? "border border-[#4f8dff]/50 bg-neutral-900/90 opacity-100 shadow-[0_0_28px_-6px_rgba(79,141,255,0.55)]"
            : "border border-white/[0.06] bg-neutral-950/70 opacity-70 shadow-[0_0_24px_-8px_rgba(79,141,255,0.35)] hover:border-white/20 hover:bg-neutral-900/90 hover:opacity-100"
        }`}
      >
        {dictationState === "recording" ? (
          // Replaces the logo entirely while actively listening — a small
          // corner dot is easy to miss, especially on a widget this size,
          // and "is it actually listening right now" is exactly the question
          // this needs to answer unambiguously. When locked (double-tap
          // engaged hands-free mode), two small static dots appear beneath
          // the wave — a visibly different, steadier mark than the moving
          // wave alone, signaling "this keeps going until you press fn
          // again," not "only while held."
          <div className="flex flex-col items-center gap-1">
            <SoundWave />
            {dictationLocked && (
              <div className="flex items-center gap-1">
                <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
                <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
              </div>
            )}
          </div>
        ) : dictationState === "transcribing" ? (
          <span className="block h-5 w-5 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        ) : (
          <img
            src="/logo.svg"
            alt="Daimon"
            draggable={false}
            className="h-8 w-8 opacity-70 invert transition group-hover:opacity-90"
          />
        )}
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
        {dictationState === "error" && (
          // Recording/transcribing already have their own unmistakable
          // icon-area visual (the wave / spinner above) — this small corner
          // dot is only still needed for the error state, which doesn't
          // replace the logo.
          <span className="absolute left-0.5 top-0.5 h-2.5 w-2.5 rounded-full border border-neutral-950 bg-red-400" />
        )}
      </button>
    </div>
  );
}
