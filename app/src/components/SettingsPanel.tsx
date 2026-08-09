import { useEffect, useState } from "react";
import { fetchConfig, type AgentConfig } from "../api";

/** Read-only panel showing the agent's current configuration. */
export function SettingsPanel() {
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchConfig()
      .then((c) => {
        if (!cancelled) setConfig(c);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => { cancelled = true; };
  }, []);

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
