import type { AgentKind } from "../types";

interface Props {
  agent: AgentKind;
  onChange: (agent: AgentKind) => void;
  /** When true the picker is disabled (session has messages — agent is locked). */
  locked: boolean;
}

const LABEL: Record<AgentKind, string> = { general: "general", coding: "coding" };
const TITLE: Record<AgentKind, string> = {
  general: "General assistant — browsing, research, notes, shell",
  coding: "Coding specialist — kernel-first, file editing, shell, git",
};

/** Two-chip selector in the chat panel header. Visible only for new (zero-
 *  message) sessions — once the first turn starts, the agent type is locked
 *  for that session's lifetime. */
export function AgentPicker({ agent, onChange, locked }: Props) {
  return (
    <span className="flex items-center gap-1">
      {(Object.keys(LABEL) as AgentKind[]).map((kind) => (
        <button
          key={kind}
          title={TITLE[kind]}
          disabled={locked}
          onClick={() => onChange(kind)}
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
            agent === kind
              ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
              : locked
                ? "text-neutral-600"
                : "text-neutral-500 hover:bg-white/5 hover:text-neutral-300"
          }`}
        >
          {LABEL[kind]}
        </button>
      ))}
    </span>
  );
}
