import type { DictationStatus, VoiceModelDownloadPayload, VoiceModelStatus } from "../types";
import { downloadVoiceModel } from "../api";
import { useEffect, useRef, useState } from "react";

const BAR = "inline-block w-[3px] rounded-full bg-[#4f8dff]";
const BAR_COUNT = 4;
const DOT = "h-1 w-1 rounded-full bg-[#4f8dff]";

interface Props {
  dictation: DictationStatus;
  model: VoiceModelStatus | null;
  modelDownload: VoiceModelDownloadPayload | null;
  onRefreshModel: () => void;
}

/** 4-bar CSS sound wave for dictation — ported from legacy SoundWave.tsx.
 *  State-driven: idle (subtle static), recording (animated + locked dots),
 *  transcribing (pulsing together), error (red static). */
export function VoiceIndicator({ dictation, model, modelDownload, onRefreshModel }: Props) {
  const [downloading, setDownloading] = useState(false);
  // "no_speech" is a transient blip — flash it then return to idle
  const [noSpeech, setNoSpeech] = useState(false);
  const noSpeechTimer = useRef<ReturnType<typeof setTimeout>>(null);

  useEffect(() => {
    if (dictation.type === "no_speech") {
      setNoSpeech(true);
      if (noSpeechTimer.current) clearTimeout(noSpeechTimer.current);
      noSpeechTimer.current = setTimeout(() => setNoSpeech(false), 1000);
    }
    return () => {
      if (noSpeechTimer.current) clearTimeout(noSpeechTimer.current);
    };
  }, [dictation.type]);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await downloadVoiceModel();
      onRefreshModel();
    } catch {
      // keep stale status; user can retry
    } finally {
      setDownloading(false);
    }
  };

  // --- Model not downloaded: clickable download chip --------------------------

  if (!model?.downloaded) {
    if (modelDownload && !modelDownload.done) {
      const pct = modelDownload.totalBytes
        ? Math.round((modelDownload.downloadedBytes / modelDownload.totalBytes) * 100)
        : null;
      return (
        <span
          title={pct !== null ? `downloading model… ${pct}%` : "downloading model…"}
          className="liquid-glass-subtle flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-neutral-400"
        >
          <span className="h-1.5 w-1.5 rounded-full border border-neutral-950 bg-amber-400 animate-daimon-pulse" />
          {pct !== null ? `${pct}%` : "dl"}
        </span>
      );
    }
    return (
      <button
        title="whisper model not downloaded — click to download"
        className="liquid-glass-subtle flex shrink-0 cursor-pointer items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-neutral-400 transition duration-200 hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)]"
        onClick={downloading ? undefined : handleDownload}
        disabled={downloading}
      >
        <span className="h-1.5 w-1.5 rounded-full border border-neutral-950 bg-amber-400" />
        {downloading ? "..." : "dl"}
      </button>
    );
  }

  // --- Determine bar state from dictation status -------------------------------

  const recording = dictation.type === "recording";
  const locked = recording && dictation.locked;
  const transcribing = dictation.type === "transcribing";
  const error = dictation.type === "error" || noSpeech;
  // "result" or "idle" → subtle static bars (the default when nothing else matches)

  // Bar opacity and animation class
  let barOpacity = "opacity-30";
  let barAnim = "";
  let barColor = "bg-[#4f8dff]";
  let title = "dictation idle";

  if (recording) {
    barOpacity = "opacity-100";
    barAnim = "animate-daimon-sound-wave";
    title = locked ? "recording (locked — press Fn to stop)" : "recording (hold Fn)";
  } else if (transcribing) {
    barOpacity = "opacity-70";
    barAnim = "animate-daimon-sound-wave";
    barColor = "bg-amber-400";
    title = "transcribing…";
  } else if (error) {
    barOpacity = "opacity-80";
    barColor = "bg-red-400";
    title = dictation.type === "error" ? dictation.message : "no speech detected";
  }

  return (
    <span title={title} className="flex shrink-0 items-center" style={{ width: 24 }}>
      {/* Bars in a flex-col: wave on top, locked dots beneath */}
      <span className="flex flex-col items-center gap-[2px]">
        <span className="flex items-center gap-[3px]">
          {Array.from({ length: BAR_COUNT }, (_, i) => (
            <span
              key={i}
              className={`${BAR} ${barOpacity} ${barAnim} ${barColor}`}
              style={{
                height: 14,
                animationDelay: transcribing ? "0s" : `${i * 0.15}s`,
              }}
            />
          ))}
        </span>
        {/* Locked dots — two small static markers beneath the wave,
            matching the legacy locked indicator (Pill.tsx:150-153). */}
        {locked && (
          <span className="flex items-center gap-1">
            <span className={DOT} />
            <span className={DOT} />
          </span>
        )}
      </span>
    </span>
  );
}
