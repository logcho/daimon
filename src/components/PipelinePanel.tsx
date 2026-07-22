import { useEffect, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { Session, TaskStep } from "../types";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { Settings } from "./Settings";
import { VaultPanel } from "./VaultPanel";
import { AutomationsPanel } from "./AutomationsPanel";

type View = "chat" | "vault" | "automations" | "settings";

// Matches `@tauri-apps/api/window`'s internal (unexported) `ResizeDirection`
// union — structurally identical, so it type-checks against
// `startResizeDragging` without needing that type exported.
type ResizeDirection = "East" | "North" | "NorthEast" | "NorthWest" | "South" | "SouthEast" | "SouthWest" | "West";

// The window has `decorations: false` (no native title bar/frame), so macOS
// never gives it edge-hover resize cursors or drag-to-resize on its own —
// that's normally a side effect of the native chrome this window doesn't
// have. These invisible strips (living in the outer p-6 margin around the
// visible rounded panel, see the glow-clipping note in the design system
// skill for why that margin exists) are the documented Tauri workaround:
// call `startResizeDragging` on pointer-down and let the OS take over the
// drag from there.
const RESIZE_HANDLES: { direction: ResizeDirection; className: string }[] = [
  { direction: "North", className: "inset-x-3 top-0 h-2 cursor-ns-resize" },
  { direction: "South", className: "inset-x-3 bottom-0 h-2 cursor-ns-resize" },
  { direction: "West", className: "inset-y-3 left-0 w-2 cursor-ew-resize" },
  { direction: "East", className: "inset-y-3 right-0 w-2 cursor-ew-resize" },
  { direction: "NorthWest", className: "left-0 top-0 h-3 w-3 cursor-nwse-resize" },
  { direction: "NorthEast", className: "right-0 top-0 h-3 w-3 cursor-nesw-resize" },
  { direction: "SouthWest", className: "left-0 bottom-0 h-3 w-3 cursor-nesw-resize" },
  { direction: "SouthEast", className: "right-0 bottom-0 h-3 w-3 cursor-nwse-resize" },
];

function ResizeHandles() {
  return (
    <>
      {RESIZE_HANDLES.map(({ direction, className }) => (
        <div
          key={direction}
          onPointerDown={() => getCurrentWindow().startResizeDragging(direction)}
          className={`absolute z-10 ${className}`}
        />
      ))}
    </>
  );
}

function StepLine({ step }: { step: TaskStep }) {
  if (step.label === "Thinking" && step.status === "running") {
    return <ThinkingIndicator />;
  }

  const name = step.tool ?? step.label;

  if (step.status === "done") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="text-emerald-400">✓</span>
        <span className="text-neutral-400">{name}</span>
      </span>
    );
  }
  if (step.status === "error") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="text-red-400">✕</span>
        <span className="text-neutral-400">{name}</span>
      </span>
    );
  }
  if (step.status === "running") {
    return (
      <span className="flex items-center gap-2 font-mono text-sm">
        <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        <span className="text-[#4f8dff]">{name}</span>
      </span>
    );
  }
  return (
    <span className="flex items-center gap-2 font-mono text-sm">
      <span className="block h-2 w-2 rounded-full border border-neutral-600" />
      <span className="text-neutral-500">{name}</span>
    </span>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-2.5 py-1 font-mono text-xs transition ${
        active ? "bg-[#4f8dff]/15 text-[#4f8dff]" : "text-neutral-500 hover:text-neutral-200"
      }`}
    >
      {children}
    </button>
  );
}

function sessionDotColor(session: Session): string {
  const turn = session.turns[session.turns.length - 1];
  if (turn?.error) return "bg-red-400";
  if (turn?.result) return "bg-emerald-400";
  return "bg-[#4f8dff]";
}

function SessionChip({
  session,
  active,
  onClick,
  onClose,
}: {
  session: Session;
  active: boolean;
  onClick: () => void;
  onClose: () => void;
}) {
  const turn = session.turns[session.turns.length - 1];
  const running = turn && !turn.result && !turn.error;
  // Labeled with the *first* turn's instruction so the chip reads as a
  // stable conversation title instead of relabeling itself every message.
  const title = session.turns[0]?.instruction ?? "";
  return (
    <span
      className={`flex shrink-0 items-center gap-1 rounded-full border pl-2.5 pr-1 py-1 font-mono text-xs transition ${
        active
          ? "border-[#4f8dff]/40 bg-[#4f8dff]/15 text-neutral-100"
          : "border-white/10 bg-white/5 text-neutral-500 hover:text-neutral-200"
      }`}
    >
      <button onClick={onClick} title={title} className="flex items-center gap-1.5">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${sessionDotColor(session)} ${running ? "animate-daimon-pulse" : ""}`} />
        <span className="max-w-[9rem] truncate">{title}</span>
      </button>
      <button
        onClick={onClose}
        title="close session"
        className="rounded-full px-1 text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90"
      >
        ×
      </button>
    </span>
  );
}

