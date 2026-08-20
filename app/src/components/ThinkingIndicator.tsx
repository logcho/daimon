import { useEffect, useState } from "react";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
// Mirrors the CLI's thinking verbs (agents/src/daimon_agent/cli/tui.py)
const VERBS = [
  "Thinking…",
  "Ruminating…",
  "Discombobulating…",
  "Consulting the oracles…",
  "Spelunking the vault…",
  "Chasing will-o'-wisps…",
  "Fiddling with knobs…",
  "Flibbertigibbeting…",
];

export function ThinkingIndicator() {
  const [frame, setFrame] = useState(0);
  const [verbIndex, setVerbIndex] = useState(0);

  useEffect(() => {
    const frameTimer = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), 80);
    const verbTimer = setInterval(() => setVerbIndex((v) => (v + 1) % VERBS.length), 2200);
    return () => {
      clearInterval(frameTimer);
      clearInterval(verbTimer);
    };
  }, []);

  return (
    <span className="inline-flex items-center gap-2 text-sm text-[#4f8dff]">
      {/* Keep the braille spinner glyph itself monospaced so its cell width
          stays fixed as frames cycle; the verb beside it is prose, so it
          rides the app's sans stack like the rest of the chat text. */}
      <span className="inline-block w-3 text-center font-mono">{FRAMES[frame]}</span>
      <span>{VERBS[verbIndex]}…</span>
    </span>
  );
}
