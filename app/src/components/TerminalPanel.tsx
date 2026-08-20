import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { onTerminalExited, onTerminalOutput, resizeTerminal, startTerminal, writeToTerminal } from "../api";
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

function decodeBase64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

// Escape sequences are always plain ASCII control/printable bytes, so a
// straight byte->char mapping is safe here even though the underlying
// stream can otherwise contain arbitrary binary data — this string is only
// ever used for pattern-matching below, never rendered or fed back into
// xterm (the original `bytes` still go to `term.write()` untouched).
function bytesToAsciiString(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += String.fromCharCode(bytes[i]);
  }
  return out;
}

// xterm.js (as of the stable version this app uses — see package.json)
// doesn't implement the Kitty keyboard protocol: it never answers a
// terminal's `CSI ? u` capability query, which is exactly the query Claude
// Code's own CLI sends at startup to decide whether it's safe to enable its
// richer interactive UI. Kitty protocol support does exist in xterm.js, but
// only in a pre-release with its own documented compatibility bugs in
// exactly this app's runtime (macOS WKWebView via Tauri 2 — see
// xtermjs/xterm.js#5894), so upgrading isn't safe right now.
//
// This synthesizes the answer ourselves instead: watches every chunk of
// real PTY output for that exact query and, if seen, writes a standards-
// compliant reply (`CSI ? 0 u` — "yes, I understand this protocol; no
// enhancements currently active") straight back into the PTY, exactly as if
// a real Kitty-capable terminal had answered. Entirely local to this
// component; doesn't touch xterm.js's rendering at all, so it costs nothing
// if this particular query never shows up (e.g. plain shell use with no
// TUI asking).
const KITTY_KEYBOARD_QUERY = "\x1b[?u";
const KITTY_KEYBOARD_QUERY_RESPONSE = "\x1b[?0u";

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
    await startTerminal(id);
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
    Promise.resolve()
      .then(() => resizeTerminal(id, term.cols, term.rows))
      .catch(() => {});
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
      void writeToTerminal(id, data);
    });

    let cancelled = false;
    let unlistenOutput: (() => void) | undefined;
    let unlistenExited: (() => void) | undefined;

    // `onTerminalOutput`/`onTerminalExited` route every open terminal's
    // events through one subscription (see api.ts) — filter to this
    // instance's own `id` so a multi-tab session doesn't cross-wire output
    // between tabs.
    onTerminalOutput((payload) => {
      if (payload.id !== id) return;
      const bytes = decodeBase64ToBytes(payload.data);
      term.write(bytes);
      // See KITTY_KEYBOARD_QUERY's doc comment above — a real terminal that
      // understood this query would already have answered it as part of
      // reading the same bytes; xterm.js doesn't, so answer on its behalf
      // whenever it shows up in the raw output stream.
      if (bytesToAsciiString(bytes).includes(KITTY_KEYBOARD_QUERY)) {
        void writeToTerminal(id, KITTY_KEYBOARD_QUERY_RESPONSE);
      }
    }).then((fn) => {
      if (cancelled) fn();
      else unlistenOutput = fn;
    });

    onTerminalExited((payload) => {
      if (payload.id !== id) return;
      setExitCode(payload.code);
    }).then((fn) => {
      if (cancelled) fn();
      else unlistenExited = fn;
    });

    // Sequenced, not two independent fire-and-forget calls: `write_to_terminal`
    // errors with "no active terminal — start one first" if it lands before
    // this tab's `start_terminal` has actually finished spawning the PTY on
    // the Rust side. Awaiting `startTerminal` before ever issuing the staged
    // command guarantees the ordering regardless of which IPC round trip
    // would otherwise resolve first. The staged command carries no trailing
    // `\r` — the user presses Enter to run it (stage-don't-execute).
    //
    // After `startTerminal` returns, re-fit and re-resize: the initial
    // `fit()` above (and the ResizeObserver's first callback) can both race
    // the async PTY spawn and land before the terminal is registered — Rust's
    // `resize_terminal` silently drops calls for unknown ids, leaving the PTY
    // at the 80×24 placeholder until the container actually changes size.
    // Re-sending here guarantees the PTY dimensions match the real grid
    // before any TUI renders.
    async function bootTerminal() {
      try {
        await startTerminal(id);
      } finally {
        // Fit even if the spawn failed. The grid is local state — leaving it
        // at the 80x24 placeholder in a full-size window makes a terminal that
        // merely failed to start look broken as well.
        refit();
      }
      if (initialCommand) {
        await writeToTerminal(id, initialCommand);
      }
    }
    void bootTerminal();

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
      cancelled = true;
      unlistenOutput?.();
      unlistenExited?.();
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
