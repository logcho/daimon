import { fmtContext, fmtCost, fmtDuration, fmtTokens } from "../lib/format";
import type { TurnUsage } from "../types";

interface Props {
  model: string;
  contextWindow: number;
  turns: number;
  usage: TurnUsage;
  lastElapsedMs?: number;
  busy: boolean;
}

/** The chat's status line — the same readout the CLI pins to the bottom of the
 *  terminal: model · turns · elapsed · ↑↓ tokens · cost · ctx%.
 *
 *  `model` comes from `GET /config`, the same source the CLI's bar and
 *  `/config` read. Three surfaces once each resolved it differently and
 *  disagreed; this one doesn't get its own opinion.
 *
 *  Positioned absolutely inside the input's wrapper (see Panel.tsx) so it
 *  floats under the pill instead of claiming a footer row of its own: `px-6`
 *  lines it up with the `>` prompt inside the input rather than the input's
 *  outer edge, and it takes no pointer events since there's nothing here to
 *  click. */
export function ChatStatusBar({ model, contextWindow, turns, usage, lastElapsedMs, busy }: Props) {
  const parts: string[] = [model];
  if (turns > 0) parts.push(`${turns} turn${turns === 1 ? "" : "s"}`);
  if (lastElapsedMs) parts.push(fmtDuration(lastElapsedMs / 1000));
  if (usage.inputTokens || usage.outputTokens) {
    parts.push(`↑${fmtTokens(usage.inputTokens)} ↓${fmtTokens(usage.outputTokens)}`);
  }
  const cost = fmtCost(usage.costUsd);
  if (cost) parts.push(cost);
  const ctx = fmtContext(usage.contextTokens, contextWindow);
  if (ctx) parts.push(ctx);

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-1.5 flex items-center gap-2 px-6">
      {busy && (
        <span className="block h-1.5 w-1.5 shrink-0 animate-daimon-pulse rounded-full bg-[#4f8dff]" />
      )}
      <p className="truncate font-mono text-[11px] text-neutral-500">{parts.join(" · ")}</p>
    </div>
  );
}
