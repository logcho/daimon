import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import { MessageBubble } from "./MessageBubble";
import { useSpinnerFrame } from "./ThinkingIndicator";

interface Props {
  messages: ChatMessage[];
  /** The bus is disconnected while the newest turn is still open. */
  stalled?: boolean;
  /** Text that has been sent but whose `user` event hasn't come back yet. */
  pending?: string | null;
  /** Bumped by the parent when the user sends something, to snap back down. */
  snapToken?: number;
  /** Replay could not reach the start of this conversation, so what is shown
   *  begins mid-way through it. */
  truncated?: boolean;
}

/** How close to the bottom still counts as "at the bottom".
 *
 *  Generous: a smooth scroll that hasn't quite landed, a bubble growing by a
 *  token, and sub-pixel rounding all land a little short, and treating any of
 *  those as "the user scrolled away" would stop the transcript following.
 */
const STICK_THRESHOLD_PX = 120;

/** The message you just sent, before the bus has echoed it back.
 *
 *  Deliberately not a transcript entry. The `user` event opens the real
 *  exchange a moment later (see App.tsx's `send`) and drawing one here as
 *  well would show every sent message twice — so this is a distinct,
 *  clearly-provisional row that the arriving event replaces. */
function PendingRow({ text }: { text: string }) {
  const frame = useSpinnerFrame();
  return (
    <div className="flex justify-end">
      <div className="flex max-w-[85%] items-baseline gap-2 rounded-2xl rounded-br-md border border-white/10 bg-white/[0.04] px-3.5 py-2 text-sm leading-relaxed text-neutral-400">
        <span className="shrink-0 font-mono text-xs text-[#4f8dff]">{frame}</span>
        <span className="min-w-0 whitespace-pre-wrap">{text}</span>
      </div>
    </div>
  );
}

export function MessageList({ messages, stalled, pending, snapToken, truncated }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // Whether the transcript should keep following new output. Starts true and
  // only goes false when the user deliberately scrolls away from the bottom.
  const [stick, setStick] = useState(true);

  const atBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX;
  }, []);

  // When we last scrolled the container ourselves. A smooth scroll fires a
  // scroll event on every frame of its animation, and each one reports a
  // position that is not yet the bottom — so without this the follow below
  // would read its own animation as the user scrolling away and switch itself
  // off halfway through.
  const programmatic = useRef(0);

  const scrollToBottom = useCallback((behavior: ScrollBehavior) => {
    programmatic.current = Date.now();
    bottomRef.current?.scrollIntoView({ behavior, block: "end" });
  }, []);

  // This used to fire on every `messages` change with no condition at all,
  // which during streaming is many times a second: scrolling up to re-read
  // something earlier in a running turn yanked you straight back to the
  // bottom, so a turn could not be read while it ran. The CLI has always got
  // this right — "scrolling up to read something keeps you there while output
  // arrives; submitting anything snaps back to the newest" — and this is the
  // same rule.
  //
  // Layout effect rather than effect: it runs before paint, so following the
  // transcript looks like the content growing rather than a visible jump.
  // `auto`, not `smooth`: while streaming this runs many times a second and
  // each increment is a line or two, so an animation has nothing to smooth and
  // every frame of it is another chance to be mistaken for a manual scroll.
  useLayoutEffect(() => {
    if (stick) scrollToBottom("auto");
  }, [messages, pending, stick, scrollToBottom]);

  // Submitting anything snaps back, however far up the user had scrolled —
  // you are asking about the newest thing, so that is where you want to be.
  useEffect(() => {
    if (snapToken === undefined) return;
    setStick(true);
    scrollToBottom("smooth");
  }, [snapToken, scrollToBottom]);

  const onScroll = useCallback(() => {
    if (Date.now() - programmatic.current < 400) return;
    setStick(atBottom());
  }, [atBottom]);

  if (messages.length === 0 && !pending) {
    return (
      <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-neutral-500">
        Ask Daimon anything — coding, research, or chores around your computer.
      </div>
    );
  }

  const last = messages.length - 1;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="themed-scroll flex-1 space-y-4 overflow-y-auto px-3 py-4"
      >
        {/* The server could not replay this far back — its ring and its event
            log both have limits. A conversation that visibly starts mid-way
            through beats one that silently pretends this is the beginning. */}
        {truncated && (
          <p className="pb-1 text-center font-mono text-[11px] text-neutral-600">
            earlier messages aren't available here
          </p>
        )}
        {messages.map((m, i) => (
          // Only the newest turn can be the stalled one: an older bubble left
          // mid-thought is history, not something still in flight.
          <MessageBubble key={m.id} message={m} stalled={stalled && i === last} />
        ))}
        {pending && <PendingRow text={pending} />}
        <div ref={bottomRef} />
      </div>

      {/* Only while detached, and only when there is something below to go
          back to. Without it, scrolling up during a long turn strands you
          with no obvious way to rejoin the live output. */}
      {!stick && (
        <button
          onClick={() => {
            setStick(true);
            scrollToBottom("smooth");
          }}
          className="liquid-glass-subtle absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full px-3 py-1 text-xs text-neutral-300 shadow-lg transition hover:text-[#4f8dff] active:scale-95"
        >
          jump to latest ↓
        </button>
      )}
    </div>
  );
}
