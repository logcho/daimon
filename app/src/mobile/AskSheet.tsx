import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface AskPrompt {
  id: string;
  kind: "question" | "plan" | "continue";
  question: string;
  options: { label: string; description?: string }[];
  multi_select?: boolean;
  plan?: string;
  header?: string;
}

/**
 * The agent is waiting on you.
 *
 * A sheet rather than an inline message because it is the one thing on screen
 * that is blocking work — and because a plan can be long enough to need its
 * own scroll without pushing the answer buttons off the bottom.
 */
export function AskSheet({
  ask,
  onAnswer,
  onDismiss,
}: {
  ask: AskPrompt;
  onAnswer: (answer: string | string[]) => void;
  onDismiss: () => void;
}) {
  const [chosen, setChosen] = useState<string[]>([]);
  const [freeform, setFreeform] = useState("");

  const multi = !!ask.multi_select;

  function pick(label: string) {
    if (!multi) {
      onAnswer(label);
      return;
    }
    setChosen((prev) => (prev.includes(label) ? prev.filter((l) => l !== label) : [...prev, label]));
  }

  return (
    <div className="fixed inset-0 z-20 flex flex-col justify-end bg-black/60 backdrop-blur-sm">
      <div
        className="max-h-[85dvh] overflow-y-auto rounded-t-3xl border-t border-white/10 bg-neutral-900 px-4 pt-4"
        style={{ paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}
      >
        <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-white/20" />

        {ask.header && (
          <div className="mb-1 text-[11px] uppercase tracking-wide text-[#4f8dff]">{ask.header}</div>
        )}
        <h2 className="text-base font-medium text-neutral-100">{ask.question}</h2>

        {ask.plan && (
          <div className="prose prose-invert prose-sm mt-3 max-w-none rounded-2xl border border-white/10 bg-white/5 p-3">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{ask.plan}</ReactMarkdown>
          </div>
        )}

        <div className="mt-4 space-y-2">
          {ask.options.map((option) => {
            const selected = chosen.includes(option.label);
            return (
              <button
                key={option.label}
                onClick={() => pick(option.label)}
                className={`w-full rounded-2xl border px-4 py-3 text-left transition active:scale-[0.99] ${
                  selected
                    ? "border-[#4f8dff]/60 bg-[#4f8dff]/15"
                    : "border-white/10 bg-white/5"
                }`}
              >
                <div className="text-sm text-neutral-100">{option.label}</div>
                {option.description && (
                  <div className="mt-0.5 text-xs text-neutral-400">{option.description}</div>
                )}
              </button>
            );
          })}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            const text = freeform.trim();
            if (text) onAnswer(text);
          }}
          className="mt-3 flex gap-2"
        >
          <input
            value={freeform}
            onChange={(e) => setFreeform(e.target.value)}
            placeholder={multi ? "or say something else" : "or type an answer"}
            className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-white/5 px-4 py-2.5 text-sm text-neutral-100 outline-none focus:border-[#4f8dff]/60"
          />
          {multi ? (
            <button
              type="button"
              onClick={() => onAnswer(chosen)}
              disabled={chosen.length === 0}
              className="rounded-2xl bg-[#4f8dff] px-4 py-2.5 text-sm font-medium text-white disabled:opacity-40"
            >
              done
            </button>
          ) : (
            <button
              type="submit"
              disabled={!freeform.trim()}
              className="rounded-2xl bg-[#4f8dff] px-4 py-2.5 text-sm font-medium text-white disabled:opacity-40"
            >
              send
            </button>
          )}
        </form>

        <button onClick={onDismiss} className="mt-2 w-full py-3 text-xs text-neutral-500">
          not now
        </button>
      </div>
    </div>
  );
}
