import { currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";

// The visible icon is 56x56 (see Pill.tsx's p-5 padding); the extra 40px
// here gives its glow room to fade out before hitting the window's hard,
// rectangular edge — otherwise the blur gets clipped square.
export const PILL_SIZE = { width: 96, height: 96 };
export const EXPANDED_SIZE = { width: 420, height: 560 };
const SCREEN_MARGIN = 20;

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

export async function collapseToPill() {
  await applySize(PILL_SIZE);
}

export async function expandToPanel() {
  await applySize(EXPANDED_SIZE);
  await getCurrentWindow().setFocus();
}
