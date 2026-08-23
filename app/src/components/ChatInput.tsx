import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { setInsertTarget, clearInsertTarget } from "../lib/voice";

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
  /** Stop the running turn. Absent when there is nothing to stop. */
  onStop?: () => void;
}

// Roughly 5-6 lines at text-sm before the input starts scrolling internally
// instead of continuing to grow — enough room for a full dictated sentence
// or two without the input eating the whole panel.
const MAX_DRAFT_INPUT_HEIGHT = 120;

export function ChatInput({ onSend, disabled, onStop }: Props) {
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
      {/* The phone has had this since it shipped; here there was nothing to do
          about a turn gone wrong but wait it out or quit the app — and the
          composer is locked while it runs, so you could not even redirect it.
          The server has always been able to stop a turn on request: closing
          the socket stopped being the way to do it once a phone hanging up
          could no longer end work the laptop was watching. */}
      {disabled && onStop && (
        <button
          type="button"
          onClick={onStop}
          title="stop this turn"
          className="shrink-0 rounded-full border border-white/15 bg-white/5 px-2.5 py-0.5 font-mono text-xs text-neutral-300 transition hover:border-red-400/40 hover:text-red-300 active:scale-95"
        >
          stop
        </button>
      )}
    </div>
  );
}
