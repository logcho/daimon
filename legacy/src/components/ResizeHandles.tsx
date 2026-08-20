import { useRef } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { MAX_PANEL_SIZE, MIN_PANEL_SIZE } from "../lib/window";

// Extracted from PipelinePanel, where these ~140 lines of pointer-capture,
// rAF-coalesced, throttled native window resizing sat above the chat panel
// they have nothing to do with.

type ResizeDirection = "East" | "North" | "NorthEast" | "NorthWest" | "South" | "SouthEast" | "SouthWest" | "West";

// The window has `decorations: false` (no native title bar/frame), so macOS
// never gives it edge-hover resize cursors or drag-to-resize on its own —
// that's normally a side effect of the native chrome this window doesn't
// have. These are the grab targets, living in the outer p-6 (24px) margin
// around the visible rounded panel (see the glow-clipping note in the
// design system skill for why that margin exists) — but straddling the
// *visible panel's* edge (the 24px mark) rather than sitting flush at the
// true, invisible window edge (0px). The window boundary itself is fully
// transparent, so a handle positioned right at it reads as "far outside"
// anything the user can actually see; centering each handle on the edge
// they can see is what makes it findable.
//
// This drives the resize manually (pointer-move deltas -> direct setSize/
// setPosition calls, coalesced to one update per animation frame — see
// `scheduleApply` below) rather than via Tauri's `startResizeDragging` —
// that API exists specifically for undecorated windows like this one, but
// its real-world reliability on macOS turned out to be inconsistent (it's
// primarily been fixed/tested for Windows and Linux upstream). Driving it
// ourselves means the actual resize behavior only depends on `setSize`/
// `setPosition`, which are already used elsewhere in this app and known to
// work correctly.
// The panel now fills the window edge-to-edge (no p-6 margin — that margin
// existed for a soft outer glow that's since been replaced by native
// vibrancy, see vibrancy.rs). So the grab targets sit right at the window
// edges rather than in a margin straddling an inset panel edge. Edges are
// thin strips; corners are small squares. The corner squares overlap the
// transparent rounded-corner region of the window, which still captures
// pointer events on a transparent Tauri window, so they remain grabbable.
const RESIZE_HANDLES: { direction: ResizeDirection; className: string }[] = [
  { direction: "North", className: "inset-x-5 top-0 h-1.5 cursor-ns-resize" },
  { direction: "South", className: "inset-x-5 bottom-0 h-1.5 cursor-ns-resize" },
  { direction: "West", className: "inset-y-5 left-0 w-1.5 cursor-ew-resize" },
  { direction: "East", className: "inset-y-5 right-0 w-1.5 cursor-ew-resize" },
  { direction: "NorthWest", className: "left-0 top-0 h-4 w-4 cursor-nwse-resize" },
  { direction: "NorthEast", className: "right-0 top-0 h-4 w-4 cursor-nesw-resize" },
  { direction: "SouthWest", className: "left-0 bottom-0 h-4 w-4 cursor-nesw-resize" },
  { direction: "SouthEast", className: "right-0 bottom-0 h-4 w-4 cursor-nwse-resize" },
];

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

interface ResizeDragState {
  direction: ResizeDirection;
  startPointerX: number;
  startPointerY: number;
  startX: number;
  startY: number;
  startWidth: number;
  startHeight: number;
}

interface PendingResize {
  width: number;
  height: number;
  x: number;
  y: number;
  reposition: boolean;
}

// Tauri's window API has no single call that sets size and position
// atomically — only separate `setSize`/`setPosition`. For a drag that
// needs both (any corner/edge involving West or North, which move the
// window's origin as well as its size), applying them as two independent
// calls means there's always a moment where only one has landed — the
// window renders with the *old* position and *new* size (or vice versa),
// which briefly overshoots or undershoots the anchored edge before the
// other call catches up. At 60fps (once per animation frame) that happens
// on every single frame, which is what reads as continuous jitter rather
// than an occasional glitch.
//
// Two mitigations: apply position before size each time (so by the time
// size lands, the window's origin is already correct — better than the
// reverse, which would render the un-moved edge overshooting past its
// target), and throttle to a fixed minimum interval rather than trying to
// keep up with every animation frame, so the native resize call actually
// has time to finish before the next one is dispatched instead of queuing
// up a backlog.
const MIN_APPLY_INTERVAL_MS = 32;

