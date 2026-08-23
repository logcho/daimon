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

/** The braille spinner frame, advancing on its own.
 *
 *  A hook rather than a component so a caller can put the glyph wherever its
 *  own layout wants it — the turn footer sets it beside a tool name, which is
 *  not the same shape as the standalone indicator below. */
export function useSpinnerFrame(): string {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), 80);
    return () => clearInterval(timer);
  }, []);
  return FRAMES[frame];
}

/** The rotating verb.
 *
 *  Deliberately the *fallback* now rather than the main event: when the turn
 *  has a running step, its name is what the user wants and "Flibbertigibbeting"
 *  is decoration in the space where information was available. This is only
 *  reached when there is genuinely nothing to name — before the first tool
 *  call, or between one finishing and the next starting. */
export function useThinkingVerb(): string {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setIndex((v) => (v + 1) % VERBS.length), 2200);
    return () => clearInterval(timer);
  }, []);
  return VERBS[index];
}

/** The spinner and a verb, with nothing else to say. */
export function ThinkingIndicator() {
  const frame = useSpinnerFrame();
  const verb = useThinkingVerb();

  return (
    <span className="inline-flex items-center gap-2 text-sm text-[#4f8dff]">
      {/* Keep the braille spinner glyph itself monospaced so its cell width
          stays fixed as frames cycle; the verb beside it is prose, so it
          rides the app's sans stack like the rest of the chat text. */}
      <span className="inline-block w-3 text-center font-mono">{frame}</span>
      <span>{verb}</span>
    </span>
  );
}
