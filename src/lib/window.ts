import { currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";

// Each of these is larger than its visible content (see the matching p-* in
// Pill.tsx / PipelinePanel.tsx) so glow effects have transparent room to
// fade out before hitting the window's hard rectangular edge.
export const PILL_SIZE = { width: 96, height: 96 };
export const EXPANDED_SIZE = { width: 468, height: 608 };

const SCREEN_MARGIN = 20;
const ANIMATION_MS = 220;

async function bottomRightPosition(width: number, height: number) {
  const monitor = (await currentMonitor()) ?? (await primaryMonitor());
  if (!monitor) return new LogicalPosition(SCREEN_MARGIN, SCREEN_MARGIN);

  const scale = monitor.scaleFactor;
  const screenWidth = monitor.size.width / scale;
  const screenHeight = monitor.size.height / scale;
  return new LogicalPosition(screenWidth - width - SCREEN_MARGIN, screenHeight - height - SCREEN_MARGIN);
}

async function applySize(size: { width: number; height: number }) {
  const win = getCurrentWindow();
  await win.setSize(new LogicalSize(size.width, size.height));
  await win.setPosition(await bottomRightPosition(size.width, size.height));
}

function easeOutCubic(t: number) {
  return 1 - Math.pow(1 - t, 3);
}

let currentSize = PILL_SIZE;

/** Animates size (and, since position is re-derived from it each frame, position) from wherever the window currently is to `target`, keeping the bottom-right corner anchored throughout. */
async function animateTo(target: { width: number; height: number }) {
  const from = currentSize;
  currentSize = target;
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
  await animateTo(PILL_SIZE);
}

export async function expandToPanel() {
  await animateTo(EXPANDED_SIZE);
  await getCurrentWindow().setFocus();
}
