import { currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";

// Each of these is larger than its visible content (see the matching p-* in
// Pill.tsx / PipelinePanel.tsx) so glow effects have transparent room to
// fade out before hitting the window's hard rectangular edge.
export const PILL_SIZE = { width: 96, height: 96 };
export const EXPANDED_SIZE = { width: 640, height: 480 };

// The panel is user-resizable (the pill is not — see collapseToPill/
// expandToPanel below, which toggle `setResizable` to match); these bound
// how far a manual drag can shrink/grow it.
const MIN_PANEL_SIZE = { width: 420, height: 360 };
const MAX_PANEL_SIZE = { width: 960, height: 800 };

const SCREEN_MARGIN = 20;
const ANIMATION_MS = 220;

async function bottomRightPosition(width: number, height: number) {
  const monitor = (await currentMonitor()) ?? (await primaryMonitor());
  if (!monitor) return new LogicalPosition(SCREEN_MARGIN, SCREEN_MARGIN);

  // `monitor.size`/`position` are the full physical panel, bezel to bezel —
  // anchoring against those puts the pill right where the Dock (or a
  // taskbar) lives. `workArea` is the OS-reported region with the Dock/menu
  // bar/taskbar already excluded, so anchor to that instead.
  const scale = monitor.scaleFactor;
  const workX = monitor.workArea.position.x / scale;
  const workY = monitor.workArea.position.y / scale;
  const workWidth = monitor.workArea.size.width / scale;
  const workHeight = monitor.workArea.size.height / scale;
  return new LogicalPosition(
    workX + workWidth - width - SCREEN_MARGIN,
    workY + workHeight - height - SCREEN_MARGIN,
  );
}

async function applySize(size: { width: number; height: number }) {
  const win = getCurrentWindow();
  await win.setSize(new LogicalSize(size.width, size.height));
  await win.setPosition(await bottomRightPosition(size.width, size.height));
}

function easeOutCubic(t: number) {
  return 1 - Math.pow(1 - t, 3);
}

/** Reads the window's actual current logical size, rather than trusting a
 * remembered value — necessary now that the panel is user-resizable, since a
 * manual drag-resize changes the real size without going through
 * `animateTo`, which would otherwise animate from a stale starting point. */
async function currentLogicalSize() {
  const win = getCurrentWindow();
  const [scale, physical] = await Promise.all([win.scaleFactor(), win.innerSize()]);
  return { width: physical.width / scale, height: physical.height / scale };
}

/** Animates size (and, since position is re-derived from it each frame, position) from wherever the window currently is to `target`, keeping the bottom-right corner anchored throughout. */
async function animateTo(target: { width: number; height: number }) {
  const from = await currentLogicalSize();
  const start = performance.now();

  await new Promise<void>((resolve) => {
    function frame() {
      const t = Math.min(1, (performance.now() - start) / ANIMATION_MS);
      const eased = easeOutCubic(t);
      const size = {
        width: from.width + (target.width - from.width) * eased,
        height: from.height + (target.height - from.height) * eased,
      };
      applySize(size).then(() => {
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
  await win.setMinSize(new LogicalSize(MIN_PANEL_SIZE.width, MIN_PANEL_SIZE.height));
  await win.setMaxSize(new LogicalSize(MAX_PANEL_SIZE.width, MAX_PANEL_SIZE.height));
  await win.setResizable(true);
  await win.setFocus();
}