export function PipelinePanel({
  sessions,
  activeSession,
  onSelectSession,
  onNewSession,
  onCloseSession,
  onCollapse,
  onSubmit,
}: {
  sessions: Session[];
  activeSession: Session | null;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onCloseSession: (sessionId: string) => void;
  onCollapse: () => void;
  onSubmit: (instruction: string) => void;
}) {
  const [view, setView] = useState<View>("chat");
  const [draft, setDraft] = useState("");
  const historyRef = useRef<HTMLDivElement>(null);

  const activeTurns = activeSession?.turns ?? [];
  const lastTurn = activeTurns[activeTurns.length - 1];
  const totalStepCount = activeTurns.reduce((sum, t) => sum + t.steps.length, 0);
  // The agent server threads conversation history across messages in a
  // session (Phase 8) via one shared, growing array per container — sending
  // a second message before the first turn resolves would race two turns
  // against that same array. Turns within a session are sequential by
  // design, so block submission until the active session's last turn has a
  // result or error (starting a brand-new session is unaffected — nothing to
  // race there).
  const activeTurnInFlight = Boolean(activeSession && lastTurn && !lastTurn.result && !lastTurn.error);

  useEffect(() => {
    historyRef.current?.scrollTo({ top: historyRef.current.scrollHeight, behavior: "smooth" });
  }, [activeSession?.id, activeTurns.length, totalStepCount, lastTurn?.result, lastTurn?.error]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (activeTurnInFlight) return;
    const instruction = draft.trim();
    if (!instruction) return;
    onSubmit(instruction);
    setDraft("");
  }

  // There must always be a way to start a second session once one exists —
  // so show the chip row (with its trailing "+") whenever any session is
  // open, not just once there are already multiple.
  const showChipRow = view === "chat" && sessions.length > 0;

  return (
    <div className="relative flex h-full w-full items-center justify-center p-6">
      <ResizeHandles />
      <div className="animate-daimon-in flex h-full w-full flex-col rounded-[28px] border border-white/10 bg-neutral-950/95 text-neutral-200 shadow-[0_0_40px_-12px_rgba(79,141,255,0.25)] backdrop-blur-2xl">
        <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
          <img src="/logo.svg" alt="" className="h-5 w-5 invert" />
          <span className="font-mono text-sm font-medium text-neutral-100">daimon</span>
          <span className="h-3.5 w-1.5 animate-pulse bg-[#4f8dff]/70" />
          <div className="flex-1" />
          <TabButton active={view === "chat"} onClick={() => setView("chat")}>
            chat
          </TabButton>
          <TabButton active={view === "vault"} onClick={() => setView("vault")}>
            vault
          </TabButton>
          <TabButton active={view === "automations"} onClick={() => setView("automations")}>
            automations
          </TabButton>
          <TabButton active={view === "settings"} onClick={() => setView("settings")}>
            settings
          </TabButton>
          <button
            onClick={onCollapse}
            className="rounded-md px-2 py-1 font-mono text-xs text-neutral-500 transition hover:bg-white/5 hover:text-neutral-200 active:scale-90"
          >
            collapse
          </button>
        </div>

        {showChipRow && (
          <div className="themed-scroll flex items-center gap-1.5 overflow-x-auto border-b border-white/10 px-3 py-2">
            {sessions.map((s) => (
              <SessionChip
                key={s.id}
                session={s}
                active={s.id === activeSession?.id}
                onClick={() => onSelectSession(s.id)}
                onClose={() => onCloseSession(s.id)}
              />
            ))}
            <button
              onClick={onNewSession}
              title="start a new session"
              className="flex shrink-0 items-center justify-center rounded-full border border-white/10 bg-white/5 px-2 py-1 font-mono text-xs text-neutral-500 transition hover:border-[#4f8dff]/40 hover:text-[#4f8dff] active:scale-90"
            >
              +
            </button>
          </div>
        )}

        {view === "settings" ? (
          <Settings />
        ) : view === "vault" ? (
          <VaultPanel />
        ) : view === "automations" ? (
          <AutomationsPanel />
        ) : (
          <div ref={historyRef} className="themed-scroll flex-1 space-y-3 overflow-y-auto p-4">
            {!activeSession && (
              <p className="font-mono text-sm text-neutral-500">no active session — give daimon something to do.</p>
            )}
            {activeSession &&
              activeSession.turns.map((turn) => (
                <div key={turn.id} className="space-y-3">
                  <div className="flex justify-end">
                    <div className="max-w-[85%] rounded-2xl rounded-br-sm border border-[#4f8dff]/20 bg-[#4f8dff]/15 px-3 py-2 font-mono text-sm text-neutral-100">
                      <span className="text-[#4f8dff]">{">"}</span> {turn.instruction}
                    </div>
                  </div>

                  <div className="flex justify-start">
                    <div className="max-w-[85%] space-y-2 rounded-2xl rounded-bl-sm border border-white/10 bg-white/5 px-3 py-2">
                      <ul className="space-y-1.5">
                        {turn.steps.map((step) => (
                          <li key={step.id}>
                            <StepLine step={step} />
                          </li>
                        ))}
                      </ul>
                      {turn.result && <p className="font-mono text-sm text-emerald-400">{turn.result}</p>}
                      {turn.error && <p className="font-mono text-sm text-red-400">{turn.error}</p>}
                    </div>
                  </div>
                </div>
              ))}
          </div>
        )}

        {view === "chat" && (
          <form onSubmit={handleSubmit} className="flex items-center gap-2 border-t border-white/10 p-3">
            <span className="pl-1 font-mono text-sm text-[#4f8dff]">{">"}</span>
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={activeTurnInFlight}
              placeholder={activeTurnInFlight ? "waiting for daimon to finish this turn…" : "tell daimon what to do..."}
              className="w-full bg-transparent font-mono text-sm text-neutral-100 placeholder:text-neutral-600 focus:outline-none disabled:opacity-40"
            />
          </form>
        )}
      </div>
    </div>
  );
}
