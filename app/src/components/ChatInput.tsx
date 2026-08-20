import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { setInsertTarget, clearInsertTarget } from "../lib/voice";

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
}

// Roughly 5-6 lines at text-sm before the input starts scrolling internally
// instead of continuing to grow — enough room for a full dictated sentence
// or two without the input eating the whole panel.
const MAX_DRAFT_INPUT_HEIGHT = 120;

export function ChatInput({ onSend, disabled }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grows the input with its content (up to MAX_DRAFT_INPUT_HEIGHT,
  // beyond which it scrolls internally).
  const recalculateDraftHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_DRAFT_INPUT_HEIGHT)}px`;
  }, []);

  useEffect(() => {
    recalculateDraftHeight();
  }, [text, recalculateDraftHeight]);

  // The calculation above depends on the textarea's current *width* (a
  // narrower width wraps the same text into more lines, giving a taller
  // scrollHeight) — but it only re-ran on `text` changes. The panel's own
  // expand animation (window.ts's animateTo) resizes the real native
  // window gradually over ~220ms, independently of React's render cycle —
  // if the height calculation happened to run while that was still
  // mid-flight, it measured at a stale, too-narrow width and locked in an
  // inflated height. A ResizeObserver on the textarea recalculates whenever
  // its actual rendered width changes for any reason.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => recalculateDraftHeight());
    observer.observe(el);
    return () => observer.disconnect();
  }, [recalculateDraftHeight]);

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
    <div className="mx-3 mb-1 flex items-center gap-2 rounded-2xl border border-white/10 bg-white/[0.04] px-3 py-2.5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.08)] backdrop-blur-md transition focus-within:border-[#4f8dff]/40 focus-within:bg-white/[0.06]">
      <span className="font-mono text-sm text-[#4f8dff]">&gt;</span>
      <textarea
        ref={textareaRef}
        autoFocus
        rows={1}
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
        placeholder={disabled ? "waiting for daimon to finish this turn…" : "tell daimon what to do..."}
        disabled={disabled}
        className="max-h-32 w-full resize-none overflow-y-auto bg-transparent py-0 font-mono text-sm leading-5 text-neutral-100 outline-none placeholder:text-neutral-500 disabled:opacity-50"
      />
    </div>
  );
}
