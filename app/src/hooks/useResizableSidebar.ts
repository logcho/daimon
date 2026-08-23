import { useCallback, useEffect, useRef, useState } from "react";

const KEY = "daimon-vault-sidebar-w";
const MIN = 180;
const MAX = 480;
const DEFAULT = 208; // what `w-52` was, so nothing jumps on first run

/** A drag-to-resize sidebar whose width outlives the panel.
 *
 *  Pointer events with capture, not HTML5 drag: the tree inside this sidebar
 *  is itself a drag source, and an HTML5 drag on the handle fights it and
 *  trails a ghost image across the window.
 *
 *  The width is read in the initializer rather than an effect so the first
 *  paint is already the right size — restoring it afterwards is a visible
 *  jump on every visit to the tab, and this panel remounts on every visit.
 */
export function useResizableSidebar() {
  const [width, setWidth] = useState(() => {
    try {
      const stored = Number(localStorage.getItem(KEY));
      return stored >= MIN && stored <= MAX ? stored : DEFAULT;
    } catch {
      // Private mode, or storage disabled — a default width is a fine answer.
      return DEFAULT;
    }
  });
  const [dragging, setDragging] = useState(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      startX.current = e.clientX;
      startWidth.current = width;
      setDragging(true);
    },
    [width],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragging) return;
      const next = startWidth.current + (e.clientX - startX.current);
      setWidth(Math.min(MAX, Math.max(MIN, next)));
    },
    [dragging],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      if (!dragging) return;
      (e.target as HTMLElement).releasePointerCapture(e.pointerId);
      setDragging(false);
    },
    [dragging],
  );

  /** Double-click the handle to go back to the default. */
  const reset = useCallback(() => setWidth(DEFAULT), []);

  // Persist on settle rather than on every move — a drag is a hundred events
  // and none of the intermediate ones is worth storing.
  useEffect(() => {
    if (dragging) return;
    try {
      localStorage.setItem(KEY, String(width));
    } catch {
      /* not being able to remember the width is not worth an error */
    }
  }, [dragging, width]);

  return { width, dragging, onPointerDown, onPointerMove, onPointerUp, reset };
}
