import { useCallback, useEffect, useState } from "react";
import {
  fetchConfig,
  listModels,
  updateConfig,
  type AgentConfig,
  type ProviderModels,
} from "../api";

// The app's full chord list, kept here as the single place a user can look it
// up — the header buttons carry the same hints as tooltips, but only one at a
// time. Mirrors useKeyboardShortcuts.ts (tabs and views) plus the system-wide
// Fn gestures in src-tauri/src/fn_key.rs; keep the three in sync.
const SHORTCUTS: [string, string][] = [
  ["Open Daimon", "tap Fn"],
  ["Open and dictate", "hold Fn"],
  ["Dictate hands-free", "double-tap Fn"],
  ["Collapse", "Esc — Esc Esc in terminal"],
  ["Previous / next view", "⌃⇧Tab / ⌃Tab"],
  ["Go to chat / terminal / vault / settings", "⌘⇧1 – ⌘⇧4"],
  ["New chat or terminal tab", "⌘T"],
  ["Go to tab 1–8 / last tab", "⌘1 – ⌘8 / ⌘9"],
  ["Previous / next tab", "⌘⇧[ / ⌘⇧]"],
  ["Close the current tab", "⌘W"],
  ["Send a message / newline", "Enter / ⇧Enter"],
];

/** Settings — configuration display, with editable API keys and model
 *  selection.
 *
 *  Keys and models are separate things and both are needed: which provider
 *  actually runs is decided by the model spec (`anthropic:claude-sonnet-5`),
 *  not by which keys exist, so a key added on its own changes nothing. Hence
 *  the Providers card (what's installed and keyed) sitting next to an editable
 *  Model card (what's selected). */
