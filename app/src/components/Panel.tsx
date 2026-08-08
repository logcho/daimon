import { getCurrentWindow } from "@tauri-apps/api/window";
import type { AgentStatus, ChatMessage, View } from "../types";
import { isSessionBusy } from "../sessionEvents";
import { ChatInput } from "./ChatInput";
import { ErrorBanner } from "./ErrorBanner";
import { MessageList } from "./MessageList";
import { TerminalPanel } from "./TerminalPanel";

interface PanelProps {
  /** Active session only — gates ChatInput. */
  busy: boolean;
  /** Global: any session (chat or CLI) is running. Drives the header dot. */
  globalBusy: boolean;
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
}

const STATUS_DOT =
  "h-1.5 w-1.5 rounded-full border border-neutral-950";

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
  status,
  view,
  onViewChange,
  onCollapse,
  sessions,
  sessionOrder,
  activeSessionId,
  onSelectChat,
  onCloseChat,
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
}: PanelProps) {
  const dot =
    globalBusy
      ? "bg-[#4f8dff] animate-daimon-pulse"
      : status?.running
        ? status.adopted ? "bg-amber-400" : "bg-emerald-400"
        : "bg-red-400";
  const statusTitle = status ? `agent on port ${status.port}${status.adopted ? " (adopted)" : ""}` : "agent offline";

  return (
    <div className="animate-daimon-in liquid-glass flex h-full w-full flex-col overflow-hidden rounded-[28px] text-white">
      <header
        className="flex items-center gap-3 border-b border-white/[0.08] px-4 py-3 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.06)]"
        onMouseDown={(e) => {
          // The whole header bar drags the window — except when the press
          // started on a button (tab chips, new-chat, collapse), which keeps
          // its own click behavior.
          if ((e.target as HTMLElement).closest("button")) return;
          getCurrentWindow().startDragging();
        }}
      >
        <img src="/logo.svg" alt="" draggable={false} className="h-5 w-5 invert" />
        <h1 className="text-sm font-semibold tracking-tight text-neutral-50">daimon</h1>
        <span className={`${STATUS_DOT} ${dot}`} title={statusTitle} />
        <nav className="ml-3 flex items-center gap-1">
          <button
            onClick={() => onViewChange("chat")}
            className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
              view === "chat"
                ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
                : "text-neutral-400 hover:bg-white/5 hover:text-neutral-200"
            }`}
          >
            chat
          </button>
          <button
            onClick={() => onViewChange("terminal")}
            className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
              view === "terminal"
                ? "bg-[#4f8dff]/20 text-[#4f8dff] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.12)]"
                : "text-neutral-400 hover:bg-white/5 hover:text-neutral-200"
            }`}
          >
            terminal
          </button>
        </nav>
        <button
          onClick={onCollapse}
          title="Collapse to pill"
          className="ml-auto rounded-full px-2 py-1 text-xs text-neutral-400 transition-colors hover:bg-white/5 hover:text-neutral-200"
        >
          ⌄
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
                      isSessionBusy(list) ? "bg-[#4f8dff] animate-daimon-pulse" : "bg-neutral-600"
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
          <ChatInput onSend={onSend} disabled={busy} />
        </div>
      )}

      {view === "terminal" && (
        <div className="flex min-h-0 flex-1 flex-col">
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
                active={id === activeTerminalId}
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
      )}
    </div>
  );
}
