import { currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { activateAndFocusWindow } from "./api";

// Each of these is larger than its visible content (see the matching p-* in
// Pill.tsx / PipelinePanel.tsx) so glow effects have transparent room to
// fade out before hitting the window's hard rectangular edge.
export const PILL_SIZE = { width: 96, height: 96 };
// Deliberately above MIN_PANEL_SIZE, not equal to it — opening straight into
// a panel that's already pinned to its own floor left the 5-tab header (see
// below) reading as cramped even before the user touched a resize handle.
export const EXPANDED_SIZE = { width: 760, height: 520 };

// The panel is user-resizable (the pill is not — see collapseToPill/
// expandToPanel below, which toggle `setResizable` to match); these bound
// how far a manual drag can shrink/grow it. The width floor in particular
// has to fit the header's full tab row (chat/vault/automations/terminal/
// settings + collapse) without wrapping — 420 was too tight once a fourth
// tab (automations) was added, 560 was in turn too tight once a fifth
// (terminal) was, and even 640 started to feel crowded once the terminal
// tab was in regular use alongside the others.
export const MIN_PANEL_SIZE = { width: 720, height: 360 };
export const MAX_PANEL_SIZE = { width: 960, height: 800 };

const SCREEN_MARGIN = 20;
const ANIMATION_MS = 220;

interface Anchor {
  x: number;
  y: number;
  horizontal: "left" | "right";
  vertical: "top" | "bottom";
}

/** The screen work-area's bottom-right corner (Dock/menu-bar-aware) — used
 * only to place the window the very first time it's ever shown, before the
 * user has dragged it anywhere themselves. */
async function screenBottomRightAnchor(): Promise<Anchor> {
  const monitor = (await currentMonitor()) ?? (await primaryMonitor());
  if (!monitor) return { x: SCREEN_MARGIN, y: SCREEN_MARGIN, horizontal: "right", vertical: "bottom" };

  // `monitor.size`/`position` are the full physical panel, bezel to bezel —
  // anchoring against those puts the pill right where the Dock (or a
  // taskbar) lives. `workArea` is the OS-reported region with the Dock/menu
  // bar/taskbar already excluded, so anchor to that instead.
  const scale = monitor.scaleFactor;
  const workX = monitor.workArea.position.x / scale;
  const workY = monitor.workArea.position.y / scale;
  const workWidth = monitor.workArea.size.width / scale;
  const workHeight = monitor.workArea.size.height / scale;
  return {
    x: workX + workWidth - SCREEN_MARGIN,
    y: workY + workHeight - SCREEN_MARGIN,
    horizontal: "right",
    vertical: "bottom",
  };
}

/** Once the user has moved the window themselves, collapsing/expanding
 * should happen toward whichever corner of the screen the window is
 * *currently* closest to — not always the bottom-right — so a window
 * dragged up near the top-left tucks into a pill up there instead of one
 * shrinking toward a point in the middle of the screen. Compares the
 * window's distance to each screen edge in turn; whichever side of each
 * axis it's nearer to becomes that axis's anchor. */
async function nearestScreenCornerAnchor(rect: { x: number; y: number; width: number; height: number }): Promise<Anchor> {
  const monitor = (await currentMonitor()) ?? (await primaryMonitor());
  if (!monitor) return { x: rect.x + rect.width, y: rect.y + rect.height, horizontal: "right", vertical: "bottom" };

  const scale = monitor.scaleFactor;
  const workX = monitor.workArea.position.x / scale;
  const workY = monitor.workArea.position.y / scale;
  const workWidth = monitor.workArea.size.width / scale;
  const workHeight = monitor.workArea.size.height / scale;

  const distanceToLeft = rect.x - workX;
  const distanceToRight = workX + workWidth - (rect.x + rect.width);
  const distanceToTop = rect.y - workY;
  const distanceToBottom = workY + workHeight - (rect.y + rect.height);

  const horizontal = distanceToLeft <= distanceToRight ? "left" : "right";
  const vertical = distanceToTop <= distanceToBottom ? "top" : "bottom";

  return {
    x: horizontal === "left" ? rect.x : rect.x + rect.width,
    y: vertical === "top" ? rect.y : rect.y + rect.height,
    horizontal,
    vertical,
  };
}

function easeOutCubic(t: number) {
  return 1 - Math.pow(1 - t, 3);
}

/** Reads the window's actual current logical size and position — necessary
 * now that the panel is both user-resizable and user-draggable, since either
 * one changes the real size/position without going through `animateTo`,
 * which would otherwise animate from a stale starting point. */
async function currentLogicalRect() {
  const win = getCurrentWindow();
  const [scale, physicalSize, physicalPosition] = await Promise.all([
    win.scaleFactor(),
    win.innerSize(),
    win.outerPosition(),
  ]);
  return {
    x: physicalPosition.x / scale,
    y: physicalPosition.y / scale,
    width: physicalSize.width / scale,
    height: physicalSize.height / scale,
  };
}

// Whether the window has ever been positioned yet. `false` only for the
// very first collapse/expand of the process's lifetime — after that, every
// collapse/expand anchors to wherever the user last dragged the window to,
// rather than snapping back to a fixed screen corner.
let hasEstablishedPosition = false;

/** Animates size from wherever the window currently is to `target`, keeping
 * one corner anchored throughout — the screen's Dock-avoiding bottom-right
 * corner the very first time this ever runs, and thereafter whichever
 * screen corner the window's current position is nearest to. Collapsing
 * and expanding both go through this same anchor-detection, so they read as
 * mirror images of each other: collapsing shrinks toward that corner,
 * expanding grows back out away from it — never a fixed spot unrelated to
 * where the user actually left the window. */
async function animateTo(target: { width: number; height: number }) {
  const fromRect = await currentLogicalRect();
  const from = { width: fromRect.width, height: fromRect.height };

  let anchor: Anchor;
  if (hasEstablishedPosition) {
    anchor = await nearestScreenCornerAnchor(fromRect);
  } else {
    anchor = await screenBottomRightAnchor();
    hasEstablishedPosition = true;
  }

  const win = getCurrentWindow();
  const start = performance.now();

  await new Promise<void>((resolve) => {
    function frame() {
      const t = Math.min(1, (performance.now() - start) / ANIMATION_MS);
      const eased = easeOutCubic(t);
      const size = {
        width: from.width + (target.width - from.width) * eased,
        height: from.height + (target.height - from.height) * eased,
      };
      const position = new LogicalPosition(
        anchor.horizontal === "left" ? anchor.x : anchor.x - size.width,
        anchor.vertical === "top" ? anchor.y : anchor.y - size.height,
      );
      Promise.all([win.setSize(new LogicalSize(size.width, size.height)), win.setPosition(position)]).then(() => {
        if (t < 1) requestAnimationFrame(frame);
        else resolve();
      });
    }
    requestAnimationFrame(frame);
  });
}

export async function collapseToPill() {
  const win = getCurrentWindow();
  // Clear the panel's min/max constraints *before* shrinking — otherwise
  // the leftover `MIN_PANEL_SIZE` floor (set by expandToPanel below) clamps
  // every frame of this animation to itself, so the window never actually
  // gets smaller than the panel. Turn resizing off too, so the pill reads
  // as a fixed-size glanceable widget rather than exposing resize handles.
  await win.setResizable(false);
  await win.setMinSize(null);
  await win.setMaxSize(null);
  await animateTo(PILL_SIZE);
  // Harmless no-op in the common case (the window is already visible) —
  // a cheap safety net in case anything upstream ever hides it.
  await win.show();
}

export async function expandToPanel() {
  await animateTo(EXPANDED_SIZE);
  const win = getCurrentWindow();
  // Constraints are applied only *after* landing at the full size, same
  // reasoning as above in reverse — applying them mid-animation would clamp
  // the early (smaller) frames to this floor and make the growth look like
  // an instant snap instead of a smooth expand.
  //
  // Tried removing this in favor of relying solely on the JS-side clamp in
  // PipelinePanel.tsx's resize handles (reasoning: two independent clamps
  // enforcing the same bounds could disagree by a rounding fraction right
  // at the edge) — that made things *more* choppy in practice, not less, so
  // back to setting both. Whatever's actually causing the boundary
  // finickiness, it isn't this.
  await win.setMinSize(new LogicalSize(MIN_PANEL_SIZE.width, MIN_PANEL_SIZE.height));
  await win.setMaxSize(new LogicalSize(MAX_PANEL_SIZE.width, MAX_PANEL_SIZE.height));
  await win.setResizable(true);
  // Plain `win.setFocus()` has the same accessory-app reliability gap that
  // motivated `activateAndFocusWindow` for the dictation path (see
  // `PipelinePanel.tsx`'s `focusDraftAtEnd` and `src-tauri/src/window_focus.rs`)
  // — it just goes unnoticed here in the common case, where expansion is
  // triggered by a real mouse click on the pill, which already carries its
  // own OS-level focus semantics that a purely programmatic call doesn't.
  // Any caller that expands the panel without a preceding real click (e.g.
  // a hotkey-driven expand) hits the exact same gap, so use the same fix
  // here for consistency rather than only where a user has already
  // reported it.
  await activateAndFocusWindow();
}
