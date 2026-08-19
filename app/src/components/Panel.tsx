import { getCurrentWindow } from "@tauri-apps/api/window";
import type { AgentStatus, ChatMessage, DictationStatus, View, VoiceModelDownloadPayload, VoiceModelStatus } from "../types";
import type { AgentConfig } from "../api";
import { hasCompletedTurn, isSessionBusy, sessionTotals } from "../sessionEvents";
import { ChatInput } from "./ChatInput";
import { ErrorBanner } from "./ErrorBanner";
import { MessageList } from "./MessageList";
import { SettingsPanel } from "./SettingsPanel";
import { SoundWave } from "./SoundWave";
import { TerminalPanel } from "./TerminalPanel";
import { VoiceIndicator } from "./VoiceIndicator";
import { SkillsPanel } from "./SkillsPanel";
import { VaultPanel } from "./VaultPanel";
import { ChatStatusBar } from "./ChatStatusBar";

interface PanelProps {
  /** Active session only — gates ChatInput. */
  busy: boolean;
  /** Global: any session (chat or CLI) is running. Drives the header dot. */
  globalBusy: boolean;
  /** A turn finished while collapsed — green dot until user expands. */
  unreadCompletion?: boolean;
  /** Whether the panel is actually on screen. It stays mounted while
   *  collapsed (App.tsx), so anything that measures or focuses itself needs
   *  to know the difference between "mounted" and "visible". */
  expanded: boolean;
  status: AgentStatus | null;
  view: View;
  onViewChange: (view: View) => void;
  onCollapse: () => void;
  /** All chat sessions, keyed by session uuid; `messages`/`busy` below
   *  stay scoped to the active session while the chips row shows all. */
  sessions: Record<string, ChatMessage[]>;
  sessionOrder: string[];
  activeSessionId: string | null;
  onSelectChat: (id: string) => void;
  onCloseChat: (id: string) => void;
  /** From GET /config — the same source the CLI's status bar reads. */
  config: AgentConfig | null;
  onAddChat: () => void;
  messages: ChatMessage[];
  onSend: (text: string) => void;
  terminalTabs: string[];
  activeTerminalId: string | null;
  initialTerminalCommands: Record<string, string>;
  onActivateTerminal: (id: string) => void;
  onCloseTerminal: (id: string) => void;
  onAddTerminal: () => void;
  /** CLI/external session ids the app doesn't own — read-only chips. */
  cliSessions?: string[];
  /** Voice dictation state + model — drives the mic chip. */
  dictation: DictationStatus;
  voiceModel: VoiceModelStatus | null;
  voiceModelDownload: VoiceModelDownloadPayload | null;
  onRefreshVoiceModel: () => void;
}

const STATUS_DOT =
  "h-1.5 w-1.5 rounded-full";

/** Chip label: the first user message, whitespace-collapsed and truncated;
 *  "chat N" before the session has said anything. */
function chatTitle(list: ChatMessage[], index: number): string {
  const title = list.find((m) => m.role === "user")?.content.replace(/\s+/g, " ").trim() ?? "";
  if (!title) return `chat ${index + 1}`;
  return title.length > 18 ? `${title.slice(0, 18)}…` : title;
}

