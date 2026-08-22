import { useCallback, useEffect, useRef, useState } from "react";
import { MessageList } from "../components/MessageList";
import { TodoList } from "../components/TodoList";
import { applyEvent, applyTodoEvent } from "../sessionEvents";
import type { AgentEvent, ChatMessage, TodoItem } from "../types";
import type { BusClient } from "../lib/busClient";
import { AskSheet, type AskPrompt } from "./AskSheet";
import { Notes } from "./Notes";
import { TerminalView } from "./TerminalView";
import {
  connect,
  fetchWorkspaces,
  forgetToken,
  type SessionInfo,
  type TerminalInfo,
  type WorkspaceInfo,
} from "./transport";

type Screen =
  | { view: "workspaces" }
  | { view: "list"; workspace: WorkspaceInfo }
  | { view: "session"; workspace: WorkspaceInfo; sessionId: string; title: string }
  | { view: "terminal"; workspace: WorkspaceInfo; id: string }
  | { view: "notes"; workspace: WorkspaceInfo };

export function Remote({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [screen, setScreen] = useState<Screen>({ view: "workspaces" });
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [ask, setAsk] = useState<AskPrompt | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const busRef = useRef<BusClient | null>(null);
  // The screen as the socket callbacks see it. They are registered once, so
  // reading React state directly would pin them to the first render's value.
  const screenRef = useRef(screen);
  screenRef.current = screen;
  const termSinks = useRef(new Map<string, (bytes: Uint8Array) => void>());

  const routeTermData = useCallback((id: string, sink: ((b: Uint8Array) => void) | null) => {
    if (sink) termSinks.current.set(id, sink);
    else termSinks.current.delete(id);
  }, []);

  // One socket for the whole app, opened once. Every view multiplexes over it.
  useEffect(() => {
    const bus = connect({
      onStatus: setConnected,
      onEvent: (session, _seq, event) => {
        const current = screenRef.current;
        if (current.view !== "session" || current.sessionId !== session) return;
        const agentEvent = event as AgentEvent;
        const type = (event as { type: string }).type;

        // `ask` and `ask_resolved` are session state, not message state — the
        // same split `applyTodoEvent` makes, and the same one cli/live.py
        // makes with LiveState.ask. A question is a thing the session is
        // waiting on, not a line in the transcript.
        if (type === "ask") {
          setAsk(event as AskPrompt);
          setBusy(false);
          return;
        }
        if (type === "ask_resolved") {
          // Somebody answered — possibly on the laptop. Take the prompt down
          // rather than letting it sit there answering an already-resumed turn.
          setAsk(null);
          setBusy(true);
          return;
        }
        if (type === "done" || type === "error") setBusy(false);

        setMessages((prev) => applyEvent(prev, agentEvent));
        setTodos((prev) => applyTodoEvent(prev, agentEvent));
      },
      onTermData: (id, bytes) => termSinks.current.get(id)?.(bytes),
      onControl: (frame) => {
        if (frame.control === "snapshot") {
          // A snapshot is the whole transcript as of now, not a continuation —
          // rebuild rather than append, or a reconnect duplicates everything.
          const events = (frame.events ?? []) as AgentEvent[];
          setMessages(events.reduce<ChatMessage[]>(foldSnapshot, []));
          setTodos((frame.todos ?? []) as TodoItem[]);
          // A session parked on a question renders the prompt immediately.
          // The event that carried it may have been hours ago; the channel
          // keeps it precisely so a client arriving late can still answer.
          setAsk((frame.pending_ask as AskPrompt | null) ?? null);
          setBusy(Boolean(frame.busy));
        } else if (frame.control === "term_snapshot") {
          termSinks.current.get(String(frame.id))?.(decodeSnapshot(frame.data));
        } else if (frame.control === "workspace_lost") {
          setError("that workspace's server stopped");
        }
      },
      onReconnect: () => {
        // Nothing we were attached to is attached any more.
        const current = screenRef.current;
        if (current.view === "session") {
          void bus.send("attach", { workspace: current.workspace.key, session: current.sessionId, wants_ask: true });
        }
      },
    });
    busRef.current = bus;
    void bus.connect().catch(() => setError("could not reach the gateway"));
    return () => bus.close();
  }, []);

  // A backgrounded tab holds a subscription the server counts as "someone is
  // watching", which keeps its idle shutdown from ever firing. Let go while
  // the phone is in a pocket; the reconnect path re-snapshots on return.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        timer = setTimeout(() => busRef.current?.close(), 120_000);
      } else {
        clearTimeout(timer);
        void busRef.current?.connect().catch(() => {});
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    fetchWorkspaces()
      .then(setWorkspaces)
      .catch((err) => (String(err.message) === "unauthorized" ? onUnauthorized() : setError(String(err.message))));
  }, [onUnauthorized]);

  async function openWorkspace(workspace: WorkspaceInfo) {
    setScreen({ view: "list", workspace });
    await refreshLists(workspace);
  }

  async function refreshLists(workspace: WorkspaceInfo) {
    const bus = busRef.current;
    if (!bus) return;
    const [sessionAck, termAck] = await Promise.all([
      bus.send("sessions", { workspace: workspace.key }),
      bus.send("term.list", { workspace: workspace.key }),
    ]);
    if (sessionAck.ok) setSessions((sessionAck.sessions ?? []) as SessionInfo[]);
    // A refusal here is expected and not an error: the gateway was started
    // without --terminals, so this device is not allowed shells.
    setTerminals(termAck.ok ? ((termAck.terminals ?? []) as TerminalInfo[]) : []);
  }

  async function openSession(workspace: WorkspaceInfo, session: SessionInfo) {
    setMessages([]);
    setTodos([]);
    setAsk(null);
    setBusy(session.busy);
    setScreen({
      view: "session",
      workspace,
      sessionId: session.session_id,
      title: session.title || session.session_id.slice(0, 8),
    });
    await busRef.current?.send("attach", {
      workspace: workspace.key,
      session: session.session_id,
      wants_ask: true,
    });
  }

  async function leaveSession(workspace: WorkspaceInfo, sessionId: string) {
    await busRef.current?.send("detach", { workspace: workspace.key, session: sessionId });
    setScreen({ view: "list", workspace });
    await refreshLists(workspace);
  }

  async function newTerminal(workspace: WorkspaceInfo) {
    const ack = await busRef.current?.send("term.open", { workspace: workspace.key, cols: 80, rows: 24 });
    if (!ack?.ok) {
      setError(ack?.error ?? "could not open a terminal");
      return;
    }
    setScreen({ view: "terminal", workspace, id: (ack.terminal as TerminalInfo).id });
  }

  return (
    <div className="flex min-h-[100dvh] flex-col bg-neutral-950 text-neutral-100">
      <Header screen={screen} connected={connected} onBack={setScreen} onLeave={leaveSession} />

      {error && (
        <button
          onClick={() => setError(null)}
          className="mx-3 mt-2 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-left text-xs text-red-300"
        >
          {error} — tap to dismiss
        </button>
      )}

      {screen.view === "workspaces" && (
        <WorkspaceList workspaces={workspaces} onOpen={openWorkspace} onSignOut={() => {
          forgetToken();
          onUnauthorized();
        }} />
      )}

      {screen.view === "list" && (
        <SessionAndTerminalList
          sessions={sessions}
          terminals={terminals}
          onSession={(s) => openSession(screen.workspace, s)}
          onTerminal={(t) => setScreen({ view: "terminal", workspace: screen.workspace, id: t.id })}
          onNewTerminal={() => newTerminal(screen.workspace)}
          onRefresh={() => refreshLists(screen.workspace)}
          onNotes={() => setScreen({ view: "notes", workspace: screen.workspace })}
        />
      )}

      {screen.view === "session" && (
        <SessionView
          messages={messages}
          todos={todos}
          busy={busy}
          onStop={() => {
            void busRef.current?.send("interrupt", {
              workspace: screen.workspace.key,
              session: screen.sessionId,
              by: "phone",
            });
          }}
          onSend={(text) => {
            const bus = busRef.current;
            if (!bus) return;
            setMessages((prev) => [
              ...prev,
              { id: crypto.randomUUID(), role: "user", content: text, steps: [], thinking: false },
              { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: true, startedAt: Date.now() },
            ]);
            setBusy(true);
            void bus.send("prompt", {
              workspace: screen.workspace.key,
              session: screen.sessionId,
              instruction: text,
            });
          }}
        />
      )}

      {screen.view === "session" && ask && (
        <AskSheet
          ask={ask}
          onAnswer={(answer) => {
            setAsk(null);
            setBusy(true);
            void busRef.current?.send("answer", {
              workspace: screen.workspace.key,
              session: screen.sessionId,
              ask_id: ask.id,
              answer,
              by: "phone",
            });
          }}
          onDismiss={() => setAsk(null)}
        />
      )}

      {screen.view === "notes" && busRef.current && (
        <Notes bus={busRef.current} workspace={screen.workspace.key} />
      )}

      {screen.view === "terminal" && busRef.current && (
        <TerminalView
          bus={busRef.current}
          workspace={screen.workspace.key}
          id={screen.id}
          onData={routeTermData}
        />
      )}
    </div>
  );
}

