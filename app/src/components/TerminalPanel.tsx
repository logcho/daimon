import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { bus, onBusControl, onBusReconnect, onTermData } from "../lib/bus";
import { decodeBase64 } from "../lib/busClient";
import { setInsertTarget, clearInsertTarget } from "../lib/voice";

// Matches the app's one dark/glass theme (single #4f8dff accent) rather than
// xterm's default black-on-white — this is a real shell prompt embedded in
// Daimon's own panel, not a separate widget with its own visual identity.
//
// `background` is fully transparent, so the panel's native vibrancy glass
// (and the live desktop blurred behind it) shows straight through behind the
// terminal text — the terminal reads as the same glass surface as the rest
// of the panel, not a tinted slab over it. Requires `allowTransparency: true`
// on the Terminal (set below); without that flag xterm forces the background
// fully opaque regardless of the alpha here. If shell text ever proves hard
// to read against a busy desktop, the tradeoff dial is this alpha — nudge it
// up toward an opaque dark to reintroduce a legibility backing.
const TERMINAL_THEME = {
  background: "rgba(0, 0, 0, 0)",
  foreground: "#e5e5e5",
  cursor: "#4f8dff",
  cursorAccent: "#0a0a0a",
  selectionBackground: "rgba(79, 141, 255, 0.35)",
  black: "#171717",
  red: "#f87171",
  green: "#34d399",
  yellow: "#facc15",
  blue: "#4f8dff",
  magenta: "#c084fc",
  cyan: "#22d3ee",
  white: "#e5e5e5",
  brightBlack: "#525252",
  brightRed: "#f87171",
  brightGreen: "#34d399",
  brightYellow: "#facc15",
  brightBlue: "#4f8dff",
  brightMagenta: "#c084fc",
  brightCyan: "#22d3ee",
  brightWhite: "#fafafa",
};

// The Kitty keyboard-protocol reply used to live here: xterm.js never answers
// `CSI ? u`, and Claude Code's CLI sends it at startup to decide whether it can
// enable its richer UI, so this component synthesized the answer.
//
// It now lives in the server's pty service instead, and had to: with the
// desktop app and a phone both attached to one shell, each of them answering
// would write two replies into the same pty. The answer has to happen exactly
// once, which means it has to happen where the pty is.

// Never fit a terminal that isn't on screen.
//
// FitAddon sizes the grid from `getComputedStyle(container)`, and a
// `display:none` element has no box, so its computed height/width come back as
// the *specified* `100%` rather than a resolved pixel value. `parseInt("100%")`
// is 100 — not NaN — so FitAddon's own NaN guard doesn't catch it and it
// happily proposes a ~13x5 grid. The tab-switch path then reflowed xterm's
// buffer to 13 columns and resized the real PTY to match, so the shell (or a
// TUI running in it) re-wrapped its output to a sliver. Switching back fitted
// the true size again, but the damage was already in the scrollback and a TUI
// only redraws on its *next* resize — which is why it took a manual window
// resize to come back.
//
// clientWidth/clientHeight are the honest test: exactly 0 when there is no box.
function hasLayoutBox(element: HTMLElement | null): boolean {
  return !!element && element.clientWidth > 0 && element.clientHeight > 0;
}