export function Panel({
  busy,
  globalBusy,
  unreadCompletion,
  expanded,
  status,
  view,
  onViewChange,
  onCollapse,
  sessions,
  sessionOrder,
  activeSessionId,
  onSelectChat,
  onCloseChat,
  config,
  onAddChat,
  messages,
  onSend,
  terminalTabs,
  activeTerminalId,
  initialTerminalCommands,
  onActivateTerminal,
  onCloseTerminal,
  onAddTerminal,
  cliSessions,
  dictation,
  voiceModel,
  voiceModelDownload,
  onRefreshVoiceModel,
}: PanelProps) {
  const dot =
    globalBusy
      ? "bg-[#4f8dff] animate-daimon-pulse"
      : !status?.running
        ? "bg-red-400"
        : unreadCompletion
          ? "bg-emerald-400"
          : "bg-neutral-600";
  const statusTitle = status ? `agent on port ${status.port}` : "agent offline";

  return (
    // The entry animation used to come for free from the panel mounting on
    // every expand. It doesn't remount any more (App.tsx keeps it alive so the
    // terminals inside survive), so the class is applied only while expanded:
    // dropping it on collapse and re-adding it on expand is what restarts the
    // animation.
    <div
      className={`${expanded ? "animate-daimon-in" : ""} liquid-glass flex h-full w-full flex-col overflow-hidden rounded-[28px] text-white`}
    >
      <header
        className="flex shrink-0 items-center gap-2 border-b border-white/[0.08] px-4 py-3 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.06)]"
        onMouseDown={(e) => {
          // The whole header bar drags the window — except when the press
          // started on a button (tab chips, new-chat, collapse), which keeps
          // its own click behavior.
          if ((e.target as HTMLElement).closest("button")) return;
          getCurrentWindow().startDragging();
        }}
      >
        <img src="/logo.svg" alt="" draggable={false} className="h-5 w-5" />
        <span className="text-sm font-semibold tracking-tight text-neutral-50">daimon</span>
        <span className={`${STATUS_DOT} ${dot}`} title={statusTitle} />
        {/* Dictation state inline — SoundWave while recording, spinner while
            transcribing, error text. Matches the legacy header layout where
            the dictation indicator sits next to the status dot, before the
            tabs, rather than after them. */}
        {dictation.type === "recording" && (
          <span className="flex items-center gap-1.5 font-mono text-xs text-[#4f8dff]">
            <SoundWave barHeight={10} />
            {dictation.locked && (
              // Two small static dots — visibly steadier than the moving
              // wave alone, signaling "hands-free, keeps going until you
              // press fn again" rather than "only while held."
              <span className="flex items-center gap-1">
                <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
                <span className="h-1 w-1 rounded-full bg-[#4f8dff]" />
              </span>
            )}
          </span>
        )}
        {dictation.type === "transcribing" && (
          <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        )}
        {dictation.type === "error" && (
          <span className="text-xs text-red-400">{dictation.message || "dictation error"}</span>
        )}
        <div className="flex-1" />
        <button
          onClick={() => onViewChange("chat")}
          className={`rounded-full px-2.5 py-1 text-xs font-medium tracking-tight transition duration-200 ${
            view === "chat"
              ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
              : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
          }`}
        >
          chat
        </button>
        <button
          onClick={() => onViewChange("terminal")}
          className={`rounded-full px-2.5 py-1 text-xs font-medium tracking-tight transition duration-200 ${
            view === "terminal"
              ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
              : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
          }`}
        >
          terminal
        </button>
        <button
          onClick={() => onViewChange("vault")}
          className={`rounded-full px-2.5 py-1 text-xs font-medium tracking-tight transition duration-200 ${
            view === "vault"
              ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
              : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
          }`}
        >
          vault
        </button>
        <button
          onClick={() => onViewChange("skills")}
          className={`rounded-full px-2.5 py-1 text-xs font-medium tracking-tight transition duration-200 ${
            view === "skills"
              ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
              : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
          }`}
        >
          skills
        </button>
        <button
          onClick={() => onViewChange("settings")}
          className={`rounded-full px-2.5 py-1 text-xs font-medium tracking-tight transition duration-200 ${
            view === "settings"
              ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
              : "text-neutral-400 hover:bg-white/5 hover:text-neutral-100"
          }`}
        >
          settings
        </button>
        <VoiceIndicator
          model={voiceModel}
          modelDownload={voiceModelDownload}
          onRefreshModel={onRefreshVoiceModel}
        />
        <button
          onClick={onCollapse}
          className="rounded-full px-2 py-1 text-xs font-medium text-neutral-400 transition duration-200 hover:bg-white/5 hover:text-neutral-100 active:scale-90"
        >
          collapse
        </button>
      </header>

      {/* Same chip-row pattern as the terminal view below — multiple
          concurrent chat sessions, each its own thread on the server.
          Only shown while the chat view itself is active. */}
      {view === "chat" && (
        <div className="themed-scroll flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-white/[0.08] px-3 py-2">
          {sessionOrder.map((id, index) => {
            const list = sessions[id] ?? [];
            return (
              <span
                key={id}
                className={`liquid-glass-subtle flex shrink-0 items-center gap-1 rounded-full pl-2.5 pr-1 py-1 text-xs transition duration-200 ${
                  id === activeSessionId
                    ? "text-neutral-100 [border-color:rgba(79,141,255,0.45)] [box-shadow:inset_0_1px_0_0_rgba(255,255,255,0.2)]"
                    : "text-neutral-400 hover:text-neutral-100"
                }`}
              >
                <button onClick={() => onSelectChat(id)} className="flex items-center gap-1.5">
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      isSessionBusy(list)
                        ? "bg-[#4f8dff] animate-daimon-pulse"
                        : hasCompletedTurn(list)
                          ? "bg-emerald-400"
                          : "bg-neutral-600"
                    }`}
                  />
                  {chatTitle(list, index)}
                </button>
                <button
                  onClick={() => onCloseChat(id)}
                  title="close chat"
                  className="rounded-full px-1 text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90"
                >
                  ×
                </button>
              </span>
            );
          })}
          {cliSessions?.map((name) => (
            <span
              key={`cli-${name}`}
              title={`CLI session "${name}" is active`}
              className="liquid-glass-subtle flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-neutral-400"
            >
              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400 animate-daimon-pulse" />
              {name}
            </span>
          ))}
          <button
            onClick={onAddChat}
            title="start a new chat"
            className="liquid-glass-subtle flex shrink-0 items-center justify-center rounded-full px-2 py-1 text-xs text-neutral-400 transition duration-200 hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)] active:scale-90"
          >
            +
          </button>
        </div>
      )}

      {view === "terminal" && (
        <div className="themed-scroll flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-white/[0.08] px-3 py-2">
          {terminalTabs.map((id, index) => (
            <span
              key={id}
              className={`liquid-glass-subtle flex shrink-0 items-center gap-1 rounded-full pl-2.5 pr-1 py-1 text-xs transition duration-200 ${
                id === activeTerminalId
                  ? "text-neutral-100 [border-color:rgba(79,141,255,0.45)] [box-shadow:inset_0_1px_0_0_rgba(255,255,255,0.2)]"
                  : "text-neutral-400 hover:text-neutral-100"
              }`}
            >
              <button onClick={() => onActivateTerminal(id)} className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-[#4f8dff]" />
                terminal {index + 1}
              </button>
              <button
                onClick={() => onCloseTerminal(id)}
                title="close terminal"
                className="rounded-full px-1 text-neutral-500 transition hover:bg-white/10 hover:text-neutral-200 active:scale-90"
              >
                ×
              </button>
            </span>
          ))}
          <button
            onClick={onAddTerminal}
            title="open a new terminal"
            className="liquid-glass-subtle flex shrink-0 items-center justify-center rounded-full px-2 py-1 text-xs text-neutral-400 transition duration-200 hover:text-[#4f8dff] hover:[border-color:rgba(79,141,255,0.4)] active:scale-90"
          >
            +
          </button>
        </div>
      )}

      {view === "chat" && (
        <div className="flex min-h-0 flex-1 flex-col">
          <MessageList messages={messages} />
          <ErrorBanner
            message={
              status?.running === false
                ? "Agent server is offline — start it with `uv run daimon-agent` (cwd agents/) or check agents/.env"
                : null
            }
          />
          {/* The input and its status line are one unit: `relative` anchors the
              floating line, `pb-5` reserves the space it sits in without the
              line itself occupying a row in this column. */}
          <div className="relative shrink-0 pb-6">
            <ChatInput onSend={onSend} disabled={busy} />
            <ChatStatusBar
              model={config?.model ?? "…"}
              contextWindow={config?.context_window ?? 128000}
              busy={busy}
              {...sessionTotals(messages)}
            />
          </div>
        </div>
      )}

      {/* Hidden rather than unmounted when another view is showing. The PTYs
          themselves live in Rust and keep running either way, but the rendered
          scrollback lives in the xterm instance here — unmounting disposed it,
          and a shell has no reason to reprint what it already printed, so
          coming back to the terminal showed a blank or half-drawn screen until
          something forced a redraw. Resizing the window was that something,
          which is why it looked like a resize "fixed" it.

          `min-h-0 flex-1` only apply while it's the visible view; the inline
          display flip is what takes it out of the layout otherwise. */}
      <div
        className={view === "terminal" ? "min-h-0 flex-1 flex-col" : ""}
        style={{ display: view === "terminal" ? "flex" : "none" }}
      >
        {/* One TerminalPanel per tab, each kept mounted even when not the
            active one (hidden via display:none) so shell scrollback
            survives tab switches — the real PTY processes live in Rust,
            but the xterm instance's rendered buffer lives here. */}
        {terminalTabs.map((id) => (
          <div
            key={id}
            className="min-h-0 flex-1"
            style={{ display: id === activeTerminalId ? "flex" : "none" }}
          >
            <TerminalPanel
              id={id}
              // Not just the active *tab*: the active tab of a view nobody is
              // looking at — or of a panel collapsed to the pill — is still off
              // screen, and a terminal that thinks it's visible will grab focus
              // and try to measure a container that has no box. Going false and
              // back true on expand is also what re-fits and repaints it.
              active={expanded && view === "terminal" && id === activeTerminalId}
              initialCommand={initialTerminalCommands[id]}
            />
          </div>
        ))}
        {terminalTabs.length === 0 && (
          <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-neutral-500">
            Press <span className="mx-1 rounded bg-white/10 px-1.5 py-0.5 text-neutral-300">+</span> to open a
            terminal — or ask Daimon a question that needs shell access.
          </div>
        )}
      </div>

      {view === "vault" && <VaultPanel />}
      {view === "skills" && <SkillsPanel />}
      {view === "settings" && <SettingsPanel />}
    </div>
  );
}
