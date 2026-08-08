import type { DictationStatus, VoiceModelDownloadPayload, VoiceModelStatus } from "../types";
import { downloadVoiceModel } from "../api";
import { useState } from "react";

const STATUS_DOT = "h-1.5 w-1.5 rounded-full border border-neutral-950";

interface Props {
  dictation: DictationStatus;
  model: VoiceModelStatus | null;
  modelDownload: VoiceModelDownloadPayload | null;
  onRefreshModel: () => void;
}

/** The mic chip in the panel header — shows dictation state (idle, recording,
 *  transcribing, error) and offers a download button when the local whisper
 *  model hasn't been fetched yet. */
export function VoiceIndicator({ dictation, model, modelDownload, onRefreshModel }: Props) {
  const [downloading, setDownloading] = useState(false);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await downloadVoiceModel();
      onRefreshModel();
    } catch {
      // keep the stale status; the user can retry
    } finally {
      setDownloading(false);
    }
  };

  let dot = "bg-neutral-600";
  let title = "dictation idle";

  if (!model?.downloaded) {
    dot = "bg-amber-400";
    title = "whisper model not downloaded — click to download";
  } else if (dictation.type === "recording") {
    dot = "bg-red-400 animate-daimon-pulse";
    title = dictation.locked ? "recording (locked)" : "recording";
  } else if (dictation.type === "transcribing") {
    dot = "bg-[#4f8dff] animate-daimon-pulse";
    title = "transcribing...";
  } else if (dictation.type === "error") {
    dot = "bg-red-400";
    title = dictation.message;
  }

  if (modelDownload && !modelDownload.done) {
    const pct = modelDownload.totalBytes
      ? Math.round((modelDownload.downloadedBytes / modelDownload.totalBytes) * 100)
      : null;
    title = pct !== null ? `downloading model… ${pct}%` : `downloading model…`;
    dot = "bg-amber-400 animate-daimon-pulse";
  }

  const chip = (
    <span
      title={title}
      className={`liquid-glass-subtle flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-neutral-400 transition duration-200 ${
        !model?.downloaded ? "cursor-pointer hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)]" : ""
      }`}
      onClick={!model?.downloaded && !downloading ? handleDownload : undefined}
    >
      <span className={`${STATUS_DOT} ${dot}`} />
      mic
    </span>
  );

  return chip;
}
