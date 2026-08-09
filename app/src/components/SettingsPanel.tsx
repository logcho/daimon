import { useCallback, useEffect, useState } from "react";
import { fetchConfig, updateConfig, type AgentConfig } from "../api";

/** Settings panel — read-only configuration display with editable API key. */
export function SettingsPanel() {
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editingKey, setEditingKey] = useState(false);
  const [keyValue, setKeyValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [keyMsg, setKeyMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    fetchConfig()
      .then(setConfig)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSaveKey = async () => {
    const val = keyValue.trim();
    if (!val) return;
    setSaving(true);
    setKeyMsg(null);
    try {
      await updateConfig(val);
      setKeyMsg("Key updated — in effect immediately.");
      setEditingKey(false);
      setKeyValue("");
      load(); // refresh to show updated status
    } catch (e) {
      setKeyMsg(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (error) {
    return (
      <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-red-400">
        Failed to load configuration: {error}
      </div>
    );
  }

  if (!config) {
    return (
      <div className="flex flex-1 items-center justify-center px-6">
        <span className="block h-4 w-4 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
      </div>
    );
  }

  const section = (title: string, rows: [string, string | number | boolean][]) => (
    <div className="liquid-glass-subtle rounded-xl px-4 py-3">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-neutral-500">{title}</h3>
      <dl className="space-y-1.5">
        {rows.map(([label, value]) => (
          <div key={label} className="flex items-baseline justify-between gap-4">
            <dt className="shrink-0 text-xs text-neutral-400">{label}</dt>
            <dd className="truncate text-right font-mono text-xs text-neutral-200">
              {typeof value === "boolean" ? (
                <span className={value ? "text-emerald-400" : "text-neutral-500"}>
                  {value ? "on" : "off"}
                </span>
              ) : (
                String(value)
              )}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );

  return (
    <div className="themed-scroll flex-1 space-y-3 overflow-y-auto p-4">
      {/* API key — editable inline */}
      <div className="liquid-glass-subtle rounded-xl px-4 py-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-neutral-500">API Key</h3>
        {editingKey ? (
          <div className="space-y-2">
            <input
              type="password"
              value={keyValue}
              onChange={(e) => setKeyValue(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleSaveKey(); if (e.key === "Escape") { setEditingKey(false); setKeyValue(""); } }}
              placeholder="sk-…"
              autoFocus
              className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 font-mono text-xs text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/50 focus:outline-none"
            />
            <div className="flex items-center gap-2">
              <button
                onClick={handleSaveKey}
                disabled={saving || !keyValue.trim()}
                className="rounded-full bg-[#4f8dff] px-3 py-1 text-xs font-medium text-white transition hover:bg-[#3b6fcc] disabled:opacity-40"
              >
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                onClick={() => { setEditingKey(false); setKeyValue(""); setKeyMsg(null); }}
                className="rounded-full px-3 py-1 text-xs font-medium text-neutral-400 transition hover:bg-white/5 hover:text-neutral-100"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-between">
            <span className="font-mono text-xs text-neutral-400">
              {config.api_key_configured ? "••••••••" : "not set"}
            </span>
            <button
              onClick={() => { setEditingKey(true); setKeyMsg(null); }}
              className="rounded-full px-2.5 py-0.5 text-xs font-medium text-[#4f8dff] transition hover:bg-[#4f8dff]/10"
            >
              Edit
            </button>
          </div>
        )}
        {keyMsg && (
          <p className={`mt-1.5 text-xs ${keyMsg.startsWith("Key updated") ? "text-emerald-400" : "text-red-400"}`}>
            {keyMsg}
          </p>
        )}
      </div>

      {section("Model", [
        ["Main model", config.model],
        ["Flash model", config.flash_model],
        ["API base", config.api_base],
        ["Temperature", config.temperature],
        ["Max tokens", config.max_tokens.toLocaleString()],
      ])}

      {section("Workspace", [
        ["Workspace dir", config.workspace],
        ["Vault dir", config.vault],
        ["Port", config.port],
      ])}

      {section("Features", [
        ["Live frames", config.live_frames],
        ["Reflection", config.reflect],
        ["Compaction", `${(config.compaction_chars / 1000).toFixed(0)}k chars`],
      ])}

      {section("Browser (PinchTab)", [
        ["URL", config.pinchtab_base],
        ["Healthy", config.pinchtab_healthy],
      ])}
    </div>
  );
}
