// A small animated bar-equalizer visual — not audio-reactive (no live
// amplitude is streamed from the backend), just an unmistakable "actively
// listening" indicator, staggered via animation-delay per bar so it reads
// as a wave rather than four bars blinking in lockstep.
const BAR_COUNT = 4;

export function SoundWave({
  className = "",
  barClassName = "",
  barHeight = 14,
}: {
  className?: string;
  barClassName?: string;
  /** In pixels — passed as an inline style, so a Tailwind height utility in
   * `barClassName` won't reliably win over (or lose to) it; use this prop to
   * resize instead of trying to override height via `barClassName`. */
  barHeight?: number;
}) {
  return (
    <div className={`flex items-center gap-[3px] ${className}`}>
      {Array.from({ length: BAR_COUNT }, (_, i) => (
        <span
          key={i}
          className={`animate-daimon-sound-wave block w-[3px] rounded-full bg-[#4f8dff] ${barClassName}`}
          style={{ height: `${barHeight}px`, animationDelay: `${i * 0.15}s` }}
        />
      ))}
    </div>
  );
}
