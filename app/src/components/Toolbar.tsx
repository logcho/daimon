import type { AgentStatus } from "../types";

const STATUS_LABEL: Record<"running" | "adopted" | "down", string> = {
  running: "agent running",
  adopted: "agent adopted (external)",
  down: "agent offline",
};

interface Props {
  status: AgentStatus | null;
  onNewChat: () => void;
  disabled: boolean;
}

export function Toolbar({ status, onNewChat, disabled }: Props) {
  const key = !status || !status.running ? "down" : status.adopted ? "adopted" : "running";
  const dot = status?.running
    ? status.adopted ? "bg-amber-500" : "bg-green-500"
    : "bg-red-500";

  return (
    <header className="flex items-center gap-2 border-b border-slate-200 bg-white px-3 py-2">
      <h1 className="text-sm font-semibold text-slate-800">Daimon</h1>
      <span className="flex items-center gap-1.5 text-xs text-slate-500" title={status ? `port ${status.port}` : undefined}>
        <span className={`h-2 w-2 rounded-full ${dot}`} />
        {STATUS_LABEL[key]}
      </span>
      <button
        onClick={onNewChat}
        disabled={disabled}
        className="ml-auto rounded-md border border-slate-300 px-2.5 py-1 text-xs text-slate-600 transition-colors hover:bg-slate-100 disabled:opacity-40"
      >
        New chat
      </button>
    </header>
  );
}
