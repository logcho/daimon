import { useRef } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { DictationStatus } from "../types";
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

interface Props {
  /** A turn is in flight — the dot pulses the accent color. */
  busy: boolean;
  /** The agent server isn't running (offline dot). */
  agentDown: boolean;
  /** The last turn errored — red dot. */
  hasError: boolean;
  /** A turn finished while the panel was collapsed — green dot. */
  unreadCompletion?: boolean;
  onExpand: () => void;
  /** Voice dictation state — driven by Rust-side Fn-key events. */
  dictation?: DictationStatus;
}

export function Pill({ busy, agentDown, hasError, unreadCompletion, onExpand, dictation }: Props) {
  const mouseDownAt = useRef<{ x: number; y: number } | null>(null);

  const status = busy
    ? "running"
    : hasError
      ? "error"
      : agentDown
        ? "down"
        : unreadCompletion
          ? "done"
          : "idle";
  const dotClass =
    status === "running"
      ? "bg-[#4f8dff] animate-daimon-pulse"
      : DOT_COLOR[status] ?? DOT_COLOR.idle;
  const label =
    status === "running"
      ? "Daimon — thinking…"
      : status === "error"
        ? "Daimon — something went wrong"
        : status === "done"
          ? "Daimon — turn complete"
          : "Daimon — ready when you are";

  // Dictation can be active while the pill is collapsed — take over the
  // icon area and add border glow distinct from the existing session-status
  // dot (opposite corner).
  const dictationActive =
    dictation?.type === "recording" || dictation?.type === "transcribing";
  const dictationLabel =
    dictation?.type === "recording"
      ? dictation.locked
        ? "listening (hands-free — press fn to stop)…"
        : "listening…"
      : dictation?.type === "transcribing"
        ? "transcribing…"
        : dictation?.type === "error"
          ? dictation.message || "dictation error"
          : null;

  return (
    // No padding wrapper — the button fills the window so the visible
    // circle aligns exactly with the native vibrancy material behind it
    // (which fills the whole window). See vibrancy.rs and window.ts's
    // PILL_SIZE note.
    <div className="flex h-full w-full items-center justify-center">
      <button
        onClick={(e) => {
          const start = mouseDownAt.current;
          const moved =
            start &&
            Math.hypot(e.screenX - start.x, e.screenY - start.y) >
              DRAG_CLICK_SUPPRESS_THRESHOLD;
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
        // No grow-on-hover (hover:scale-105): the button fills the window
        // edge-to-edge, so scaling up would clip against the window bounds.
        // active:scale-95 (a shrink) is fine and stays. Outer-glow shadows
        // are gone too (no transparent margin left to fade into); the
        // dictation states are signaled by border color + opacity instead.
        className={`animate-daimon-in liquid-glass group relative flex h-full w-full items-center justify-center rounded-full transition duration-300 active:scale-95 ${
          dictation?.type === "error"
            ? "opacity-100 [border-color:rgba(248,113,113,0.55)]"
            : dictationActive
              ? "opacity-100 [border-color:rgba(79,141,255,0.6)]"
              : "opacity-90 hover:opacity-100 hover:[border-color:rgba(255,255,255,0.22)]"
        }`}
      >
        {dictation?.type === "recording" ? (
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
            {dictation.locked && (
              <div className="flex items-center gap-1">
                <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
                <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
              </div>
            )}
          </div>
        ) : dictation?.type === "transcribing" ? (
          <span className="block h-5 w-5 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        ) : (
          <img
            src="/logo.svg"
            alt="Daimon"
            draggable={false}
            className="h-8 w-8 opacity-70 transition group-hover:opacity-90"
          />
        )}
        <span
          className={`absolute right-1 top-1 h-2.5 w-2.5 rounded-full ${dotClass}`}
        />
        {dictation?.type === "error" && (
          // Recording/transcribing already have their own unmistakable
          // icon-area visual (the wave / spinner above) — this small corner
          // dot is only still needed for the error state, which doesn't
          // replace the logo.
          <span className="absolute left-0.5 top-0.5 h-2.5 w-2.5 rounded-full bg-red-400" />
        )}
      </button>
    </div>
  );
}
