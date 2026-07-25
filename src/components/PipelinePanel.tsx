import { useCallback, useEffect, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { activateAndFocusWindow } from "../lib/api";
import { MAX_PANEL_SIZE, MIN_PANEL_SIZE } from "../lib/window";
import type { DictationState, PendingDraft, PendingInput, Session, TaskStep, View } from "../types";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { Settings } from "./Settings";
import { VaultPanel } from "./VaultPanel";
import { AutomationsPanel } from "./AutomationsPanel";
import { TerminalPanel } from "./TerminalPanel";
import { ScreenPanel } from "./ScreenPanel";
import { SoundWave } from "./SoundWave";

// Roughly 5-6 lines at text-sm before the input starts scrolling internally
// instead of continuing to grow — enough room for a full dictated sentence
// or two without the input eating the whole panel.
const MAX_DRAFT_INPUT_HEIGHT = 120;

type ResizeDirection = "East" | "North" | "NorthEast" | "NorthWest" | "South" | "SouthEast" | "SouthWest" | "West";

// The window has `decorations: false` (no native title bar/frame), so macOS
// never gives it edge-hover resize cursors or drag-to-resize on its own —
// that's normally a side effect of the native chrome this window doesn't
// have. These are the grab targets, living in the outer p-6 (24px) margin
// around the visible rounded panel (see the glow-clipping note in the
// design system skill for why that margin exists) — but straddling the
// *visible panel's* edge (the 24px mark) rather than sitting flush at the
// true, invisible window edge (0px). The window boundary itself is fully
// transparent, so a handle positioned right at it reads as "far outside"
// anything the user can actually see; centering each handle on the edge
// they can see is what makes it findable.
//
// This drives the resize manually (pointer-move deltas -> direct setSize/
// setPosition calls, coalesced to one update per animation frame — see
// `scheduleApply` below) rather than via Tauri's `startResizeDragging` —
// that API exists specifically for undecorated windows like this one, but
// its real-world reliability on macOS turned out to be inconsistent (it's
// primarily been fixed/tested for Windows and Linux upstream). Driving it
// ourselves means the actual resize behavior only depends on `setSize`/
// `setPosition`, which are already used elsewhere in this app and known to
// work correctly.
// The panel now fills the window edge-to-edge (no p-6 margin — that margin
// existed for a soft outer glow that's since been replaced by native
// vibrancy, see vibrancy.rs). So the grab targets sit right at the window
// edges rather than in a margin straddling an inset panel edge. Edges are
// thin strips; corners are small squares. The corner squares overlap the
// transparent rounded-corner region of the window, which still captures
// pointer events on a transparent Tauri window, so they remain grabbable.
const RESIZE_HANDLES: { direction: ResizeDirection; className: string }[] = [
  { direction: "North", className: "inset-x-5 top-0 h-1.5 cursor-ns-resize" },
  { direction: "South", className: "inset-x-5 bottom-0 h-1.5 cursor-ns-resize" },
  { direction: "West", className: "inset-y-5 left-0 w-1.5 cursor-ew-resize" },
  { direction: "East", className: "inset-y-5 right-0 w-1.5 cursor-ew-resize" },
  { direction: "NorthWest", className: "left-0 top-0 h-4 w-4 cursor-nwse-resize" },
  { direction: "NorthEast", className: "right-0 top-0 h-4 w-4 cursor-nesw-resize" },
  { direction: "SouthWest", className: "left-0 bottom-0 h-4 w-4 cursor-nesw-resize" },
  { direction: "SouthEast", className: "right-0 bottom-0 h-4 w-4 cursor-nwse-resize" },
];

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

interface ResizeDragState {
  direction: ResizeDirection;
  startPointerX: number;
  startPointerY: number;
  startX: number;
  startY: number;
  startWidth: number;
  startHeight: number;
}

interface PendingResize {
  width: number;
  height: number;
  x: number;
  y: number;
  reposition: boolean;
}

// Tauri's window API has no single call that sets size and position
// atomically — only separate `setSize`/`setPosition`. For a drag that
// needs both (any corner/edge involving West or North, which move the
// window's origin as well as its size), applying them as two independent
// calls means there's always a moment where only one has landed — the
// window renders with the *old* position and *new* size (or vice versa),
// which briefly overshoots or undershoots the anchored edge before the
// other call catches up. At 60fps (once per animation frame) that happens
// on every single frame, which is what reads as continuous jitter rather
// than an occasional glitch.
//
// Two mitigations: apply position before size each time (so by the time
// size lands, the window's origin is already correct — better than the
// reverse, which would render the un-moved edge overshooting past its
// target), and throttle to a fixed minimum interval rather than trying to
// keep up with every animation frame, so the native resize call actually
// has time to finish before the next one is dispatched instead of queuing
// up a backlog.
const MIN_APPLY_INTERVAL_MS = 32;

function ResizeHandles() {
  const dragRef = useRef<ResizeDragState | null>(null);
  const pendingRef = useRef<PendingResize | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastAppliedAtRef = useRef(0);
  const applyInFlightRef = useRef(false);

  async function applyPending(pending: PendingResize) {
    applyInFlightRef.current = true;
    lastAppliedAtRef.current = performance.now();
    try {
      const win = getCurrentWindow();
      if (pending.reposition) {
        await win.setPosition(new LogicalPosition(pending.x, pending.y));
      }
      await win.setSize(new LogicalSize(pending.width, pending.height));
    } finally {
      applyInFlightRef.current = false;
    }
  }

  function scheduleApply() {
    if (rafRef.current !== null) return;
    const tick = () => {
      rafRef.current = null;
      const pending = pendingRef.current;
      if (!pending) return;

      const sinceLastApply = performance.now() - lastAppliedAtRef.current;
      if (applyInFlightRef.current || sinceLastApply < MIN_APPLY_INTERVAL_MS) {
        // Not ready yet — the previous update is still landing, or it's too
        // soon since the last one. Check again next frame rather than
        // dropping this pending value.
        rafRef.current = requestAnimationFrame(tick);
        return;
      }

      void applyPending(pending);
    };
    rafRef.current = requestAnimationFrame(tick);
  }

  async function handlePointerDown(direction: ResizeDirection, e: React.PointerEvent<HTMLDivElement>) {
    e.preventDefault();
    const win = getCurrentWindow();
    const [scale, size, position] = await Promise.all([win.scaleFactor(), win.innerSize(), win.outerPosition()]);
    dragRef.current = {
      direction,
      startPointerX: e.screenX,
      startPointerY: e.screenY,
      startX: position.x / scale,
      startY: position.y / scale,
      startWidth: size.width / scale,
      startHeight: size.height / scale,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function handlePointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag) return;

    const dx = e.screenX - drag.startPointerX;
    const dy = e.screenY - drag.startPointerY;

    let width = drag.startWidth;
    let height = drag.startHeight;
    let x = drag.startX;
    let y = drag.startY;

    // Each direction that includes an edge clamps that dimension, then (for
    // the edges that move the window's origin — West/North) re-derives the
    // position from the *clamped* size so the opposite edge stays perfectly
    // anchored even once the size hits its min/max, instead of drifting.
    if (drag.direction.includes("East")) {
      width = clamp(drag.startWidth + dx, MIN_PANEL_SIZE.width, MAX_PANEL_SIZE.width);
    }
    if (drag.direction.includes("West")) {
      width = clamp(drag.startWidth - dx, MIN_PANEL_SIZE.width, MAX_PANEL_SIZE.width);
      x = drag.startX + drag.startWidth - width;
    }
    if (drag.direction.includes("South")) {
      height = clamp(drag.startHeight + dy, MIN_PANEL_SIZE.height, MAX_PANEL_SIZE.height);
    }
    if (drag.direction.includes("North")) {
      height = clamp(drag.startHeight - dy, MIN_PANEL_SIZE.height, MAX_PANEL_SIZE.height);
      y = drag.startY + drag.startHeight - height;
    }

    // Once a dimension is clamped at its min/max, further cursor movement in
    // that same direction keeps producing pointer-move events but no actual
    // change in the result — the cursor just keeps moving further past
    // where the edge stopped responding. Skip re-applying (and re-scheduling
    // an IPC round trip for) a value that's identical to what's already
    // pending/applied, rather than continuing to act on every move event
    // even though nothing about the target size/position actually changed.
    const current = pendingRef.current;
    if (current && current.width === width && current.height === height && current.x === x && current.y === y) {
      return;
    }

    pendingRef.current = {
      width,
      height,
      x,
      y,
      reposition: drag.direction.includes("West") || drag.direction.includes("North"),
    };
    scheduleApply();
  }

  function handlePointerUp(e: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    // Apply whatever the last recorded position was immediately, rather
    // than dropping it — otherwise a drag that ends mid-frame could settle
    // one update behind where the cursor actually stopped.
    const pending = pendingRef.current;
    if (pending) void applyPending(pending);
    e.currentTarget.releasePointerCapture(e.pointerId);
  }

  return (
    <>
      {RESIZE_HANDLES.map(({ direction, className }) => (
        <div
          key={direction}
          onPointerDown={(e) => handlePointerDown(direction, e)}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          className={`absolute z-10 ${className}`}
        />
      ))}
    </>
  );
}

// Only ever called with real tool-call steps — the "Thinking" pseudo-step
// is filtered out and rendered separately (see the turn-rendering block
// below, and its comment explaining why).
function StepLine({ step }: { step: TaskStep }) {
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

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-2.5 py-1 text-xs font-medium tracking-tight transition duration-200 ${
        active
          ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
          : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
      }`}
    >
      {children}
    </button>
  );
}

function sessionDotColor(session: Session): string {
  const turn = session.turns[session.turns.length - 1];
  if (turn?.error) return "bg-red-400";
  if (turn?.result) return "bg-emerald-400";
  return "bg-[#4f8dff]";
}

function SessionChip({
  session,
  active,
  onClick,
  onClose,
}: {
  session: Session;
  active: boolean;
  onClick: () => void;
  onClose: () => void;
}) {
  const turn = session.turns[session.turns.length - 1];
  const running = turn && !turn.result && !turn.error;
  // Labeled with the *first* turn's instruction so the chip reads as a
  // stable conversation title instead of relabeling itself every message.
  const title = session.turns[0]?.instruction ?? "";
  return (
    <span
      className={`liquid-glass-subtle flex shrink-0 items-center gap-1 rounded-full pl-2.5 pr-1 py-1 text-xs transition duration-200 ${
        active
          ? "text-neutral-100 [border-color:rgba(79,141,255,0.45)] [box-shadow:inset_0_1px_0_0_rgba(255,255,255,0.2)]"
          : "text-neutral-400 hover:text-neutral-100"
      }`}
    >
      <button onClick={onClick} title={title} className="flex items-center gap-1.5">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${sessionDotColor(session)} ${running ? "animate-daimon-pulse" : ""}`} />
        <span className="max-w-[9rem] truncate">{title}</span>
      </button>
      <button
        onClick={onClose}
        title="close session"
        className="rounded-full px-1 text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90"
      >
        ×
      </button>
    </span>
  );
}

