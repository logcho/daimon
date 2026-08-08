import { currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { invoke } from "@tauri-apps/api/core";

// The pill/panel fill the window edge-to-edge (no inset padding wrapper) so
// the visible shape aligns exactly with the native vibrancy material, which
// fills the whole window — see vibrancy.rs. That's why the window is 56px, not
// 96: it *is* the visible circle. A 56px square with the shared 28px vibrancy
// radius is a perfect circle (28 = 56/2).
export const PILL_SIZE = { width: 56, height: 56 };
// Deliberately above MIN_PANEL_SIZE, not equal to it — opening straight into
// a panel already pinned to its own floor reads as cramped.
export const EXPANDED_SIZE = { width: 760, height: 500 };

// The panel is user-resizable (the pill is not — see collapseToPill/
// expandToPanel below, which toggle `setResizable` to match); these bound
// how far a manual drag can shrink/grow it. The width floor has to fit the
// header's tab row (chat/terminal + collapse) without wrapping.
export const MIN_PANEL_SIZE = { width: 720, height: 500 };
export const MAX_PANEL_SIZE = { width: 1152, height: 800 };

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
 * shrinking toward a point in the middle of the screen. */
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
 * expanding grows back out away from it. */
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

const SAVED_SIZE_KEY = "daimon-panel-size";

function loadPanelSize(): { width: number; height: number } | null {
  try {
    const raw = localStorage.getItem(SAVED_SIZE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed.width === "number" && typeof parsed.height === "number") {
      // Clamp to min/max bounds — the saved size may be from a different
      // screen or a prior version with different limits.
      return {
        width: Math.min(MAX_PANEL_SIZE.width, Math.max(MIN_PANEL_SIZE.width, parsed.width)),
        height: Math.min(MAX_PANEL_SIZE.height, Math.max(MIN_PANEL_SIZE.height, parsed.height)),
      };
    }
  } catch {
    // corrupted — ignore
  }
  return null;
}

function savePanelSize(size: { width: number; height: number }) {
  try {
    localStorage.setItem(SAVED_SIZE_KEY, JSON.stringify(size));
  } catch {
    // quota exceeded or private browsing — silently ignore
  }
}

export async function collapseToPill() {
  const win = getCurrentWindow();
  // Save the panel's current size before collapsing so the next expand
  // restores it instead of always landing at the fixed default.
  const rect = await currentLogicalRect();
  savePanelSize({ width: rect.width, height: rect.height });
  // Clear the panel's min/max constraints *before* shrinking — otherwise
  // the leftover `MIN_PANEL_SIZE` floor (set by expandToPanel below) clamps
  // every frame of this animation to itself, so the window never actually
  // gets smaller than the panel. Turn resizing off too, so the pill reads
  // as a fixed-size glanceable widget rather than exposing resize handles.
  await win.setResizable(false);
  await win.setMinSize(null);
  await win.setMaxSize(null);
  await animateTo(PILL_SIZE);
  // Re-assert always-on-top + visible-on-all-workspaces after the collapse
  // animation — same defensive reasoning as expandToPanel.
  await win.setAlwaysOnTop(true);
  await win.setVisibleOnAllWorkspaces(true);
  // Harmless no-op in the common case (the window is already visible) —
  // a cheap safety net in case anything upstream ever hides it.
  await win.show();
}

export async function expandToPanel() {
  const saved = loadPanelSize();
  await animateTo(saved ?? EXPANDED_SIZE);
  const win = getCurrentWindow();
  // Constraints are applied only *after* landing at the full size — applying
  // them mid-animation would clamp the early (smaller) frames to this floor
  // and make the growth look like an instant snap instead of a smooth expand.
  await win.setMinSize(new LogicalSize(MIN_PANEL_SIZE.width, MIN_PANEL_SIZE.height));
  await win.setMaxSize(new LogicalSize(MAX_PANEL_SIZE.width, MAX_PANEL_SIZE.height));
  await win.setResizable(true);
  // Explicitly re-assert always-on-top and visible-on-all-workspaces — Tauri
  // preserves the creation flags, but a defensive re-apply after the expand
  // animation guards against any platform-specific window-level reset during
  // resize. On macOS `alwaysOnTop` sets NSFloatingWindowLevel and
  // `visibleOnAllWorkspaces` sets `canJoinAllSpaces` + `fullScreenAuxiliary`,
  // which together are what "float in front of everything, across every
  // Space/desktop, including full-screen apps" actually requires.
  await win.setAlwaysOnTop(true);
  await win.setVisibleOnAllWorkspaces(true);
  // With ActivationPolicy::Accessory set on the Rust side, plain setFocus() is
  // unreliable — the window can become nominally key without the app itself
  // ever activating. The dedicated `activate_and_focus_window` command (ported
  // from legacy's window_focus.rs) calls NSApplication::activate() on the main
  // thread then makes the webview first responder, which is what AppKit needs
  // to actually route keystrokes into our input.
  await invoke("activate_and_focus_window");
}
