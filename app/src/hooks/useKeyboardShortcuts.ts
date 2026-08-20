import { useEffect, useRef } from "react";
import type { View } from "../types";

/** Everything the shortcut handler needs to read or drive. All of it already
 *  lives in App.tsx — this hook adds no state of its own. */
export interface ShortcutContext {
  expanded: boolean;
  expand: () => void;
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
}

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
 * | Chord                            | Action                          |
 * |----------------------------------|---------------------------------|
 * | Cmd+T                            | new tab in the current scope    |
 * | Cmd+1 – Cmd+8                    | select the Nth tab              |
 * | Cmd+9                            | select the *last* tab           |
 * | Cmd+W                            | close the active tab            |
 * | Cmd+Shift+] / Ctrl+Tab           | next tab, wrapping              |
 * | Cmd+Shift+[ / Ctrl+Shift+Tab     | previous tab, wrapping          |
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
      } else if (
        (cmd && event.shiftKey && (code === "BracketLeft" || code === "BracketRight")) ||
        (ctrl && code === "Tab")
      ) {
        const forward = code === "Tab" ? !event.shiftKey : code === "BracketRight";
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