export function ResizeHandles() {
  const dragRef = useRef<ResizeDragState | null>(null);
  const pendingRef = useRef<PendingResize | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastAppliedAtRef = useRef(0);
  const applyInFlightRef = useRef(false);

  async function applyPending(pending: PendingResize) {
    applyInFlightRef.current = true;
    lastAppliedAtRef.current = performance.now();
    try {
      const win = getCurrentWindow();
      if (pending.reposition) {
        await win.setPosition(new LogicalPosition(pending.x, pending.y));
      }
      await win.setSize(new LogicalSize(pending.width, pending.height));
    } finally {
      applyInFlightRef.current = false;
    }
  }

  function scheduleApply() {
    if (rafRef.current !== null) return;
    const tick = () => {
      rafRef.current = null;
      const pending = pendingRef.current;
      if (!pending) return;

      const sinceLastApply = performance.now() - lastAppliedAtRef.current;
      if (applyInFlightRef.current || sinceLastApply < MIN_APPLY_INTERVAL_MS) {
        // Not ready yet — the previous update is still landing, or it's too
        // soon since the last one. Check again next frame rather than
        // dropping this pending value.
        rafRef.current = requestAnimationFrame(tick);
        return;
      }

      void applyPending(pending);
    };
    rafRef.current = requestAnimationFrame(tick);
  }

  async function handlePointerDown(direction: ResizeDirection, e: React.PointerEvent<HTMLDivElement>) {
    e.preventDefault();
    const win = getCurrentWindow();
    const [scale, size, position] = await Promise.all([win.scaleFactor(), win.innerSize(), win.outerPosition()]);
    dragRef.current = {
      direction,
      startPointerX: e.screenX,
      startPointerY: e.screenY,
      startX: position.x / scale,
      startY: position.y / scale,
      startWidth: size.width / scale,
      startHeight: size.height / scale,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function handlePointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag) return;

    const dx = e.screenX - drag.startPointerX;
    const dy = e.screenY - drag.startPointerY;

    let width = drag.startWidth;
    let height = drag.startHeight;
    let x = drag.startX;
    let y = drag.startY;

    // Each direction that includes an edge clamps that dimension, then (for
    // the edges that move the window's origin — West/North) re-derives the
    // position from the *clamped* size so the opposite edge stays perfectly
    // anchored even once the size hits its min/max, instead of drifting.
    if (drag.direction.includes("East")) {
      width = clamp(drag.startWidth + dx, MIN_PANEL_SIZE.width, MAX_PANEL_SIZE.width);
    }
    if (drag.direction.includes("West")) {
      width = clamp(drag.startWidth - dx, MIN_PANEL_SIZE.width, MAX_PANEL_SIZE.width);
      x = drag.startX + drag.startWidth - width;
    }
    if (drag.direction.includes("South")) {
      height = clamp(drag.startHeight + dy, MIN_PANEL_SIZE.height, MAX_PANEL_SIZE.height);
    }
    if (drag.direction.includes("North")) {
      height = clamp(drag.startHeight - dy, MIN_PANEL_SIZE.height, MAX_PANEL_SIZE.height);
      y = drag.startY + drag.startHeight - height;
    }

    // Once a dimension is clamped at its min/max, further cursor movement in
    // that same direction keeps producing pointer-move events but no actual
    // change in the result — the cursor just keeps moving further past
    // where the edge stopped responding. Skip re-applying (and re-scheduling
    // an IPC round trip for) a value that's identical to what's already
    // pending/applied, rather than continuing to act on every move event
    // even though nothing about the target size/position actually changed.
    const current = pendingRef.current;
    if (current && current.width === width && current.height === height && current.x === x && current.y === y) {
      return;
    }

    pendingRef.current = {
      width,
      height,
      x,
      y,
      reposition: drag.direction.includes("West") || drag.direction.includes("North"),
    };
    scheduleApply();
  }

  function handlePointerUp(e: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    // Apply whatever the last recorded position was immediately, rather
    // than dropping it — otherwise a drag that ends mid-frame could settle
    // one update behind where the cursor actually stopped.
    const pending = pendingRef.current;
    if (pending) void applyPending(pending);
    e.currentTarget.releasePointerCapture(e.pointerId);
  }

  return (
    <>
      {RESIZE_HANDLES.map(({ direction, className }) => (
        <div
          key={direction}
          onPointerDown={(e) => handlePointerDown(direction, e)}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          className={`absolute z-10 ${className}`}
        />
      ))}
    </>
  );
}
