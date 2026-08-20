/** Formatters for the numbers the agent reports.
 *
 *  Ports of the ones the CLI's status bar uses (`cli/render.py`) — the same
 *  rules, so the app and the terminal read identically for the same turn.
 *  Where they'd differ is exactly where someone starts wondering which is
 *  telling the truth.
 */

/** Compact token counts: 942, 12.4k, 1.83M. */
export function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/** Elapsed time at a resolution a human reads at a glance. */
export function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
  }
  return `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}m`;
}

/** Money, or nothing at all.
 *
 *  Both `null` (a model with no known price) and exactly zero render empty. A
 *  literal "$0.0000" reads as a measurement rather than the absence of one,
 *  and an unpriced model must never look free. */
export function fmtCost(cost: number | null | undefined): string {
  if (!cost) return "";
  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
}

/** Context usage as a percentage of the window, capped at 100. */
export function fmtContext(used: number, window: number): string {
  if (!used || !window) return "";
  return `ctx ${Math.min(Math.round((used / window) * 100), 100)}%`;
}
