import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage, Step } from "../types";
import { ErrorBanner } from "./ErrorBanner";
import { ThinkingIndicator } from "./ThinkingIndicator";

const STEP_ICON: Record<Step["status"], string> = {
  pending: "○",
  running: "◌",
  done: "✓",
  error: "✗",
};

const STEP_COLOR: Record<Step["status"], string> = {
  pending: "text-neutral-500",
  running: "text-[#4f8dff]",
  done: "text-emerald-400",
  error: "text-red-400",
};

function StepList({ steps }: { steps: Step[] }) {
  if (steps.length === 0) return null;
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-xs text-neutral-500">
        {steps.length} tool step{steps.length === 1 ? "" : "s"}
      </summary>
      <ul className="mt-1 space-y-0.5 text-xs text-neutral-400">
        {steps.map((s) => (
          <li key={s.id} className="flex items-center gap-1.5">
            <span className={STEP_COLOR[s.status]}>{STEP_ICON[s.status]}</span>
            <span>{s.label}</span>
            {s.tool && <span className="rounded bg-white/10 px-1 text-[10px] text-neutral-400">{s.tool}</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}

interface Props {
  message: ChatMessage;
}

export function MessageBubble({ message }: Props) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md border border-[#4f8dff]/30 bg-[#4f8dff]/[0.18] px-3.5 py-2 text-sm text-neutral-50 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)]">
          {message.content}
        </div>
      </div>
    );
  }

  const empty = message.content === "";
  return (
    <div className="flex justify-start">
      <div className="max-w-[92%]">
        <div className="liquid-glass-subtle rounded-2xl rounded-bl-md px-3.5 py-2.5 text-sm text-neutral-50">
          {empty && message.thinking && <ThinkingIndicator />}
          {empty && message.error && (
            <span className="text-red-400">{message.error}</span>
          )}
          {!empty && (
            <div className="md">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            </div>
          )}
          <StepList steps={message.steps} />
        </div>
        {message.error && <ErrorBanner message={message.error} />}
      </div>
    </div>
  );
}