export function PipelinePanel({
  sessions,
  activeSession,
  onSelectSession,
  onNewSession,
  onCloseSession,
  onCollapse,
  onSubmit,
  pendingDraft,
  onConsumePendingDraft,
  dictationState = "idle",
  dictationLocked = false,
  dictationMessage,
  view,
  onViewChange,
  terminalTabs,
  activeTerminalId,
  onSelectTerminalTab,
  onOpenTerminalTab,
  onCloseTerminalTab,
  pendingTerminalInput,
  onConsumePendingTerminalInput,
  onDictateToTerminal,
  initialTerminalCommands,
}: {
  sessions: Session[];
  activeSession: Session | null;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onCloseSession: (sessionId: string) => void;
  onCollapse: () => void;
  onSubmit: (instruction: string) => void;
  pendingDraft?: PendingDraft | null;
  onConsumePendingDraft?: () => void;
  dictationState?: DictationState;
  dictationLocked?: boolean;
  dictationMessage?: string;
  // Also lifted to App.tsx, same reasoning as the terminal-tab props below —
  // which tab (chat/vault/automations/terminal/settings) is showing needs to
  // survive a collapse-to-pill cycle too. It used to reset to "chat" on
  // every single collapse/expand (since this whole component unmounts on
  // collapse and `view` was a plain local useState with "chat" as its
  // initial value), which meant collapsing while on, say, the terminal tab
  // and reopening always dumped the user back on chat instead of wherever
  // they actually left off.
  view: View;
  onViewChange: (view: View) => void;
  // Lifted up to App.tsx, same reasoning as `sessions` above: this
  // component fully unmounts on collapse-to-pill (see App.tsx's
  // `expanded ? <PipelinePanel/> : <Pill/>`), so any state that needs to
  // survive a collapse/expand cycle — including which terminal tabs exist
  // and are still running — can't live here. It used to, and the very bug
  // this comment is explaining was the direct, reported result: collapsing
  // while `claude` was running inside a terminal tab looked exactly like
  // the tab (and the shell process it was pointing at) had been closed,
  // since the frontend forgot the tab's id existed and could never
  // reconnect to it again, even though the backend shell was likely still
  // alive and simply orphaned.
  terminalTabs: string[];
  activeTerminalId: string | null;
  onSelectTerminalTab: (id: string) => void;
  onOpenTerminalTab: () => void;
  onCloseTerminalTab: (id: string) => void;
  pendingTerminalInput?: PendingInput | null;
  onConsumePendingTerminalInput?: () => void;
  onDictateToTerminal: (input: PendingInput) => void;
  // Keyed by terminal tab id — a one-shot command staged by the agent (via a
  // `ui_action` session-status event) for a tab it just opened, typed into
  // that tab's PTY once ready but never auto-submitted. Lives in App.tsx for
  // the same collapse-survival reason as `terminalTabs` etc. above.
  initialTerminalCommands: Record<string, string>;
}) {
  const [draft, setDraft] = useState("");
  const historyRef = useRef<HTMLDivElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grows the input with its content (up to MAX_DRAFT_INPUT_HEIGHT,
  // beyond which it scrolls internally) — this used to be a single-line
  // <input>, which silently clipped anything longer than its width instead
  // of wrapping. That was especially bad for a dictated result: a long
  // transcription would visually look like "it didn't paste in" when it
  // actually had, just invisibly off the right edge of a tiny fixed-height
  // field.
  const recalculateDraftHeight = useCallback(() => {
    const el = draftRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_DRAFT_INPUT_HEIGHT)}px`;
  }, []);

  useEffect(() => {
    recalculateDraftHeight();
  }, [draft, recalculateDraftHeight]);

  // The calculation above depends on the textarea's current *width* (a
  // narrower width wraps the same text into more lines, giving a taller
  // scrollHeight) — but it only re-ran on `draft` changes. The panel's own
  // expand animation (window.ts's animateTo) resizes the real native
  // window gradually over ~220ms, independently of React's render cycle —
  // if the height calculation happened to run while that was still
  // mid-flight (very plausible right when a Fn-triggered recording
  // auto-expands the panel: the textarea mounts and this effect fires
  // essentially immediately, while the window is still animating from
  // pill-size), it measured at a stale, too-narrow width and locked in an
  // inflated height with nothing ever correcting it afterward. A
  // ResizeObserver on the textarea recalculates whenever its actual
  // rendered width changes for any reason — the expand animation settling,
  // a manual resize-drag, anything — closing that gap regardless of timing.
  useEffect(() => {
    const el = draftRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => recalculateDraftHeight());
    observer.observe(el);
    return () => observer.disconnect();
  }, [recalculateDraftHeight]);

  // Deferred one frame so it runs after the DOM actually shows the chat
  // view's textarea — it may not exist yet this same tick if a different
  // tab (vault/automations/settings) was showing when this fires.
  //
  // `el.focus()` alone only manages focus *within* this webview's own
  // document — it does nothing to make Daimon's actual OS window the
  // frontmost/key one. Dictation is fundamentally ambient: the whole point
  // of a hotkey trigger is that some *other* app very likely has real OS
  // focus at that moment (you're writing an email, browsing, whatever).
  // Without explicitly forcing real OS-level focus first, the textarea can
  // show a focus ring while every actual keystroke — Enter included — still
  // goes to whatever app the OS considers frontmost, which looks exactly
  // like "focus isn't working" even though the DOM technically did focus
  // the right element.
  //
  // Plain `getCurrentWindow().setFocus()` (the JS API) turned out not to be
  // enough on its own: Daimon runs with `ActivationPolicy::Accessory` on
  // macOS (no Dock icon/Cmd+Tab entry, so it stays out of normal app
  // switching), and accessory-policy apps can have their *window* become
  // nominally key without the *application* itself ever becoming genuinely
  // active — keyboard routing depends on the latter. `activateAndFocusWindow`
  // (a Tauri command, see `src-tauri/src/window_focus.rs`) does the extra
  // `NSApplication.activate()` step this needs, in addition to bringing the
  // window itself forward.
  async function focusDraftAtEnd() {
    // Awaited, not fire-and-forget — this is an async IPC round trip to the
    // OS. The previous version raced it against a same-frame
    // `requestAnimationFrame`, which very likely won every time (a rAF
    // callback fires far sooner than an IPC call resolves), meaning
    // `el.focus()` was probably running *before* the OS had actually
    // granted the window real focus — a DOM focus a webview may not fully
    // honor if its own window isn't key yet.
    await activateAndFocusWindow();
    requestAnimationFrame(() => {
      const el = draftRef.current;
      if (!el) return;
      el.focus();
      // Cursor at the end, not wherever focusing a filled field happens to
      // default to — ready to just hit enter, or keep typing.
      el.setSelectionRange(el.value.length, el.value.length);
    });
  }

  // The moment a recording *ends* (transcription starts), switch to chat
  // and focus the input — regardless of what happens next (real text
  // arrives, it turns out to be silence, or it errors out). Dictating is
  // meant to end with you either continuing to type or hitting enter to
  // send, without needing to click into the field first; waiting for the
  // eventual outcome to decide that would leave the input unfocused for
  // however long transcription takes, and wouldn't focus it at all on a
  // silent/failed recording.
  useEffect(() => {
    if (dictationState !== "transcribing") return;
    // Stay put if the terminal tab is what's currently open — the recording
    // indicator in the header is already visible regardless of active tab,
    // so there's nothing lost by not force-switching away, and
    // focusDraftAtEnd targets the chat textarea specifically, which isn't
    // even mounted while another tab is showing.
    if (view === "terminal") return;
    onViewChange("chat");
    focusDraftAtEnd();
  }, [dictationState, view]);

  // A transcription result should always land somewhere the user can see and
  // edit it before submitting — even if the panel happened to be sitting on
  // a different tab (vault/automations/settings) when it arrived, so switch
  // back to chat here rather than silently dropping it into a hidden input.
  useEffect(() => {
    if (!pendingDraft) return;
    // An empty/whitespace-only transcription (silence, a false-triggered
    // recording that captured nothing meaningful) has nothing worth showing
    // — skip filling the input rather than clobbering whatever the user may
    // have already been typing with a blank value. (Focus already moved
    // here when transcribing started, via the effect above.)
    if (pendingDraft.text.trim()) {
      if (view === "terminal") {
        // Route into the live PTY instead, and stay right where the user
        // is — watching dictated text land in the terminal — rather than
        // switching them away from the tab they were just on.
        onDictateToTerminal({ text: pendingDraft.text, id: pendingDraft.id });
      } else {
        setDraft(pendingDraft.text);
        onViewChange("chat");
        focusDraftAtEnd();
      }
    }
    onConsumePendingDraft?.();
  }, [pendingDraft, onConsumePendingDraft, view, onDictateToTerminal]);

  const activeTurns = activeSession?.turns ?? [];
  const lastTurn = activeTurns[activeTurns.length - 1];
  const totalStepCount = activeTurns.reduce((sum, t) => sum + t.steps.length, 0);
  // The agent server threads conversation history across messages in a
  // session (Phase 8) via one shared, growing array per container — sending
  // a second message before the first turn resolves would race two turns
  // against that same array. Turns within a session are sequential by
  // design, so block submission until the active session's last turn has a
  // result or error (starting a brand-new session is unaffected — nothing to
  // race there).
  const activeTurnInFlight = Boolean(activeSession && lastTurn && !lastTurn.result && !lastTurn.error);
  // Drives the "screen" tab's live view — see ScreenPanel.tsx. Only
  // meaningful while the active session's last turn is actually running;
  // once it completes, `applyToTurn` (sessionEvents.ts) clears `liveFrame`
  // itself, so there's nothing extra to reset here.
  const isLive = activeTurnInFlight && Boolean(lastTurn?.liveFrame);

  useEffect(() => {
    historyRef.current?.scrollTo({ top: historyRef.current.scrollHeight, behavior: "smooth" });
  }, [activeSession?.id, activeTurns.length, totalStepCount, lastTurn?.result, lastTurn?.error]);

  function submitDraft() {
    if (activeTurnInFlight) return;
    const instruction = draft.trim();
    if (!instruction) return;
    onSubmit(instruction);
    setDraft("");
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    submitDraft();
  }

  // Enter sends (matching a plain <input>'s behavior, which this replaces);
  // Shift+Enter inserts a real newline instead — the standard chat-input
  // convention, and necessary now that the field is a multi-line textarea.
  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitDraft();
    }
  }

  // There must always be a way to start a second session once one exists —
  // so show the chip row (with its trailing "+") whenever any session is
  // open, not just once there are already multiple.
  const showChipRow = view === "chat" && sessions.length > 0;

  return (
    // No p-6 margin — the panel fills the window edge-to-edge so its visible
    // rounded rectangle aligns exactly with the native vibrancy material (see
    // vibrancy.rs). The soft outer glow that margin used to provide is gone,
    // traded for the dynamic native glass.
    <div className="relative flex h-full w-full items-center justify-center">
      <ResizeHandles />
      <div className="animate-daimon-in liquid-glass flex h-full w-full flex-col overflow-hidden rounded-[28px] text-white">
        <div
          onMouseDown={(e) => {
            // Lets the header double as a drag handle — this window has no
            // native title bar to provide that for free (same reason the
            // resize handles above exist). Skip starting a drag if the
            // mousedown actually landed on a button, so tab-switching and
            // collapsing still work as plain clicks.
            if ((e.target as HTMLElement).closest("button")) return;
            getCurrentWindow().startDragging();
          }}
          className="flex shrink-0 items-center gap-2 border-b border-white/[0.08] px-4 py-3 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.06)]"
        >
          <img src="/logo.svg" alt="" draggable={false} className="h-5 w-5 invert" />
          <span className="text-sm font-semibold tracking-tight text-neutral-50">daimon</span>
          <span className="h-1.5 w-1.5 animate-daimon-pulse rounded-full bg-[#4f8dff]" />
          {dictationState === "recording" && (
            <span className="flex items-center gap-1.5 font-mono text-xs text-[#4f8dff]">
              <SoundWave barHeight={10} />
              {dictationLocked && (
                // Two small static dots — visibly steadier than the moving
                // wave alone, signaling "hands-free, keeps going until you
                // press fn again" rather than "only while held."
                <span className="flex items-center gap-1">
                  <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
                  <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
                </span>
              )}
            </span>
          )}
          {dictationState === "transcribing" && (
            <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
          )}
          {dictationState === "error" && (
            <span className="text-xs text-red-400">{dictationMessage || "dictation error"}</span>
          )}
          <div className="flex-1" />
          <TabButton active={view === "chat"} onClick={() => onViewChange("chat")}>
            chat
          </TabButton>
          <TabButton active={view === "vault"} onClick={() => onViewChange("vault")}>
            vault
          </TabButton>
          <TabButton active={view === "automations"} onClick={() => onViewChange("automations")}>
            automations
          </TabButton>
          <TabButton
            active={view === "terminal"}
            onClick={() => {
              // Lazily open the first terminal tab the first time this view
              // is ever opened at all — a plain launch (or switching among
              // chat/vault/automations/settings) never eagerly spawns a host
              // shell process nobody asked for.
              if (terminalTabs.length === 0) onOpenTerminalTab();
              onViewChange("terminal");
            }}
          >
            terminal
          </TabButton>
          <TabButton active={view === "screen"} onClick={() => onViewChange("screen")}>
            {isLive ? "screen ●" : "screen"}
          </TabButton>
          <TabButton active={view === "settings"} onClick={() => onViewChange("settings")}>
            settings
          </TabButton>
          <button
            onClick={onCollapse}
            className="rounded-full px-2 py-1 text-xs font-medium text-neutral-400 transition duration-200 hover:bg-white/5 hover:text-neutral-100 active:scale-90"
          >
            collapse
          </button>
        </div>

        {showChipRow && (
          <div className="themed-scroll flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-white/[0.08] px-3 py-2">
            {sessions.map((s) => (
              <SessionChip
                key={s.id}
                session={s}
                active={s.id === activeSession?.id}
                onClick={() => onSelectSession(s.id)}
                onClose={() => onCloseSession(s.id)}
              />
            ))}
            <button
              onClick={onNewSession}
              title="start a new session"
              className="liquid-glass-subtle flex shrink-0 items-center justify-center rounded-full px-2 py-1 text-xs text-neutral-400 transition duration-200 hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)] active:scale-90"
            >
              +
            </button>
          </div>
        )}

        {/* Same chip-row pattern as chat sessions above — multiple
            concurrent terminal tabs, each its own real shell process. Only
            shown while the terminal view itself is active, same as the
            session chip row only showing on the chat view. */}
        {view === "terminal" && (
          <div className="themed-scroll flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-white/[0.08] px-3 py-2">
            {terminalTabs.map((id, index) => (
              <span
                key={id}
                className={`liquid-glass-subtle flex shrink-0 items-center gap-1 rounded-full pl-2.5 pr-1 py-1 text-xs transition duration-200 ${
                  id === activeTerminalId
                    ? "text-neutral-100 [border-color:rgba(79,141,255,0.45)] [box-shadow:inset_0_1px_0_0_rgba(255,255,255,0.2)]"
                    : "text-neutral-400 hover:text-neutral-100"
                }`}
              >
                <button onClick={() => onSelectTerminalTab(id)} className="flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-[#4f8dff]" />
                  terminal {index + 1}
                </button>
                <button
                  onClick={() => onCloseTerminalTab(id)}
                  title="close terminal"
                  className="rounded-full px-1 text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90"
                >
                  ×
                </button>
              </span>
            ))}
            <button
              onClick={onOpenTerminalTab}
              title="open a new terminal"
              className="liquid-glass-subtle flex shrink-0 items-center justify-center rounded-full px-2 py-1 text-xs text-neutral-400 transition duration-200 hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)] active:scale-90"
            >
              +
            </button>
          </div>
        )}

        {view === "settings" ? (
          <Settings />
        ) : view === "vault" ? (
          <VaultPanel />
        ) : view === "automations" ? (
          <AutomationsPanel />
        ) : view === "screen" ? (
          <ScreenPanel isLive={isLive} liveFrame={lastTurn?.liveFrame} />
        ) : view === "chat" ? (
          <div ref={historyRef} className="themed-scroll min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
            {!activeSession && (
              <p className="text-sm text-neutral-500">No active session — give daimon something to do.</p>
            )}
            {activeSession &&
              activeSession.turns.map((turn) => {
                // The "Thinking" pseudo-step is always emitted *first*, at
                // the very start of the turn (see run.ts) — so it always
                // lands at index 0 of `turn.steps`, with every real tool-call
                // step appended *after* it. Rendered in plain array order,
                // that pins the loading indicator at the *top* of a list
                // that keeps growing underneath it, while auto-scroll keeps
                // the viewport pinned to the newest (bottom) tool step —
                // so the one thing that tells you it's still working
                // scrolls out of view as soon as a couple of tool calls
                // happen. Splitting it out and rendering it separately,
                // after the tool-step list, keeps it anchored at the
                // bottom — exactly where the eventual result will appear —
                // regardless of how many steps pile up above it.
                const thinking = turn.steps.find((s) => s.label === "Thinking");
                const toolSteps = turn.steps.filter((s) => s.label !== "Thinking");
                return (
                  <div key={turn.id} className="space-y-3">
                    <div className="flex justify-end">
                      <div className="max-w-[85%] rounded-2xl rounded-br-md border border-[#4f8dff]/30 bg-[#4f8dff]/[0.18] px-3.5 py-2 text-sm leading-relaxed text-neutral-50 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)] backdrop-blur-sm">
                        {turn.instruction}
                      </div>
                    </div>

                    <div className="flex justify-start">
                      <div className="liquid-glass-subtle max-w-[85%] space-y-2 rounded-2xl rounded-bl-md px-3.5 py-2.5">
                        {toolSteps.length > 0 && (
                          <ul className="space-y-1.5">
                            {toolSteps.map((step) => (
                              <li key={step.id}>
                                <StepLine step={step} />
                              </li>
                            ))}
                          </ul>
                        )}
                        {thinking?.status === "running" && <ThinkingIndicator />}
                        {/* whitespace-pre-wrap: the agent's result is plain
                            text with real newlines (see graph.ts's system
                            prompt — deliberately no markdown, since there's
                            no renderer here), but HTML collapses \n by
                            default. Without this, multi-line answers rendered
                            as one run-on paragraph regardless of how the
                            agent actually formatted them. */}
                        {turn.result && (
                          <p className="whitespace-pre-wrap text-sm leading-relaxed text-white">{turn.result}</p>
                        )}
                        {turn.error && (
                          <p className="whitespace-pre-wrap text-sm leading-relaxed text-red-400">{turn.error}</p>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
          </div>
        ) : null}

        {/* One TerminalPanel per open tab, each kept mounted (just hidden)
            once created rather than unmounted like the other views above —
            tearing down a tab's xterm instance every time it's not the
            frontmost one would throw away its scrollback even though the
            underlying host shell process itself is unaffected either way.
            Only the tab that's both the active *view* and the active
            *terminal id* is actually visible/receiving dictation input. */}
        {terminalTabs.map((id) => {
          const isVisible = view === "terminal" && id === activeTerminalId;
          return (
            <div key={id} className={isVisible ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
              <TerminalPanel
                id={id}
                active={isVisible}
                pendingInput={id === activeTerminalId ? pendingTerminalInput : null}
                onConsumePendingInput={onConsumePendingTerminalInput}
                initialCommand={initialTerminalCommands[id]}
              />
            </div>
          );
        })}

        {view === "chat" && (
          <form
            onSubmit={handleSubmit}
            className="m-3 flex shrink-0 items-start gap-2 rounded-2xl border border-white/10 bg-white/[0.04] px-3 py-2.5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.08)] backdrop-blur-md transition focus-within:border-[#4f8dff]/40 focus-within:bg-white/[0.06]"
          >
            <span className="pt-0.5 font-mono text-sm text-[#4f8dff]">{">"}</span>
            <textarea
              ref={draftRef}
              autoFocus
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={activeTurnInFlight}
              placeholder={activeTurnInFlight ? "waiting for daimon to finish this turn…" : "tell daimon what to do..."}
              className="themed-scroll w-full resize-none overflow-y-auto bg-transparent pt-0.5 font-mono text-sm leading-relaxed text-neutral-100 placeholder:text-neutral-600 focus:outline-none disabled:opacity-40"
            />
          </form>
        )}
      </div>
    </div>
  );
}
