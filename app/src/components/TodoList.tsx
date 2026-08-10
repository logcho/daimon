import type { TodoItem } from "../types";

/** The agent's task list, mirroring the CLI's `render.todo_lines`.
 *
 *  The active item is undimmed and finished ones are struck through, so the
 *  eye lands on what's happening now rather than on the pile behind it. */
export function TodoList({ items }: { items: TodoItem[] }) {
  if (items.length === 0) return null;
  const done = items.filter((i) => i.status === "done").length;

  return (
    <div className="rounded-lg border border-white/[0.08] bg-white/[0.02] px-2.5 py-2">
      <p className="mb-1 text-[11px] uppercase tracking-wider text-neutral-500">
        todos <span className="text-neutral-600">{done}/{items.length}</span>
      </p>
      <ul className="space-y-0.5">
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
    </div>
  );
}
