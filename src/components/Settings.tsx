import { useEffect, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { getApiKeyStatus, setApiKey } from "../lib/api";

type SaveState = "idle" | "saving" | "saved" | "error";

export function Settings() {
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [draft, setDraft] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    getApiKeyStatus()
      .then(setHasKey)
      .catch(() => setHasKey(false));
  }, []);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    const key = draft.trim();
    if (!key) return;

    setSaveState("saving");
    try {
      await setApiKey(key);
      setHasKey(true);
      setDraft("");
      setSaveState("saved");
      setTimeout(() => setSaveState("idle"), 2500);
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err));
      setSaveState("error");
    }
  }

  return (
    <div className="themed-scroll flex-1 overflow-y-auto p-4">
      <h3 className="font-mono text-sm text-neutral-100">anthropic_api_key</h3>
      <p className="mt-1 font-mono text-xs text-neutral-500">
        {hasKey === null && "checking…"}
        {hasKey === true && <span className="text-emerald-400">● configured</span>}
        {hasKey === false && (
          <span>○ not set — tasks run a scripted demo instead of the real agent</span>
        )}
      </p>

      <form onSubmit={handleSave} className="mt-4 space-y-2">
        <input
          type="password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="sk-ant-..."
          className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-[#4f8dff]/60 focus:outline-none focus:ring-2 focus:ring-[#4f8dff]/20"
        />
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={!draft.trim() || saveState === "saving"}
            className="rounded-full border border-white/15 bg-white/5 px-4 py-1.5 font-mono text-xs text-neutral-100 transition hover:border-white/25 hover:bg-white/10 active:scale-95 disabled:opacity-40"
          >
            {saveState === "saving" ? "saving…" : "save"}
          </button>
          {saveState === "saved" && (
            <span className="font-mono text-xs text-emerald-400">saved — applies to the next task</span>
          )}
          {saveState === "error" && <span className="font-mono text-xs text-red-400">{errorMessage}</span>}
        </div>
      </form>

      <p className="mt-6 font-mono text-xs leading-relaxed text-neutral-600">
        get a key from{" "}
        <button
          type="button"
          onClick={() => openUrl("https://console.anthropic.com/settings/keys")}
          className="text-[#4f8dff] hover:underline"
        >
          console.anthropic.com
        </button>
        . stored locally in .env, only ever sent into the background workspace at task time.
      </p>
    </div>
  );
}
