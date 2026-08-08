import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { setInsertTarget, clearInsertTarget } from "../lib/voice";

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
}

export function ChatInput({ onSend, disabled }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Autosize: reset height to 0 so scrollHeight reflects content, not the
  // previous expanded height. Capped by max-h-32 + overflow-y-auto in CSS.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="m-3 flex items-start gap-2 rounded-2xl border border-white/10 bg-white/[0.04] px-3 py-2.5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.08)]">
      <span className="mt-[3px] font-mono text-sm leading-5 text-[#4f8dff]">&gt;</span>
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() =>
          setInsertTarget({
            kind: "chat",
            // Append dictated text to whatever the user has already typed
            setText: (t: string) => setText((prev) => (prev ? `${prev} ${t}` : t)),
          })
        }
        onBlur={() => clearInsertTarget()}
        rows={1}
        placeholder={disabled ? "Working…" : "Message Daimon (Enter to send)"}
        disabled={disabled}
        className="max-h-32 flex-1 resize-none bg-transparent py-0 font-mono text-sm leading-5 text-neutral-100 outline-none placeholder:text-neutral-500 disabled:opacity-50"
      />
    </div>
  );
}
