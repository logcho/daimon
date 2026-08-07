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

function StepList({ steps }: { steps: Step[] }) {
  if (steps.length === 0) return null;
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-xs text-slate-400">
        {steps.length} tool step{steps.length === 1 ? "" : "s"}
      </summary>
      <ul className="mt-1 space-y-0.5 text-xs text-slate-500">
        {steps.map((s) => (
          <li key={s.id} className="flex items-center gap-1.5">
            <span className={s.status === "error" ? "text-red-500" : s.status === "done" ? "text-green-600" : "text-slate-400"}>
              {STEP_ICON[s.status]}
            </span>
            <span>{s.label}</span>
            {s.tool && <span className="rounded bg-slate-200 px-1 text-[10px] text-slate-500">{s.tool}</span>}
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
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-blue-600 px-3.5 py-2 text-sm text-white whitespace-pre-wrap">
          {message.content}
        </div>
      </div>
    );
  }

  const empty = message.content === "";
  return (
    <div className="flex justify-start">
      <div className="max-w-[92%]">
        <div className="rounded-2xl rounded-bl-md border border-slate-200 bg-white px-3.5 py-2.5 text-sm text-slate-800">
          {empty && message.thinking && <ThinkingIndicator />}
          {empty && message.error && (
            <span className="text-red-600">{message.error}</span>
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
