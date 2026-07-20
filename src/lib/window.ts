import { currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";

export const PILL_SIZE = { width: 380, height: 64 };
export const EXPANDED_SIZE = { width: 420, height: 560 };
const SCREEN_MARGIN = 16;

async function topRightPosition(width: number) {
  const monitor = (await currentMonitor()) ?? (await primaryMonitor());
  if (!monitor) return new LogicalPosition(SCREEN_MARGIN, SCREEN_MARGIN);

  const scale = monitor.scaleFactor;
  const screenWidth = monitor.size.width / scale;
  return new LogicalPosition(screenWidth - width - SCREEN_MARGIN, SCREEN_MARGIN);
}

async function applySize(size: { width: number; height: number }) {
  const win = getCurrentWindow();
  await win.setSize(new LogicalSize(size.width, size.height));
  await win.setPosition(await topRightPosition(size.width));
}

export async function collapseToPill() {
  await applySize(PILL_SIZE);
}

export async function expandToPanel() {
  await applySize(EXPANDED_SIZE);
  await getCurrentWindow().setFocus();
}
