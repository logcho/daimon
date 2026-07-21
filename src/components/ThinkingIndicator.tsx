import { useEffect, useState } from "react";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const VERBS = ["Thinking", "Reasoning", "Planning", "Considering", "Working it out", "Piecing it together"];

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
    <span className="inline-flex items-center gap-2 font-mono text-sm text-[#4f8dff]">
      <span className="inline-block w-3 text-center">{FRAMES[frame]}</span>
      <span>{VERBS[verbIndex]}…</span>
    </span>
  );
}
