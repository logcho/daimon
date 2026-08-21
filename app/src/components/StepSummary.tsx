import type { Step } from "../types";
import { fmtDuration } from "../lib/format";

/** Tool → the category it counts toward in a finished turn's summary.
 *
 *  Kept deliberately coarse. "9 read · 3 edit · 2 shell" tells you the shape of
 *  what happened; a breakdown by individual tool name would just be the step
 *  list again with the useful part (what each one was called *with*) removed.
 *
 *  Mirrored by `CATEGORY` in `agents/src/daimon_agent/cli/render.py` — the two
 *  clients show the same turn and should describe it the same way. Add a tool
 *  to both. */
const CATEGORY: Record<string, string> = {
  read_file: "read",
  grep_files: "read",
  glob_files: "read",
  list_directory: "read",
  read_note: "read",
  list_notes: "read",
  read_skill: "read",
  list_skills: "read",
  recall: "read",

  write_file: "edit",
  edit_file: "edit",
  delete_file: "edit",
  move_file: "edit",
  mkdir: "edit",
  create_note: "edit",
  append_to_note: "edit",
  move_note: "edit",
  save_skill: "edit",

  run_shell: "shell",
  kernel_execute: "shell",
  run_tests: "shell",
  check_code: "shell",
  debug: "shell",

  web_search: "web",
  web_fetch: "web",
  open_url: "web",
  read_page: "web",
  extract_text: "web",
  find_skills: "web",
};

/** The order categories appear in, so two turns are comparable at a glance. */
const ORDER = ["read", "edit", "shell", "web"];

/** Most children shown behind a disclosure. Expanding a 200-step turn should
 *  not lock the webview, and past a certain length nobody is reading anyway. */
export const MAX_EXPANDED = 30;

/** The category tally as display parts — `["9 read", "3 edit"]`, in ORDER.
 *
 *  Sub-agent steps are skipped: they are counted separately where it matters,
 *  and their own tool calls are not the parent's work. */
export function categoryParts(steps: Step[]): string[] {
  const counts = new Map<string, number>();
  for (const step of steps) {
    if (step.agent_label) continue;
    const category = CATEGORY[step.tool ?? ""];
    if (category) counts.set(category, (counts.get(category) ?? 0) + 1);
  }
  const parts: string[] = [];
  for (const category of ORDER) {
    const n = counts.get(category);
    if (n) parts.push(`${n} ${category}`);
  }
  return parts;
}

export function summarise(steps: Step[], elapsedMs?: number): string {
  const agents = steps.filter((s) => s.agent_label).length;

  const parts = [`${steps.length} step${steps.length === 1 ? "" : "s"}`, ...categoryParts(steps)];
  if (agents) parts.push(`${agents} sub-agent${agents === 1 ? "" : "s"}`);
  if (elapsedMs) parts.push(fmtDuration(elapsedMs / 1000));
  return parts.join(" · ");
}

interface Props {
  /** The line shown when collapsed. A node, not a string: a live run's label
   *  carries a spinner and a coloured tool name. */
  label: React.ReactNode;
  /** Rendered only once opened. */
  children: React.ReactNode;
  /** Nested one level, for a sub-agent sitting inside a turn's own disclosure. */
  indented?: boolean;
}

/** A collapsed run of steps, expandable in place.
 *
 *  `<details>`/`<summary>` rather than a button plus state: the disclosure
 *  behaviour, the keyboard handling, and the accessible role all come for
 *  free, and the children genuinely aren't rendered until it opens — which is
 *  the point for a turn with two hundred of them. */
export function StepSummary({ label, children, indented }: Props) {
  return (
    <details className={`group ${indented ? "ml-4" : ""}`}>
      <summary className="flex cursor-pointer list-none items-baseline gap-2 font-mono text-xs text-neutral-500 hover:text-neutral-300">
        <span className="shrink-0 text-neutral-600 group-open:hidden">▸</span>
        <span className="hidden shrink-0 text-neutral-600 group-open:inline">▾</span>
        <span className="min-w-0 flex-1 truncate">{label}</span>
      </summary>
      <div className="mt-1">{children}</div>
    </details>
  );
}