export function SettingsPanel() {
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [models, setModels] = useState<Record<string, ProviderModels>>({});
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(() => {
    setError(null);
    fetchConfig()
      .then(setConfig)
      .catch((e) => setError(String(e)));
    // Reloaded alongside the config because adding a key changes what can be
    // listed: the provider is only askable once it has one.
    listModels()
      .then(setModels)
      .catch(() => setModels({}));  // a picker with no options still beats an error
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async (field: "key" | "model" | "flash_model", value: string) => {
    const val = value.trim();
    if (!val) return;
    setSaving(true);
    setMessage(null);
    try {
      await updateConfig({ [field]: val });
      setMessage({
        ok: true,
        text: field === "key" ? "Key saved — in effect immediately." : "Model updated.",
      });
      setEditing(null);
      setDraft("");
      load(); // refresh to show the new state
    } catch (e) {
      // The server rejects a model whose provider isn't installed or keyed,
      // and says which — surface that rather than a generic failure.
      setMessage({ ok: false, text: String(e).replace(/^Error:\s*/, "") });
    } finally {
      setSaving(false);
    }
  };

  const editor = (field: "key" | "model" | "flash_model", placeholder: string) => (
    <div className="space-y-2">
      <input
        type={field === "key" ? "password" : "text"}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save(field, draft);
          if (e.key === "Escape") { setEditing(null); setDraft(""); }
        }}
        placeholder={placeholder}
        autoFocus
        className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 font-mono text-xs text-neutral-200 placeholder:text-neutral-600 focus:border-[#4f8dff]/50 focus:outline-none"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={() => save(field, draft)}
          disabled={saving || !draft.trim()}
          className="rounded-full bg-[#4f8dff] px-3 py-1 text-xs font-medium text-white transition hover:bg-[#3b6fcc] disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={() => { setEditing(null); setDraft(""); setMessage(null); }}
          className="rounded-full px-3 py-1 text-xs font-medium text-neutral-400 transition hover:bg-white/5 hover:text-neutral-100"
        >
          Cancel
        </button>
      </div>
    </div>
  );

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

  const activeProvider = config.model.includes(":")
    ? config.model.split(":")[0]
    : config.provider;

  /** A model row: a select grouped by provider, with anything unusable shown
   *  but disabled, plus a Custom… escape for models newer than the list or
   *  pinned to a dated snapshot. */
  const modelRow = (label: string, field: "model" | "flash_model", value: string) => {
    const spec = (provider: string, model: string) =>
      provider === config.provider ? model : `${provider}:${model}`;

    // The configured value may not be in any list (a snapshot, or a model the
    // provider stopped advertising) — it still has to be selectable, or
    // opening this panel would silently change the setting.
    const known = Object.entries(models).some(([p, s]) =>
      s.models.some((m) => spec(p, m) === value),
    );

    if (editing === field) {
      return (
        <div className="flex items-baseline justify-between gap-4">
          <dt className="shrink-0 text-xs text-neutral-400">{label}</dt>
          <dd className="min-w-0 flex-1">
            {editor(field, "provider:model, e.g. anthropic:claude-sonnet-5")}
          </dd>
        </div>
      );
    }

    return (
      <div className="flex items-baseline justify-between gap-4">
        <dt className="shrink-0 text-xs text-neutral-400">{label}</dt>
        <dd className="min-w-0 flex-1">
          <select
            value={value}
            disabled={saving}
            onChange={(e) => {
              if (e.target.value === "__custom__") {
                setEditing(field);
                setDraft(value);
                setMessage(null);
                return;
              }
              save(field, e.target.value);
            }}
            className="glass-select w-full cursor-pointer truncate rounded-lg border border-white/10 px-2 py-1 font-mono text-xs text-neutral-200 focus:border-[#4f8dff]/50 focus:outline-none"
          >
            {!known && <option value={value}>{value}</option>}
            {Object.entries(models).map(([provider, state]) => (
              <optgroup
                key={provider}
                label={
                  state.key_configured
                    ? provider
                    : `${provider} — ${state.installed ? "no key" : "not installed"}`
                }
              >
                {state.models.map((model) => (
                  <option
                    key={`${provider}/${model}`}
                    value={spec(provider, model)}
                    // Unusable without a key or the package — visible so you
                    // know it exists, unselectable so you can't half-set it.
                    disabled={!state.key_configured || !state.installed}
                  >
                    {model}
                  </option>
                ))}
              </optgroup>
            ))}
            <option value="__custom__">Custom…</option>
          </select>
        </dd>
      </div>
    );
  };

  return (
    <div className="themed-scroll flex-1 space-y-3 overflow-y-auto p-4">
      {/* Providers — what this install can run, and what it's missing */}
      <div className="liquid-glass-subtle rounded-xl px-4 py-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-neutral-500">Providers</h3>
        <div className="space-y-1.5">
          {Object.entries(config.providers ?? {}).map(([name, state]) => (
            <div key={name} className="flex items-baseline justify-between gap-3">
              <dt className="flex shrink-0 items-baseline gap-2 text-xs text-neutral-300">
                {name}
                {name === activeProvider && (
                  <span className="rounded bg-[#4f8dff]/15 px-1.5 py-0.5 text-[10px] text-[#4f8dff]">in use</span>
                )}
              </dt>
              <dd className="flex items-baseline gap-2 text-xs">
                <span className={state.installed ? "text-emerald-400" : "text-neutral-500"}>
                  {state.installed ? "installed" : "not installed"}
                </span>
                <span className="text-neutral-600">·</span>
                <span className={state.key_configured ? "text-emerald-400" : "text-neutral-500"}>
                  {state.key_configured ? "key set" : "no key"}
                </span>
              </dd>
            </div>
          ))}
        </div>

        {/* A missing package and a missing key are different problems. */}
        {Object.entries(config.providers ?? {})
          .filter(([, s]) => !s.installed)
          .map(([name]) => (
            <p key={name} className="mt-1.5 font-mono text-[11px] text-neutral-500">
              uv sync --extra {name}
            </p>
          ))}

        <div className="mt-3 border-t border-white/5 pt-2">
          {editing === "key" ? (
            editor("key", "sk-… or sk-ant-…")
          ) : (
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-500">
                Paste a key — the provider is detected from it
              </span>
              <button
                onClick={() => { setEditing("key"); setDraft(""); setMessage(null); }}
                className="rounded-full px-2.5 py-0.5 text-xs font-medium text-[#4f8dff] transition hover:bg-[#4f8dff]/10"
              >
                Add key
              </button>
            </div>
          )}
        </div>

        {message && (
          <p className={`mt-1.5 text-xs ${message.ok ? "text-emerald-400" : "text-red-400"}`}>
            {message.text}
          </p>
        )}
      </div>

      {/* Model — what actually decides which provider runs */}
      <div className="liquid-glass-subtle rounded-xl px-4 py-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-neutral-500">Model</h3>
        <dl className="space-y-1.5">
          {modelRow("Main", "model", config.model)}
          {modelRow("Flash", "flash_model", config.flash_model)}
          <div className="flex items-baseline justify-between gap-4">
            <dt className="shrink-0 text-xs text-neutral-400">API base</dt>
            <dd className="truncate text-right font-mono text-xs text-neutral-200">{config.api_base}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-4">
            <dt className="shrink-0 text-xs text-neutral-400">Temperature</dt>
            <dd className="truncate text-right font-mono text-xs text-neutral-200">{config.temperature}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-4">
            <dt className="shrink-0 text-xs text-neutral-400">Max tokens</dt>
            <dd className="truncate text-right font-mono text-xs text-neutral-200">
              {config.max_tokens.toLocaleString()}
            </dd>
          </div>
        </dl>
        <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
          Main runs the agent; Flash runs sub-agents and summarisation. Either takes a
          <span className="font-mono"> provider:model </span>
          spec, so the main agent can use a stronger model while sub-agents stay cheap.
        </p>
      </div>

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

      {section("Keyboard", SHORTCUTS)}

      {section("Browser (PinchTab)", [
        ["URL", config.pinchtab_base],
        ["Healthy", config.pinchtab_healthy],
      ])}
    </div>
  );
}
