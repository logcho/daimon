import { useRef } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";

// Beyond this many screen pixels of movement between mousedown and the
// resulting click, treat it as a drag (the user repositioning the pill) —
// not a tap — and don't expand. Without this, `startDragging()` still lets
// a plain click through afterward (that's what makes click-to-expand keep
// working at all), but a real drag-to-move ends in a mouseup on the same
// element too, which would otherwise *also* fire onClick and expand the
// window right after the user just finished moving it.
const DRAG_CLICK_SUPPRESS_THRESHOLD = 4;

interface Props {
  /** A turn is in flight — the dot pulses the accent color. */
  busy: boolean;
  /** The agent server isn't running (offline dot). */
  agentDown: boolean;
  /** The last turn errored — red dot. */
  hasError: boolean;
  onExpand: () => void;
}

export function Pill({ busy, agentDown, hasError, onExpand }: Props) {
  const mouseDownAt = useRef<{ x: number; y: number } | null>(null);

  const status = busy ? "running" : hasError ? "error" : agentDown ? "down" : "idle";
  const dotClass =
    status === "running" ? "bg-[#4f8dff] animate-daimon-pulse" : status === "error" ? "bg-red-400" : "bg-neutral-600";
  const label =
    status === "running" ? "Daimon — thinking…" : status === "error" ? "Daimon — something went wrong" : "Daimon — ready when you are";

  return (
    // No padding wrapper — the button fills the window so the visible
    // circle aligns exactly with the native vibrancy material behind it
    // (which fills the whole window). See vibrancy.rs and window.ts's
    // PILL_SIZE note.
    <div className="flex h-full w-full items-center justify-center">
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
        title={label}
        // No grow-on-hover (hover:scale-105): the button fills the window
        // edge-to-edge, so scaling up would clip against the window bounds.
        // active:scale-95 (a shrink) is fine and stays. Outer-glow shadows
        // are gone too (no transparent margin left to fade into).
        className="animate-daimon-in liquid-glass group relative flex h-full w-full items-center justify-center rounded-full transition duration-300 active:scale-95 opacity-90 hover:opacity-100 hover:[border-color:rgba(255,255,255,0.22)]"
      >
        <img
          src="/logo.svg"
          alt="Daimon"
          draggable={false}
          className="h-8 w-8 opacity-70 invert transition group-hover:opacity-90"
        />
        <span className={`absolute right-1 top-1 h-2.5 w-2.5 rounded-full border border-neutral-950 ${dotClass}`} />
      </button>
    </div>
  );
}
