export function ThinkingIndicator() {
  return (
    <span className="inline-flex items-center gap-1 text-slate-400">
      <span className="daimon-dot inline-block h-1.5 w-1.5 rounded-full bg-current" />
      <span className="daimon-dot inline-block h-1.5 w-1.5 rounded-full bg-current" />
      <span className="daimon-dot inline-block h-1.5 w-1.5 rounded-full bg-current" />
      <span className="ml-1 text-xs">Thinking…</span>
    </span>
  );
}
