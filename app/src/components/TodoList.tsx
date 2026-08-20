import { useEffect, useState } from "react";
import type { TodoItem } from "../types";

/** The agent's task list, pinned above the composer.
 *
 *  Session state, not message state — the same split `cli/live.py` makes,
 *  where todos live in `LiveState` and never enter the transcript. A checklist
 *  that scrolls away with the turn that created it leaves the screen at
 *  exactly the point the work gets long enough to want one.
 *
 *  The active item is undimmed and finished ones are struck through, so the
 *  eye lands on what's happening now rather than on the pile behind it. Long
 *  lists collapse to their header; the header alone still carries the count
 *  and the current item, which is the part worth having at a glance. */
export function TodoList({ items }: { items: TodoItem[] }) {
  const [open, setOpen] = useState(true);
  const done = items.filter((i) => i.status === "done").length;
  const active = items.find((i) => i.status === "in_progress");

  // Re-open when a genuinely different list arrives. Someone who collapsed the
  // list collapsed *that* list, not the idea of a checklist — but a status
  // flipping from pending to done shouldn't spring it back open, so the
  // signature is the item text, not the whole list.
  const signature = items.map((i) => i.text).join(" ");
  useEffect(() => setOpen(true), [signature]);

  if (items.length === 0) return null;

  return (
    <div className="shrink-0 border-t border-white/[0.06] bg-white/[0.02] px-3 py-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline gap-2 text-left"
      >
        <span className="text-[11px] uppercase tracking-wider text-neutral-500">todos</span>
        <span className="font-mono text-[11px] text-neutral-600">
          {done}/{items.length}
        </span>
        {/* Collapsed, the header has to carry the one thing that matters:
            what the agent is on right now. */}
        {!open && active && (
          <span className="truncate text-xs text-neutral-400">{active.text}</span>
        )}
        <span className="ml-auto shrink-0 font-mono text-[11px] text-neutral-600">
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <ul className="themed-scroll mt-1 max-h-32 space-y-0.5 overflow-y-auto">
          {items.map((item) => (
            <li key={item.id} className="flex items-baseline gap-2 text-xs">
              <span
                className={
                  item.status === "done"
                    ? "text-emerald-400"
                    : item.status === "in_progress"
                      ? "text-[#4f8dff]"
                      : "text-neutral-600"
                }
              >
                {item.status === "done" ? "✓" : item.status === "in_progress" ? "▸" : "○"}
              </span>
              <span
                className={
                  item.status === "done"
                    ? "text-neutral-600 line-through"
                    : item.status === "in_progress"
                      ? "text-neutral-200"
                      : "text-neutral-500"
                }
              >
                {item.text}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
