import { useCallback, useEffect, useRef, useState } from "react";
import {
  agentStatus,
  closeTerminal,
  onDictationStatus,
  onVoiceModelDownload,
  sendMessage,
  setWindowVibrancy,
  startChat,
  voiceModelStatus,
} from "./api";
import { applyEvent, isSessionBusy, onSessionStatus } from "./sessionEvents";
import { collapseToPill, expandToPanel } from "./lib/window";
import type {
  AgentStatus,
  ChatMessage,
  DictationStatus,
  SessionStatusPayload,
  View,
  VoiceModelDownloadPayload,
  VoiceModelStatus,
} from "./types";
import { insertText } from "./lib/voice";
import { Panel } from "./components/Panel";
import { Pill } from "./components/Pill";

export default function App() {
  // Chat sessions are a map keyed by session uuid plus a stable order and
  // an active id — the single `messages` array + sessionRef is gone. Each
  // session streams independently (the server serializes turns per session,
  // not globally), so switching tabs mid-turn is the point, not an edge.
  const [sessions, setSessions] = useState<Record<string, ChatMessage[]>>({});
  const [sessionOrder, setSessionOrder] = useState<string[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  // Whether the panel (vs. the pill) is showing. The pill itself is always
  // the real window — see collapseToPill/expandToPanel in lib/window.ts.
  const [expanded, setExpanded] = useState(false);
  // Which panel view is active — lives here rather than as a Panel-local
  // useState for the same reason `terminalTabs` etc. do below: the Panel
  // fully unmounts on collapse-to-pill, so a plain local `useState<View>`
  // would reset back to "chat" on every single collapse/expand, always
  // dumping the user back on chat instead of wherever they'd left off.
  const [view, setView] = useState<View>("chat");

  // Terminal tab state lives here, not inside the Panel, so it survives a
  // collapse-to-pill cycle — the Panel fully unmounts whenever `expanded`
  // goes false, and state living inside an unmounted component is gone
  // (the real shell processes behind the tabs survive; the frontend must
  // remember the tab ids to keep talking to them).
  const [terminalTabs, setTerminalTabs] = useState<string[]>([]);
  const [activeTerminalId, setActiveTerminalId] = useState<string | null>(null);
  // Keyed by terminal tab id: one-shot "type this in once the shell has
  // actually started" commands for tabs the *agent* opened (via a `ui_action`
  // event on the session-status stream — see handleEvent below).
  const [initialTerminalCommands, setInitialTerminalCommands] = useState<Record<string, string>>({});

  // Voice dictation state — driven entirely by Rust-side events (Fn key
  // gesture monitor + cpal recording thread + whisper-rs transcription).
  const [dictation, setDictation] = useState<DictationStatus>({ type: "idle" });
  const [voiceModel, setVoiceModel] = useState<VoiceModelStatus | null>(null);
  const [voiceModelDownload, setVoiceModelDownload] = useState<VoiceModelDownloadPayload | null>(null);

  const refreshVoiceModel = useCallback(() => {
    voiceModelStatus().then(setVoiceModel).catch(() => {});
  }, []);

  // The event listener must read the session map without re-subscribing on
  // every streamed chunk (the listener registers once against a stable
  // handleEvent — otherwise every chunk would tear down and re-register the
  // Tauri listener), so a ref mirrors the state. commitSessions is the only
  // writer, keeping the two in lockstep.
  const sessionsRef = useRef<Record<string, ChatMessage[]>>({});
  const expandedRef = useRef(false);
  const commitSessions = useCallback(
    (updater: (prev: Record<string, ChatMessage[]>) => Record<string, ChatMessage[]>) => {
      setSessions((prev) => {
        const next = updater(prev);
        sessionsRef.current = next;
        return next;
      });
    },
    [],
  );

  // Derived per-session state. `busy` guards the active session's input only
  // — per-session concurrency is the point — while the pill pulses when any
  // session is working OR the server reports global busy (CLI turns, queued
  // turns, etc.). The local check gives instant reactivity between polls.
  const messages = activeSessionId ? (sessions[activeSessionId] ?? []) : [];
  const busy = isSessionBusy(messages);
  const anyBusy = (status?.busy ?? false) || sessionOrder.some((sid) => isSessionBusy(sessions[sid] ?? []));
  const hasError = sessionOrder.some((sid) => (sessions[sid] ?? []).some((m) => m.error));
  // Green "success" dot: a turn finished while the panel was collapsed.
  // Cleared when the user expands — they've seen the result.
  const [unreadCompletion, setUnreadCompletion] = useState(false);
  // External (CLI) sessions the app doesn't own — surfaced as read-only chips
  // in the panel so the user sees CLI activity in the dashboard.
  const cliSessions = (status?.sessions ?? []).filter((sid) => !(sid in sessionsRef.current));

  const refreshStatus = useCallback(() => {
    agentStatus()
      .then(setStatus)
      .catch(() => setStatus({ running: false, adopted: false, port: 4711, busy: false, active_turns: 0, sessions: [] }));
  }, []);

  const expand = useCallback(() => {
    setExpanded(true);
    expandedRef.current = true;
    // Don't clear unreadCompletion — keep green while user reads.
    void expandToPanel();
  }, []);

  const collapse = useCallback(() => {
    setExpanded(false);
    expandedRef.current = false;
    // If the user didn't send a new message while reading, the green
    // has served its purpose — back to idle grey.
    setUnreadCompletion(false);
    void collapseToPill();
  }, []);

  // The "+" chip on the terminal view — opens a fresh tab with a new shell
  // process. (Agent-triggered tabs instead come from the ui_action handler
  // in handleEvent, which also stages an initial command.)
  const openNewTerminalTab = useCallback(() => {
    const id = crypto.randomUUID();
    setTerminalTabs((tabs) => [...tabs, id]);
    setActiveTerminalId(id);
  }, []);

  // Closing a tab both ends its real shell process (`closeTerminal`, fired
  // and forgotten — nothing here needs to wait for the kill to land before
  // dropping it from the list) and picks a new active tab if the closed one
  // was it, falling back to a neighbor, or to nothing.
  const closeTerminalTab = useCallback((id: string) => {
    void closeTerminal(id);
    setTerminalTabs((tabs) => {
      const remaining = tabs.filter((tabId) => tabId !== id);
      // Always keep at least one terminal tab — if the user closes the last
      // one, mint a fresh default so the terminal view is never empty.
      if (remaining.length === 0) {
        const fresh = crypto.randomUUID();
        setActiveTerminalId(fresh);
        return [fresh];
      }
      setActiveTerminalId((current) => (current !== id ? current : remaining[remaining.length - 1] ?? null));
      return remaining;
    });
    // Hygiene, not strictly required for correctness — `initialCommand` is
    // only ever consumed once, in TerminalPanel's mount effect, keyed by a
    // dependency array that never re-fires it — but this keeps the map from
    // growing unboundedly across many opened/closed agent-triggered tabs.
    setInitialTerminalCommands((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  // Closing a chat tab is a UI decision only: the server keeps running the
  // turn, and its result lands in the checkpointer thread (`daimon -n <uuid>`
  // could pick it up). There is no kill-the-turn endpoint, by design.
  const closeChat = useCallback(
    (id: string) => {
      commitSessions((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setSessionOrder((order) => {
        const remaining = order.filter((sid) => sid !== id);
        setActiveSessionId((current) => (current !== id ? current : remaining[remaining.length - 1] ?? null));
        return remaining;
      });
    },
    [commitSessions],
  );

  const handleEvent = useCallback(
    (payload: SessionStatusPayload) => {
      const event = payload.event;
      if (event.type === "ui_action" && event.action === "open_terminal_with_command") {
        // The agent staged a shell command — open a fresh terminal tab with it
        // *typed but not executed* (the user presses Enter to run it). The
        // panel may currently be collapsed — agent-triggered instructions run
        // regardless of pill/panel state. Handled before any session routing:
        // a command the agent staged opens the terminal even if the chat that
        // asked for it was closed mid-turn (matches legacy).
        const id = crypto.randomUUID();
        setTerminalTabs((tabs) => [...tabs, id]);
        setActiveTerminalId(id);
        setInitialTerminalCommands((prev) => ({ ...prev, [id]: event.command ?? "" }));
        setView("terminal");
        expand();
        return; // never turn/chat content — nothing to fold into a session
      }
      const sid = payload.session_id;
      // Unknown/closed session ids are dropped — the turn still runs server-
      // side, this UI just isn't showing it anymore.
      if (!(sid in sessionsRef.current)) return;
      commitSessions((prev) => ({ ...prev, [sid]: applyEvent(prev[sid], event) }));
      if (event.type === "done" || event.type === "error") {
        refreshStatus();
        // If the panel is collapsed, light the green "success" dot so the user
        // knows a result is waiting — cleared when they expand to read it.
        if (!expandedRef.current) setUnreadCompletion(true);
      }
    },
    [commitSessions, refreshStatus, expand],
  );

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    onSessionStatus(handleEvent).then((u) => {
      if (cancelled) u();
      else unlisten = u;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [handleEvent]);

  // Dictation event subscription — driven by the Fn-key gesture monitor and
  // the cpal recording thread (both Rust-side). Routes transcribed text to the
  // focused input and auto-expands the panel when recording starts so the
  // VoiceIndicator is visible.
  useEffect(() => {
    let unlistenDictation: (() => void) | undefined;
    let unlistenDownload: (() => void) | undefined;
    let cancelled = false;

    onDictationStatus((status) => {
      setDictation(status);
      if (status.type === "recording" || status.type === "transcribing") {
        expand();
      }
      if (status.type === "result") {
        insertText(status.text);
      }
    }).then((u) => {
      if (cancelled) u();
      else unlistenDictation = u;
    });

    onVoiceModelDownload((payload) => {
      setVoiceModelDownload(payload);
      if (payload.done) refreshVoiceModel();
    }).then((u) => {
      if (cancelled) u();
      else unlistenDownload = u;
    });

    return () => {
      cancelled = true;
      unlistenDictation?.();
      unlistenDownload?.();
    };
  }, [expand, refreshVoiceModel]);

  // On mount: collapse to the pill, install the native vibrancy material
  // behind the window (28px = a circle at the pill size and the panel's
  // rounded corner when expanded, so one value covers both states), report
  // agent status, pre-create the first chat session and default terminal tab.
  // Poll /status every 1.5s so CLI-initiated turns light the pill — the
  // localhost GET is cheap and the pill dot reactivity matters.
  useEffect(() => {
    void collapseToPill();
    void setWindowVibrancy(28);
    refreshStatus();
    refreshVoiceModel();
    const poll = setInterval(refreshStatus, 1500);
    startChat().then((sid) => {
      commitSessions((prev) => ({ ...prev, [sid]: [] }));
      setSessionOrder((order) => [...order, sid]);
      setActiveSessionId((current) => current ?? sid);
    }).catch(() => {});
    openNewTerminalTab();
    return () => clearInterval(poll);
  }, [refreshStatus, commitSessions, openNewTerminalTab]);

  const newChat = async () => {
    try {
      const sid = await startChat();
      commitSessions((prev) => ({ ...prev, [sid]: [] }));
      setSessionOrder((order) => [...order, sid]);
      setActiveSessionId(sid);
    } catch {
      // keep the current session; the status dot shows the agent is down
    }
  };

  const send = async (text: string) => {
    const sid = activeSessionId;
    if (!sid || busy || !text.trim()) return;
    setUnreadCompletion(false); // new turn starting → busy blue takes over
    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: "user", content: text, steps: [], thinking: false };
    const asstMsg: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: true };
    commitSessions((prev) => ({ ...prev, [sid]: [...(prev[sid] ?? []), userMsg, asstMsg] }));
    try {
      await sendMessage(sid, text);
    } catch (err) {
      // Invoke failed before the stream could start (agent down, etc.).
      commitSessions((prev) => {
        const list = prev[sid] ?? [];
        const last = list[list.length - 1];
        if (last?.role === "assistant") {
          return { ...prev, [sid]: [...list.slice(0, -1), { ...last, thinking: false, error: String(err) }] };
        }
        return prev;
      });
    }
  };

  return (
    <main className="h-full w-full bg-transparent p-0">
      {expanded ? (
        <Panel
          busy={busy}
          globalBusy={anyBusy}
          unreadCompletion={unreadCompletion}
          status={status}
          view={view}
          onViewChange={setView}
          onCollapse={collapse}
          sessions={sessions}
          sessionOrder={sessionOrder}
          activeSessionId={activeSessionId}
          onSelectChat={setActiveSessionId}
          onCloseChat={closeChat}
          onAddChat={newChat}
          messages={messages}
          onSend={send}
          terminalTabs={terminalTabs}
          activeTerminalId={activeTerminalId}
          initialTerminalCommands={initialTerminalCommands}
          onActivateTerminal={setActiveTerminalId}
          onCloseTerminal={closeTerminalTab}
          onAddTerminal={openNewTerminalTab}
          cliSessions={cliSessions}
          dictation={dictation}
          voiceModel={voiceModel}
          voiceModelDownload={voiceModelDownload}
          onRefreshVoiceModel={refreshVoiceModel}
        />
      ) : (
        <Pill
          busy={anyBusy}
          agentDown={!status?.running}
          hasError={hasError}
          unreadCompletion={unreadCompletion}
          onExpand={expand}
          dictation={dictation}
        />
      )}
    </main>
  );
}
