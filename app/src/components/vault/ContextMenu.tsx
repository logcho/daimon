import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  /** Renders in red and, on the first click, re-labels itself instead of
   *  firing — the same arm-then-confirm `DeleteButton` uses, because Tauri's
   *  webview has no `window.confirm` to ask with. */
  destructive?: boolean;
  /** A dividing line above this item. */
  separated?: boolean;
}

export interface ContextMenuProps {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
}

/** A right-click menu.
 *
 *  Everything a file manager needs a dialog for — rename, confirm a delete —
 *  has to happen without one here: `window.prompt`, `confirm` and `alert` all
 *  return null in Tauri's webview, silently. So rename hands back to an inline
 *  input in the tree, and delete arms in place.
 */
export function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ x, y });
  const [armed, setArmed] = useState<number | null>(null);

  // Flip back inside the window when opened near an edge — a menu half
  // off-screen is a menu with unreachable items.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    setPos({
      x: x + width > window.innerWidth - 8 ? Math.max(8, x - width) : x,
      y: y + height > window.innerHeight - 8 ? Math.max(8, y - height) : y,
    });
  }, [x, y]);

  useEffect(() => {
    const close = () => onClose();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // `pointerdown` rather than `click` so the menu is gone before whatever
    // is underneath reacts.
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", onKey);
    window.addEventListener("blur", close);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", close);
      window.removeEventListener("resize", close);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      style={{ left: pos.x, top: pos.y }}
      // Stop the window-level closer from firing on a click *inside* the menu.
      onPointerDown={(e) => e.stopPropagation()}
      className="liquid-glass-subtle fixed z-50 min-w-[10rem] overflow-hidden rounded-lg border border-white/10 py-1 text-xs shadow-xl"
    >
      {items.map((item, i) => (
        <div key={item.label}>
          {item.separated && <div className="my-1 border-t border-white/10" />}
          <button
            type="button"
            onClick={() => {
              if (item.destructive && armed !== i) {
                setArmed(i);
                return;
              }
              item.onSelect();
              onClose();
            }}
            className={`block w-full px-3 py-1.5 text-left transition ${
              item.destructive
                ? armed === i
                  ? "bg-red-500/20 text-red-300"
                  : "text-red-400/80 hover:bg-red-500/10 hover:text-red-300"
                : "text-neutral-300 hover:bg-white/10 hover:text-neutral-100"
            }`}
          >
            {item.destructive && armed === i ? `click again to ${item.label.toLowerCase()}` : item.label}
          </button>
        </div>
      ))}
    </div>
  );
}
