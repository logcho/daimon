import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import {
  activateAndFocusWindow,
  onTerminalExited,
  onTerminalOutput,
  resizeTerminal,
  startTerminal,
  writeToTerminal,
} from "../lib/api";
import type { PendingInput } from "../types";

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
// richer interactive UI (the bordered input box, slash-command menu, mode
// indicator — see PROMPT.md's Phase 11 section for how this was confirmed
// by reading the `claude` binary's own capability-negotiation code). Kitty
// protocol support does exist in xterm.js, but only in a pre-release with
// its own documented compatibility bugs in exactly this app's runtime
// (macOS WKWebView via Tauri 2 — see xtermjs/xterm.js#5894), so upgrading
// isn't safe right now.
//
// This synthesizes the answer ourselves instead: watches every chunk of
// real PTY output for that exact query and, if seen, writes a standards-
// compliant reply (`CSI ? 0 u` — "yes, I understand this protocol; no
// enhancements currently active", matching the shape Claude Code's own
// parser expects, `/^\x1b\[\?(\d+)u$/`) straight back into the PTY, exactly
// as if a real Kitty-capable terminal had answered. Entirely local to this
// component; doesn't touch xterm.js's rendering at all, so it costs nothing
// if this particular query never shows up (e.g. plain shell use with no
// TUI asking).
const KITTY_KEYBOARD_QUERY = "\x1b[?u";
const KITTY_KEYBOARD_QUERY_RESPONSE = "\x1b[?0u";

export function TerminalPanel({
  id,
  active,
  pendingInput,
  onConsumePendingInput,
  initialCommand,
}: {
  id: string;
  active: boolean;
  pendingInput?: PendingInput | null;
  onConsumePendingInput?: () => void;
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
  const lastConsumedInputIdRef = useRef<string | null>(null);
  const [exitCode, setExitCode] = useState<number | null | undefined>(undefined);

  async function handleRestart() {
    setExitCode(undefined);
    await startTerminal(id);
  }

  // Mount the xterm instance once and subscribe to the PTY event stream.
  // This effect intentionally has no dependency on `pendingInput` — that's
  // handled by its own effect below so a new dictation result doesn't
  // re-run this setup/teardown. `id` *is* listed as a dependency, but only
  // for lint-correctness (it's read inside the effect) — it never actually
  // changes for a mounted instance, since `PipelinePanel.tsx` renders one
  // `TerminalPanel` per open tab keyed by `id` in a React list, so a new id
  // always means a whole new component instance, never a prop update on an
  // existing one.
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
    fitAddon.fit();

    termRef.current = term;
    fitAddonRef.current = fitAddon;

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
    onTerminalOutput((eventId, dataBase64) => {
      if (eventId !== id) return;
      const bytes = decodeBase64ToBytes(dataBase64);
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

    onTerminalExited((eventId, code) => {
      if (eventId !== id) return;
      setExitCode(code);
    }).then((fn) => {
      if (cancelled) fn();
      else unlistenExited = fn;
    });

    // Sequenced, not two independent fire-and-forget calls: `write_to_terminal`
    // errors with "no active terminal — start one first" if it lands before
    // this tab's `start_terminal` has actually finished spawning the PTY on
    // the Rust side. Awaiting `startTerminal` before ever issuing the staged
    // command guarantees the ordering regardless of which IPC round trip
    // would otherwise resolve first.
    async function bootTerminal() {
      await startTerminal(id);
      if (initialCommand) {
        await writeToTerminal(id, initialCommand);
      }
    }
    void bootTerminal();

    // Fires on every container size change — the panel's own expand/collapse
    // animation and manual resize-drag both resize this container
    // independently of xterm's own render cycle, and neither one dispatches
    // a window-level resize event. A ResizeObserver is the only thing that
    // reliably catches all of those (same lesson already learned for the
    // chat textarea's auto-grow height — see PipelinePanel.tsx).
    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit();
      void resizeTerminal(id, term.cols, term.rows);
    });
    resizeObserver.observe(container);

    return () => {
      cancelled = true;
      unlistenOutput?.();
      unlistenExited?.();
      dataDisposable.dispose();
      resizeObserver.disconnect();
      term.dispose();
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, [id]);

  // `PipelinePanel` keeps this component mounted-but-hidden (`display:none`)
  // once opened, rather than unmounting it on every tab switch, so scrollback
  // survives. Two consequences of that: (1) xterm.js's own hidden `<textarea>`
  // — the actual DOM element that receives keystrokes, same underlying
  // mechanism as the chat input's focus bug this app already hit and fixed
  // once — never gets real focus just from being the active tab; nothing
  // clicks into it automatically the way switching to the chat tab doesn't
  // need to (that textarea has `autoFocus`). Without an explicit `term.focus()`
  // here, typing after switching to this tab can silently go nowhere. (2) a
  // `display:none` container measures as zero-size, so whatever `fit()`
  // computed while hidden (or before this was ever shown) is stale the
  // moment it becomes visible again — re-fit and re-report the real size
  // every time this tab actually becomes the active one, not just once at
  // mount, so the shell (and anything running inside it, like Claude Code's
  // own UI) is always working from an accurate terminal size rather than
  // whatever the hardcoded 80x24 placeholder or a pre-hide measurement left
  // it at.
  useEffect(() => {
    if (!active) return;
    const term = termRef.current;
    const fitAddon = fitAddonRef.current;
    if (!term || !fitAddon) return;
    fitAddon.fit();
    void resizeTerminal(id, term.cols, term.rows);
    // `term.focus()` alone only moves focus *within* this webview's own
    // DOM — the same gap `activateAndFocusWindow` exists for on the chat
    // input path (see `window_focus.rs`'s module doc). Clicking the
    // terminal tab button is an ordinary click inside an already-focused
    // window in the common case, so this is likely a no-op there — but the
    // dictation-into-terminal path can activate this tab from a state where
    // Daimon's window isn't the OS's real key/active window at all (same
    // as dictation into chat), and without this, `term.focus()` would set
    // `document.activeElement` inside a webview the OS isn't actually
    // routing keystrokes to yet.
    void activateAndFocusWindow().then(() => term.focus());
  }, [id, active]);

  // Routes a dictated result straight into this tab's PTY, same reasoning as
  // onData above: no local echo/focus trick, the shell's own echo over
  // terminal-output is what actually renders it. Guarded by the pending
  // input's own id (not this terminal's `id`) so this only fires once per
  // new dictation result, not on every re-render while it's still set.
  useEffect(() => {
    if (!pendingInput) return;
    if (lastConsumedInputIdRef.current === pendingInput.id) return;
    lastConsumedInputIdRef.current = pendingInput.id;
    if (pendingInput.text) {
      void writeToTerminal(id, pendingInput.text);
    }
    onConsumePendingInput?.();
  }, [id, pendingInput, onConsumePendingInput]);

  return (
    // No opaque background on this container — the terminal is transparent
    // (see TERMINAL_THEME/allowTransparency) so the panel's liquid glass
    // shows through behind the text, making the terminal read as another
    // glass surface rather than a solid slab.
    <div className="relative min-h-0 flex-1 overflow-hidden">
      <div ref={containerRef} className="h-full w-full p-2" />
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
