import type { VoiceModelDownloadPayload, VoiceModelStatus } from "../types";
import { downloadVoiceModel } from "../api";
import { useState } from "react";

interface Props {
  model: VoiceModelStatus | null;
  modelDownload: VoiceModelDownloadPayload | null;
  onRefreshModel: () => void;
}

/** Model download chip — only renders when the whisper model hasn't been
 *  downloaded yet (or is currently downloading). Dictation visualization
 *  (SoundWave, spinner, error) is now inline in the Panel header. */
export function VoiceIndicator({ model, modelDownload, onRefreshModel }: Props) {
  const [downloading, setDownloading] = useState(false);

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

  // Model is downloaded — dictation is visualized inline in the header;
  // this chip is only for the download pathway.
  if (model?.downloaded) return null;

  if (modelDownload && !modelDownload.done) {
    const pct = modelDownload.totalBytes
      ? Math.round((modelDownload.downloadedBytes / modelDownload.totalBytes) * 100)
      : null;
    return (
      <span
        title={pct !== null ? `downloading model… ${pct}%` : "downloading model…"}
        className="liquid-glass-subtle flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-neutral-400"
      >
        <span className="h-1.5 w-1.5 animate-daimon-pulse rounded-full border border-neutral-950 bg-amber-400" />
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
