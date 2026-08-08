// Dictation insert-target registry.
//
// When Fn-key dictation finishes transcribing, the result text must land in
// whichever input the user is currently focused on — the chat textarea, the
// active terminal, or neither (dropped silently). This module holds a single
// mutable slot that the focused input registers on focus and clears on blur,
// so the dictation handler in App.tsx doesn't need to know which DOM element
// owns the caret — it just calls `insertText()`.

import type { InsertTarget } from "../types";

let target: InsertTarget = null;

/** Called by each input (ChatInput, TerminalPanel) when it receives focus. */
export function setInsertTarget(t: InsertTarget): void {
  target = t;
}

/** Called when an input loses focus — avoids stale-target routing after
 *  the user clicks away from both chat and terminal (e.g. onto a chip). */
export function clearInsertTarget(): void {
  target = null;
}

/** Route dictated text to the currently focused input.
 *  Chat: appends to the textarea content (does not send).
 *  Terminal: pastes into the PTY via xterm's paste mechanism (the bytes land
 *  in the shell's readline buffer, unexecuted — the user presses Enter). */
export function insertText(text: string): void {
  if (!target || !text.trim()) return;
  if (target.kind === "chat") {
    target.setText(text);
  } else if (target.kind === "terminal") {
    target.paste(text);
  }
}