// --- pieces -----------------------------------------------------------------

function Header({
  screen,
  connected,
  onBack,
  onLeave,
}: {
  screen: Screen;
  connected: boolean;
  onBack: (s: Screen) => void;
  onLeave: (w: WorkspaceInfo, sessionId: string) => void;
}) {
  const title =
    screen.view === "workspaces" ? "Daimon"
      : screen.view === "list" ? screen.workspace.name
        : screen.view === "session" ? screen.title
          : screen.view === "notes" ? "notes"
            : "terminal";

  return (
    <header
      className="sticky top-0 z-10 flex items-center gap-3 border-b border-white/10 bg-neutral-950/90 px-3 py-3 backdrop-blur-xl"
      style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}
    >
      {screen.view !== "workspaces" && (
        <button
          onClick={() => {
            if (screen.view === "session") onLeave(screen.workspace, screen.sessionId);
            else if (screen.view === "terminal" || screen.view === "notes")
              onBack({ view: "list", workspace: screen.workspace });
            else onBack({ view: "workspaces" });
          }}
          className="rounded-lg px-2 py-1 text-sm text-neutral-400 active:text-neutral-100"
        >
          ‹ back
        </button>
      )}
      <h1 className="min-w-0 flex-1 truncate text-sm font-medium">{title}</h1>
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${connected ? "bg-emerald-400" : "bg-neutral-600"}`}
        title={connected ? "connected" : "reconnecting"}
      />
    </header>
  );
}

function WorkspaceList({
  workspaces,
  onOpen,
  onSignOut,
}: {
  workspaces: WorkspaceInfo[];
  onOpen: (w: WorkspaceInfo) => void;
  onSignOut: () => void;
}) {
  return (
    <div className="flex-1 px-3 py-3">
      {workspaces.length === 0 && (
        <p className="px-1 py-8 text-center text-sm text-neutral-500">
          No agent servers are running on that machine.
        </p>
      )}
      {workspaces.map((workspace) => (
        <button
          key={workspace.key}
          onClick={() => onOpen(workspace)}
          className="mb-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3.5 text-left active:bg-white/10"
        >
          <div className="font-medium">{workspace.name}</div>
          <div className="truncate text-xs text-neutral-500">{workspace.path}</div>
        </button>
      ))}
      <button onClick={onSignOut} className="mt-6 w-full py-3 text-xs text-neutral-600">
        forget this device
      </button>
    </div>
  );
}

function SessionAndTerminalList({
  sessions,
  terminals,
  onSession,
  onTerminal,
  onNewTerminal,
  onRefresh,
  onNotes,
}: {
  sessions: SessionInfo[];
  terminals: TerminalInfo[];
  onSession: (s: SessionInfo) => void;
  onTerminal: (t: TerminalInfo) => void;
  onNewTerminal: () => void;
  onRefresh: () => void;
  onNotes: () => void;
}) {
  return (
    <div className="flex-1 overflow-y-auto px-3 py-3">
      <button
        onClick={onNotes}
        className="mb-4 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-left text-sm active:bg-white/10"
      >
        notes ›
      </button>
      <div className="mb-2 flex items-center justify-between px-1">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500">terminals</h2>
        <button onClick={onNewTerminal} className="text-xs text-[#4f8dff]">+ new</button>
      </div>
      {terminals.length === 0 && (
        <p className="px-1 pb-3 text-xs text-neutral-600">none running</p>
      )}
      {terminals.map((terminal) => (
        <button
          key={terminal.id}
          onClick={() => onTerminal(terminal)}
          className="mb-2 flex w-full items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-left active:bg-white/10"
        >
          <span className={`h-2 w-2 rounded-full ${terminal.exited ? "bg-neutral-600" : "bg-emerald-400"}`} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm">{terminal.cwd}</span>
            <span className="text-xs text-neutral-500">
              {terminal.exited ? "exited" : `pid ${terminal.pid}`}
              {terminal.clients > 0 && ` · ${terminal.clients} watching`}
            </span>
          </span>
        </button>
      ))}

      <div className="mb-2 mt-6 flex items-center justify-between px-1">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500">sessions</h2>
        <button onClick={onRefresh} className="text-xs text-neutral-500">refresh</button>
      </div>
      {sessions.length === 0 && <p className="px-1 text-xs text-neutral-600">no conversations yet</p>}
      {sessions.map((session) => (
        <button
          key={session.session_id}
          onClick={() => onSession(session)}
          className="mb-2 w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-left active:bg-white/10"
        >
          <div className="flex items-center gap-2">
            {session.busy && <span className="h-2 w-2 animate-pulse rounded-full bg-[#4f8dff]" />}
            <span className="min-w-0 flex-1 truncate text-sm">
              {session.title || session.session_id.slice(0, 8)}
            </span>
            {session.pending_ask && (
              <span className="shrink-0 rounded-full bg-amber-400/15 px-2 py-0.5 text-[10px] text-amber-300">
                waiting on you
              </span>
            )}
          </div>
          {session.last_result && (
            <div className="mt-1 line-clamp-2 text-xs text-neutral-500">{session.last_result}</div>
          )}
        </button>
      ))}
    </div>
  );
}

function SessionView({
  messages,
  todos,
  busy,
  onSend,
  onStop,
}: {
  messages: ChatMessage[];
  todos: TodoItem[];
  busy: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [draft, setDraft] = useState("");
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <MessageList messages={messages} />
      </div>
      {todos.length > 0 && (
        <div className="border-t border-white/10 px-3 py-2">
          <TodoList items={todos} />
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const text = draft.trim();
          if (!text) return;
          setDraft("");
          onSend(text);
        }}
        className="flex gap-2 border-t border-white/10 px-3 py-2"
        style={{ paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="message"
          className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-white/5 px-4 py-2.5 text-sm outline-none focus:border-[#4f8dff]/60"
        />
        {busy ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-2xl border border-white/15 bg-white/5 px-4 py-2.5 text-sm font-medium text-neutral-200"
          >
            stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!draft.trim()}
            className="rounded-2xl bg-[#4f8dff] px-4 py-2.5 text-sm font-medium text-white disabled:opacity-40"
          >
            send
          </button>
        )}
      </form>
    </div>
  );
}

// --- helpers ----------------------------------------------------------------

/** Replay a recorded event into a transcript. The user's own prompts are in
 *  the stream now (that is what `user_event` is for), so a session someone
 *  else started reads as a conversation rather than a monologue. */
function foldSnapshot(messages: ChatMessage[], event: AgentEvent): ChatMessage[] {
  if ((event as { type: string }).type === "user") {
    const text = String((event as unknown as { text: string }).text ?? "");
    return [
      ...messages,
      { id: crypto.randomUUID(), role: "user", content: text, steps: [], thinking: false },
      { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: false },
    ];
  }
  return applyEvent(messages, event);
}

function decodeSnapshot(data: unknown): Uint8Array {
  const binary = atob(String(data ?? ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
