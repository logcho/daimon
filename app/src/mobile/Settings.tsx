import { useEffect, useState } from "react";
import type { BusClient } from "../lib/busClient";
import { disablePush, enablePush, pushState, type PushState } from "./notifications";
import { forgetToken } from "./transport";

interface SkillRow {
  name: string;
  description: string;
  source: "vault" | "project";
}

interface DeviceRow {
  id: string;
  label: string;
  created_at: number;
}

/** Notifications, what the agent is configured with, the skill library, and
 *  the devices allowed in. Read-only apart from the two things that only make
 *  sense from the phone itself: turning notifications on, and cutting a device
 *  off. */
export function Settings({
  bus,
  workspace,
  onSignOut,
}: {
  bus: BusClient;
  workspace: string;
  onSignOut: () => void;
}) {
  const [push, setPush] = useState<PushState>(() => pushState());
  const [pushError, setPushError] = useState<string | null>(null);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [openSkill, setOpenSkill] = useState<{ name: string; content: string } | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [devices, setDevices] = useState<DeviceRow[]>([]);

  useEffect(() => {
    void (async () => {
      const [skillAck, configAck] = await Promise.all([
        bus.send("skills", { workspace }),
        bus.send("config", { workspace }),
      ]);
      if (skillAck.ok) setSkills((skillAck.skills ?? []) as SkillRow[]);
      if (configAck.ok) setConfig(configAck.config as Record<string, unknown>);
    })();
    void fetch("/devices", { headers: authHeader() })
      .then((r) => (r.ok ? r.json() : { devices: [] }))
      .then((body) => setDevices(body.devices ?? []))
      .catch(() => {});
  }, [bus, workspace]);

  async function toggleNotifications() {
    setPushError(null);
    if (push === "on") {
      await disablePush();
      setPush("off");
      return;
    }
    const result = await enablePush();
    if (result.ok) setPush("on");
    else setPushError(result.error ?? "could not turn notifications on");
  }

  async function revoke(id: string) {
    await fetch(`/devices/${id}`, { method: "DELETE", headers: authHeader() });
    setDevices((prev) => prev.filter((d) => d.id !== id));
  }

  if (openSkill) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-white/10 px-3 py-2">
          <button onClick={() => setOpenSkill(null)} className="text-sm text-neutral-400">‹ settings</button>
          <span className="min-w-0 flex-1 truncate text-xs text-neutral-500">{openSkill.name}</span>
        </div>
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap px-4 py-3 font-mono text-xs text-neutral-300">
          {openSkill.content}
        </pre>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-3 py-3">
      <Section title="notifications">
        <button
          onClick={toggleNotifications}
          disabled={push === "unsupported" || push === "needs-home-screen"}
          className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-left text-sm active:bg-white/10 disabled:opacity-50"
        >
          {push === "on" && "tell me when a session is waiting  ·  on"}
          {push === "off" && "tell me when a session is waiting  ·  off"}
          {push === "denied" && "notifications are blocked in this browser"}
          {push === "unsupported" && "this browser can't do notifications"}
          {/* The one case worth explaining rather than just disabling: iOS
              refuses push for a tab, and nothing on screen would say why. */}
          {push === "needs-home-screen" && "add this page to your home screen first"}
        </button>
        {pushError && <p className="mt-2 px-1 text-xs text-red-400">{pushError}</p>}
        <p className="mt-2 px-1 text-xs leading-relaxed text-neutral-600">
          Notifications say which session wants you, never what it asked — the
          payload passes through Apple's push service on the way here.
        </p>
      </Section>

      <Section title="agent">
        {config ? (
          <dl className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-xs">
            <Row label="model" value={String(config.model ?? "—")} />
            <Row label="provider" value={String(config.provider ?? "—")} />
            <Row label="api key" value={config.api_key_configured ? "configured" : "missing"} />
            <Row label="workspace" value={String(config.workspace ?? "—")} />
          </dl>
        ) : (
          <p className="px-1 text-xs text-neutral-600">loading…</p>
        )}
      </Section>

      <Section title={`skills (${skills.length})`}>
        {skills.length === 0 && <p className="px-1 text-xs text-neutral-600">none yet</p>}
        {skills.map((skill) => (
          <button
            key={skill.name}
            onClick={async () => {
              const ack = await bus.send("skill.read", { workspace, name: skill.name });
              if (ack.ok) setOpenSkill({ name: skill.name, content: String(ack.content ?? "") });
            }}
            className="mb-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-left active:bg-white/10"
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm">{skill.name}</span>
              {skill.source === "project" && (
                <span className="shrink-0 rounded-full bg-white/10 px-2 py-0.5 text-[10px] text-neutral-400">
                  project
                </span>
              )}
            </div>
            <div className="mt-0.5 line-clamp-2 text-xs text-neutral-500">{skill.description}</div>
          </button>
        ))}
      </Section>

      <Section title="paired devices">
        {devices.map((device) => (
          <div
            key={device.id}
            className="mb-2 flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-4 py-3"
          >
            <span className="min-w-0 flex-1 truncate text-sm">{device.label}</span>
            <button onClick={() => revoke(device.id)} className="shrink-0 text-xs text-red-400">
              revoke
            </button>
          </div>
        ))}
        <p className="mt-1 px-1 text-xs text-neutral-600">
          Revoking cuts a device off immediately, including its notifications.
        </p>
      </Section>

      <button
        onClick={() => {
          forgetToken();
          onSignOut();
        }}
        className="mb-8 mt-6 w-full py-3 text-xs text-neutral-600"
      >
        forget this device
      </button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-6">
      <h2 className="mb-2 px-1 text-xs uppercase tracking-wide text-neutral-500">{title}</h2>
      {children}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3 py-1">
      <dt className="w-20 shrink-0 text-neutral-500">{label}</dt>
      <dd className="min-w-0 flex-1 truncate text-neutral-200">{value}</dd>
    </div>
  );
}

function authHeader(): Record<string, string> {
  try {
    const token = window.localStorage.getItem("daimon-remote-token");
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}
