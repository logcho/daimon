import { useEffect, useRef } from "react";
import type { View } from "../types";

/** Everything the shortcut handler needs to read or drive. All of it already
 *  lives in App.tsx — this hook adds no state of its own. */
export interface ShortcutContext {
  expanded: boolean;
  expand: () => void;
  collapse: () => void;
  view: View;
  setView: (view: View) => void;
  sessionOrder: string[];
  activeSessionId: string | null;
  selectChat: (id: string) => void;
  newChat: () => void;
  closeChat: (id: string) => void;
  terminalTabs: string[];
  activeTerminalId: string | null;
  selectTerminal: (id: string) => void;
  newTerminal: () => void;
  closeTerminal: (id: string) => void;
  /** Give the topmost dismissable layer — currently the link viewer — first
   *  refusal on Escape. Returns whether it consumed the key.
   *
   *  It has to be routed through here rather than handled by the layer itself:
   *  this hook's listener is registered on `window` in the capture phase at App
   *  mount, and same-target capture listeners fire in registration order, so an
   *  overlay mounted later can never get there first. Without this, Escape over
   *  an open viewer collapsed the whole panel to the pill instead of closing
   *  the layer in front of it. */
  dismissTop?: () => boolean;
}

/** Direct-jump order for Cmd+Shift+1..4 — matches the header button order in
 *  Panel.tsx, so the digit you press lines up with what you see. */
const VIEW_ORDER: View[] = ["chat", "terminal", "vault", "settings"];

/** How close together two Escapes must be, in the terminal view, to mean
 *  "collapse" rather than two ordinary Escapes headed for the shell. */
const DOUBLE_ESC_WINDOW_MS = 400;

/** Which tab strip a shortcut acts on. The vault and settings views have no
 *  tabs of their own, so they fall through to chat — Cmd+T there gives you a
 *  new chat and drops you on the chat view rather than being a dead key. */
type Scope = "chat" | "terminal";

interface ResolvedScope {
  scope: Scope;
  tabs: string[];
  activeId: string | null;
  select: (id: string) => void;
  create: () => void;
  close: (id: string) => void;
}

function resolveScope(ctx: ShortcutContext): ResolvedScope {
  if (ctx.view === "terminal") {
    return {
      scope: "terminal",
      tabs: ctx.terminalTabs,
      activeId: ctx.activeTerminalId,
      select: ctx.selectTerminal,
      create: ctx.newTerminal,
      close: ctx.closeTerminal,
    };
  }
  return {
    scope: "chat",
    tabs: ctx.sessionOrder,
    activeId: ctx.activeSessionId,
    select: ctx.selectChat,
    create: ctx.newChat,
    close: ctx.closeChat,
  };
}

/**
 * Browser/iTerm-style tab shortcuts for the chat and terminal strips.
 *
 * Two levels, split by modifier family: **Cmd acts on tabs, Ctrl and
 * Cmd+Shift+digit act on views.**
 *
 * | Chord              | Action                                    |
 * |--------------------|-------------------------------------------|
 * | Cmd+T              | new tab in the current scope              |
 * | Cmd+1 – Cmd+8      | select the Nth tab                        |
 * | Cmd+9              | select the *last* tab                     |
 * | Cmd+W              | close the active tab                      |
 * | Cmd+Shift+]        | next tab, wrapping                        |
 * | Cmd+Shift+[        | previous tab, wrapping                    |
 * | Ctrl+Tab           | next view, wrapping (Shift for previous)  |
 * | Cmd+Shift+1 – 4    | jump to chat / terminal / vault / settings|
 * | Escape             | collapse (twice, in the terminal view)    |
 *
 * Two details carry the whole design:
 *
 * 1. **Capture phase.** The terminal view keeps xterm's hidden `<textarea>`
 *    focused and pipes every keystroke straight to the PTY (see
 *    TerminalPanel's `term.onData`). A window-level *capture* listener runs
 *    before any handler on that textarea, so stopping propagation on a matched
 *    chord means xterm never sees it — no `attachCustomKeyEventHandler`
 *    needed, and the delicate Kitty-protocol path stays untouched. Anything
 *    that isn't one of the chords above is left completely alone, so ordinary
 *    typing (including bare Esc, t, and 1) still reaches the shell.
 *
 * 2. **A ref, not a dependency array.** The handler reads `view`,
 *    `sessionOrder`, `terminalTabs` and friends, all of which change on
 *    basically every render. Mirroring the context into a ref (same pattern as
 *    App.tsx's `sessionsRef`) lets the listener register exactly once instead
 *    of tearing down and re-adding itself constantly.
 */
