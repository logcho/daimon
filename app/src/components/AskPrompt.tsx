import { useEffect, useState } from "react";
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
 * The agent is waiting on an answer.
 *
 * Sits above the composer rather than in the transcript, for the reason todos
 * do: a question is something the session is *waiting on*, not a line of
 * history. It also has to stay put while the answer is chosen, which a message
 * that scrolls away cannot.
 *
 * The same prompt may be showing on a phone. Whoever answers first wins, and
 * the other side's copy disappears on the `ask_resolved` that follows.
 */
export function AskPrompt({
  ask,
  onAnswer,
}: {
  ask: AskPrompt;
  onAnswer: (answer: string | string[]) => void;
}) {
  const [chosen, setChosen] = useState<string[]>([]);
  const [freeform, setFreeform] = useState("");
  const multi = !!ask.multi_select;

  // A new question must not inherit the last one's selection.
  useEffect(() => {
    setChosen([]);
    setFreeform("");
  }, [ask.id]);

  function pick(label: string) {
    if (!multi) return onAnswer(label);
    setChosen((prev) => (prev.includes(label) ? prev.filter((l) => l !== label) : [...prev, label]));
  }

  return (
    <div className="mx-3 mb-2 rounded-2xl border [border-color:rgba(79,141,255,0.35)] bg-[#4f8dff]/5 p-3">
      {ask.header && (
        <div className="mb-1 text-[10px] uppercase tracking-wide text-[#4f8dff]">{ask.header}</div>
      )}
      <div className="text-sm text-neutral-100">{ask.question}</div>

      {ask.plan && (
        <div className="prose prose-invert prose-sm mt-2 max-h-56 max-w-none overflow-y-auto rounded-xl border border-white/10 bg-black/20 p-2.5">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{ask.plan}</ReactMarkdown>
        </div>
      )}

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {ask.options.map((option) => {
          const selected = chosen.includes(option.label);
          return (
            <button
              key={option.label}
              onClick={() => pick(option.label)}
              title={option.description}
              className={`rounded-full px-3 py-1 text-xs transition active:scale-95 ${
                selected
                  ? "bg-[#4f8dff] text-white"
                  : "liquid-glass-subtle text-neutral-200 hover:text-[#4f8dff]"
              }`}
            >
              {option.label}
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
        className="mt-2 flex gap-1.5"
      >
        <input
          value={freeform}
          onChange={(e) => setFreeform(e.target.value)}
          placeholder={multi ? "or say something else" : "or type an answer"}
          className="min-w-0 flex-1 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-neutral-100 outline-none focus:[border-color:rgba(79,141,255,0.5)]"
        />
        {multi ? (
          <button
            type="button"
            onClick={() => onAnswer(chosen)}
            disabled={chosen.length === 0}
            className="rounded-full bg-[#4f8dff] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            done
          </button>
        ) : (
          <button
            type="submit"
            disabled={!freeform.trim()}
            className="rounded-full bg-[#4f8dff] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            send
          </button>
        )}
      </form>
    </div>
  );
}
