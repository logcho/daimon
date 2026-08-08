import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage, Step } from "../types";
import { ThinkingIndicator } from "./ThinkingIndicator";

function StepLine({ step }: { step: Step }) {
  const name = step.tool ?? step.label;

  if (step.status === "done") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="text-emerald-400">✓</span>
        <span className="text-neutral-400">{name}</span>
      </span>
    );
  }
  if (step.status === "error") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="text-red-400">✕</span>
        <span className="text-neutral-400">{name}</span>
      </span>
    );
  }
  if (step.status === "running") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        <span className="text-[#4f8dff]">{name}</span>
      </span>
    );
  }
  return (
    <span className="flex items-center gap-2 font-mono text-sm">
      <span className="block h-2 w-2 rounded-full border border-neutral-600" />
      <span className="text-neutral-500">{name}</span>
    </span>
  );
}

interface Props {
  message: ChatMessage;
}

export function MessageBubble({ message }: Props) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md border border-[#4f8dff]/30 bg-[#4f8dff]/[0.18] px-3.5 py-2 text-sm leading-relaxed text-neutral-50 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)] backdrop-blur-sm">
          {message.content}
        </div>
      </div>
    );
  }

  const empty = message.content === "";
  return (
    <div className="flex justify-start">
      <div className="liquid-glass-subtle max-w-[85%] space-y-2 rounded-2xl rounded-bl-md px-3.5 py-2.5">
        {message.steps.length > 0 && (
          <ul className="space-y-1.5">
            {message.steps.map((step) => (
              <li key={step.id}>
                <StepLine step={step} />
              </li>
            ))}
          </ul>
        )}
        {empty && message.thinking && <ThinkingIndicator />}
        {!empty && (
          <div className="daimon-prose prose prose-invert prose-sm max-w-none text-sm leading-relaxed text-white">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
        {message.error && (
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-red-400">{message.error}</p>
        )}
      </div>
    </div>
  );
}