export function useKeyboardShortcuts(ctx: ShortcutContext) {
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;
  // Only meaningful in the terminal view — see the Escape branch below.
  const lastEscapeAt = useRef(0);

  useEffect(() => {
    function handler(event: KeyboardEvent) {
      // Holding a chord down shouldn't spawn a tab per repeat.
      if (event.repeat) return;

      const ctx = ctxRef.current;
      // `e.code` rather than `e.key`: layout-robust, and on macOS Cmd+Shift+[
      // reports `e.key === "{"`, which is awkward to match on.
      const code = event.code;
      const cmd = event.metaKey && !event.ctrlKey && !event.altKey;
      const ctrl = event.ctrlKey && !event.metaKey && !event.altKey;

      // Escape dismisses. Handled ahead of the chord table below and
      // returning early, because that path ends by *expanding* the panel —
      // the exact opposite of what this wants.
      const bare = !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
      if (code === "Escape" && bare) {
        // Innermost layer first. Note this only fires while focus is in the
        // app's own DOM — a keypress inside a cross-origin iframe never reaches
        // the parent document, so the viewer's ✕ stays the reliable exit.
        if (ctx.dismissTop?.()) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }

        // The API-key field in settings binds Escape itself to cancel editing.
        // Narrow on purpose: the chat draft is a <textarea>, and Escape is
        // meant to collapse from there (the draft survives — panel state
        // outlives a collapse).
        if ((event.target as HTMLElement | null)?.tagName === "INPUT") return;

        if (ctx.view !== "terminal") {
          event.preventDefault();
          event.stopPropagation();
          ctx.collapse();
          return;
        }

        // In the terminal, Escape belongs to the shell — vim's mode exit,
        // readline's meta prefix, a TUI's cancel. So this never calls
        // preventDefault: both Escapes still reach the PTY, and only the
        // second one within the window also collapses. Swallowing it would be
        // worse than the duplicate, which is harmless everywhere it lands.
        const now = event.timeStamp;
        const isDouble = now - lastEscapeAt.current <= DOUBLE_ESC_WINDOW_MS;
        lastEscapeAt.current = isDouble ? 0 : now;
        if (isDouble) ctx.collapse();
        return;
      }

      // Resolved lazily so an unmatched key does no work at all.
      let action: (() => void) | null = null;

      if (cmd && !event.shiftKey && code === "KeyT") {
        action = () => {
          const { scope, create } = resolveScope(ctx);
          ctx.setView(scope);
          create();
        };
      } else if (cmd && !event.shiftKey && code === "KeyW") {
        // The only chord that does *not* fall through to chat: silently
        // closing a tab you can't currently see is a bad surprise, so this is
        // a no-op on the vault and settings views.
        if (ctx.view === "chat" || ctx.view === "terminal") {
          action = () => {
            const { activeId, close } = resolveScope(ctx);
            if (activeId) close(activeId);
          };
        }
      } else if (cmd && !event.shiftKey && /^Digit[1-9]$/.test(code)) {
        const digit = Number(code.slice(5));
        action = () => {
          const { scope, tabs, select } = resolveScope(ctx);
          // Cmd+9 is "last tab" regardless of count (the Chrome/Safari/iTerm
          // convention), not literally the ninth.
          const target = digit === 9 ? tabs[tabs.length - 1] : tabs[digit - 1];
          if (!target) return;
          ctx.setView(scope);
          select(target);
        };
      } else if (cmd && event.shiftKey && /^Digit[1-4]$/.test(code)) {
        // The outer level: shift means "view", not "tab". Unambiguous against
        // the plain Cmd+1-9 branch above, which requires !shiftKey.
        const target = VIEW_ORDER[Number(code.slice(5)) - 1];
        action = () => ctx.setView(target);
      } else if (ctrl && code === "Tab") {
        // Ctrl+Tab walks the whole view ring, wrapping — chat -> terminal ->
        // vault -> settings -> chat. Ctrl+Shift+Tab walks it backwards, which
        // is what keeps chat and terminal one press apart in either direction
        // even though they sit at opposite ends of a four-stop loop.
        const forward = !event.shiftKey;
        action = () => {
          const current = VIEW_ORDER.indexOf(ctx.view);
          const next = (current + (forward ? 1 : -1) + VIEW_ORDER.length) % VIEW_ORDER.length;
          ctx.setView(VIEW_ORDER[next]);
        };
      } else if (cmd && event.shiftKey && (code === "BracketLeft" || code === "BracketRight")) {
        const forward = code === "BracketRight";
        action = () => {
          const { scope, tabs, activeId, select } = resolveScope(ctx);
          if (tabs.length === 0) return;
          const current = activeId ? tabs.indexOf(activeId) : -1;
          const from = current === -1 ? 0 : current;
          const next = (from + (forward ? 1 : -1) + tabs.length) % tabs.length;
          ctx.setView(scope);
          select(tabs[next]);
        };
      }

      if (!action) return;

      // Matched — keep it away from xterm, the chat textarea, and the webview's
      // own default handling.
      event.preventDefault();
      event.stopPropagation();

      // Shortcuts work from the pill too: expand first, then act. `expand()` is
      // a synchronous state set plus a fire-and-forget window animation, so the
      // action below batches into the same render. (Best-effort only — the app
      // runs as an Accessory app, so a DOM keydown only arrives at all if the
      // pill window actually holds focus.)
      if (!ctx.expanded) ctx.expand();
      action();
    }

    window.addEventListener("keydown", handler, { capture: true });
    return () => window.removeEventListener("keydown", handler, { capture: true });
  }, []);
}