export function TerminalPanel({
  id,
  active,
  initialCommand,
}: {
  id: string;
  active: boolean;
  // One-shot command staged by the agent (via a `ui_action` event, see
  // App.tsx) for a freshly-opened tab — typed into the PTY once it's ready
  // to receive input, but never executed on the user's behalf; they review
  // and press Enter themselves. Read only inside this component's mount
  // effect below, intentionally not part of that effect's dependency array
  // (see the comment there) — same "apply once at creation" treatment `id`
  // itself already gets.
  initialCommand?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const [exitCode, setExitCode] = useState<number | null | undefined>(undefined);

  async function handleRestart() {
    setExitCode(undefined);
    const term = termRef.current;
    // The server drops an exited terminal's entry, so opening the same id
    // spawns a fresh shell rather than finding the dead one.
    await bus().send("term.open", { id, cols: term?.cols ?? 80, rows: term?.rows ?? 24 });
    await bus().send("term.attach", { id, cols: term?.cols ?? 80, rows: term?.rows ?? 24 });
  }

  /** Fit the grid to the container and tell the PTY — but only when there is
   *  a real box to measure (see hasLayoutBox). Returns whether it ran, so
   *  callers can tell "sized" from "deferred until this tab is visible". */
  const refit = useCallback(() => {
    const term = termRef.current;
    const fitAddon = fitAddonRef.current;
    if (!term || !fitAddon || !hasLayoutBox(containerRef.current)) return false;
    // Re-measure the character cell before fitting. A terminal that was opened
    // (or last measured) while off screen has a zero-size cell, and FitAddon
    // refuses to propose dimensions from one — silently, so the grid stayed at
    // the 80x24 default however big the container actually was. Resizing to
    // the size it already has is xterm's own documented no-op path, and it
    // re-measures when the cell size isn't valid.
    term.resize(term.cols, term.rows);
    fitAddon.fit();
    // Fire-and-forget, and never let a transport failure escape: this runs
    // from a ResizeObserver callback and a rAF, and an exception there would
    // skip whatever the caller does next (the repaint and focus below). The
    // fit itself has already happened, which is the part the user sees.
    //
    // Note the pty may not end up this size: it takes the smallest size of
    // everything attached, so a phone watching the same shell wins. That is
    // deliberate — see the server's terminal service.
    bus().post("term.resize", { id, cols: term.cols, rows: term.rows });
    return true;
  }, [id]);

  // Mount the xterm instance once and subscribe to the PTY event stream.
  // `id` *is* listed as a dependency, but only for lint-correctness (it's
  // read inside the effect) — it never actually changes for a mounted
  // instance, since the panel renders one `TerminalPanel` per open tab keyed
  // by `id` in a React list, so a new id always means a whole new component
  // instance, never a prop update on an existing one.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const term = new Terminal({
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 13,
      cursorBlink: true,
      theme: TERMINAL_THEME,
      allowProposedApi: true,
      // Lets TERMINAL_THEME.background's alpha actually take effect (xterm
      // forces an opaque background otherwise) so the terminal renders as a
      // glass surface over the panel. Carries a documented render-perf cost,
      // negligible for an interactive shell at this size.
      allowTransparency: true,
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(container);

    termRef.current = term;
    fitAddonRef.current = fitAddon;
    refit();

    const dataDisposable = term.onData((data) => {
      // Fire-and-forget rather than awaited: a round trip per keystroke would
      // put the whole latency of the socket between typing and seeing the
      // character echoed back.
      bus().post("term.input", { id, data });
    });

    // The bus multiplexes every terminal over one socket, so route by id —
    // the same reason the Tauri event handler filtered by id before.
    const unlistenData = onTermData(id, (bytes) => term.write(bytes));

    const unlistenControl = onBusControl((frame) => {
      if (frame.id !== id) return;
      if (frame.control === "term_exited") {
        setExitCode((frame.code ?? null) as number | null);
      } else if (frame.control === "term_snapshot") {
        // Scrollback for a shell that has been running without us — on first
        // attach, and again after a reconnect. Reset first: this is a fresh
        // rendering of the whole buffer, not a continuation of what is on
        // screen, and appending it would duplicate everything.
        term.reset();
        term.write(decodeBase64(String(frame.data ?? "")));
        setExitCode(frame.exited ? ((frame.code ?? null) as number | null) : undefined);
      }
    });

    // Open, then attach: `term.open` is idempotent per id, so a remount finds
    // the shell that is already running instead of spawning a second one. That
    // is the whole point of terminals living in the server — this panel can be
    // torn down and rebuilt, or the app restarted, without the shell noticing.
    //
    // Awaited in sequence: a staged command must not be written before the
    // attach that will show its output.
    async function bootTerminal() {
      try {
        await bus().send("term.open", { id, cols: term.cols, rows: term.rows });
        await bus().send("term.attach", { id, cols: term.cols, rows: term.rows });
      } finally {
        // Fit even if the spawn failed. The grid is local state — leaving it
        // at the 80x24 placeholder in a full-size window makes a terminal that
        // merely failed to start look broken as well.
        refit();
      }
      if (initialCommand) {
        // No trailing newline, ever: the user reads it and presses Enter
        // (stage-don't-execute).
        bus().post("term.input", { id, data: initialCommand });
      }
    }
    void bootTerminal();

    // A dropped socket means the server no longer has us attached, so
    // re-attach rather than assuming the stream simply resumes. The snapshot
    // that comes back repaints whatever happened while we were away.
    const unlistenReconnect = onBusReconnect(() => {
      void bus().send("term.attach", { id, cols: term.cols, rows: term.rows });
    });

    // Debounced — fires on every container size change, but the PTY
    // resize is only issued once the size has settled (50ms trailing).
    // Without this, the 220ms panel expand/collapse animation produces a
    // resize call on every animation frame, which races with the TUI's
    // render cycle inside the terminal.
    let resizeTimer: ReturnType<typeof setTimeout> | undefined;
    const resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(refit, 50);
    });
    resizeObserver.observe(container);

    return () => {
      // Detach, do not close: the shell keeps running. Closing a tab used to
      // be the only way a terminal ended, because it lived in the window.
      bus().post("term.detach", { id });
      unlistenData();
      unlistenControl();
      unlistenReconnect();
      dataDisposable.dispose();
      clearTimeout(resizeTimer);
      resizeObserver.disconnect();
      term.dispose();
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, [id, refit]);

  // The panel keeps this component mounted-but-hidden (`display:none`) once
  // opened, rather than unmounting it on every tab switch, so scrollback
  // survives. Three consequences of that:
  //
  // (1) xterm.js's own hidden `<textarea>` — the actual DOM element that
  //     receives keystrokes — never gets real focus just from being the active
  //     tab; without an explicit `term.focus()` here, typing after switching to
  //     this tab can silently go nowhere.
  // (2) nothing measured the container while it was hidden (refit refuses to,
  //     see hasLayoutBox), so if the window was resized in the meantime this is
  //     the first chance to match the grid to it.
  // (3) xterm pauses rendering whenever its element isn't intersecting the
  //     viewport, and only repaints when its own IntersectionObserver fires —
  //     a separate task that lands after this effect. `refresh` repaints the
  //     rows now, so the tab is correct on the frame it appears instead of a
  //     beat later.
  //
  // Deferred a frame: this effect runs in the same commit that flips
  // `display`, and the container needs to have been laid out before it can be
  // measured.
  useEffect(() => {
    if (!active) return;
    const frame = requestAnimationFrame(() => {
      const term = termRef.current;
      if (!term) return;
      refit();
      term.refresh(0, term.rows - 1);
      term.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [id, active, refit]);

  // Register this terminal as the dictation insert target when active, so
  // Fn-key transcription lands in the shell's readline buffer (unexecuted —
  // the user presses Enter to run it).
  useEffect(() => {
    const term = termRef.current;
    if (!active || !term) return;
    setInsertTarget({ kind: "terminal", paste: (text: string) => {
      void term.paste(text);
    }});
    return () => {
      clearInsertTarget();
    };
  }, [id, active]);

  return (
    // No opaque background on this container — the terminal is transparent
    // (see TERMINAL_THEME/allowTransparency) so the panel's liquid glass
    // shows through behind the text, making the terminal read as another
    // glass surface rather than a solid slab.
    <div className="relative min-h-0 flex-1 overflow-hidden">
      {/* Visual padding wrapper — keeps 12px breathing room without
          affecting FitAddon's measurements (it measures containerRef,
          which is padding-free, so PTY cols/rows match the real grid). */}
      <div className="h-full w-full p-3">
        <div ref={containerRef} className="h-full w-full" />
      </div>
      {exitCode !== undefined && (
        <div className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-3 border-t border-white/10 bg-neutral-950/70 px-4 py-2.5 backdrop-blur-xl">
          <span className="text-xs text-neutral-300">
            shell exited{exitCode !== null ? ` (code ${exitCode})` : ""}
          </span>
          <button
            onClick={handleRestart}
            className="liquid-glass-subtle rounded-full px-3 py-1 text-xs font-medium text-neutral-100 transition duration-200 hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)] active:scale-95"
          >
            restart
          </button>
        </div>
      )}
    </div>
  );
}
