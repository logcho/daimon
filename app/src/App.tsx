import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentConfig } from "./api";
import {
  agentStatus,
  fetchConfig,
  onDictationStatus,
  onUiCommand,
  onVoiceModelDownload,
  openExternal,
  setWindowVibrancy,
  startChat,
  voiceModelStatus,
} from "./api";
import { applyEvent, applyTodoEvent, isSessionBusy } from "./sessionEvents";
import { bus, onBusControl, onBusReconnect, onSessionEvent } from "./lib/bus";
import { collapseToPill, expandToPanel } from "./lib/window";
import type { AskPrompt } from "./components/AskPrompt";
import type {
  AgentEvent,
  AgentStatus,
  ChatMessage,
  DictationStatus,
  TodoItem,
  View,
  VoiceModelDownloadPayload,
  VoiceModelStatus,
} from "./types";
import { insertText } from "./lib/voice";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { useLinkInterception } from "./hooks/useLinkInterception";
import { LinkViewer } from "./components/LinkViewer";
import { Panel } from "./components/Panel";
import { Pill } from "./components/Pill";

//: How many existing conversations to reopen on launch. The tab strip is a
//: working set, not an archive — the rest stay in the session directory and
//: are reachable from another device.
const MAX_RESTORED_CHATS = 5;

export default function App() {
  // Chat sessions are a map keyed by session uuid plus a stable order and
  // an active id — the single `messages` array + sessionRef is gone. Each
  // session streams independently (the server serializes turns per session,
  // not globally), so switching tabs mid-turn is the point, not an edge.
  const [sessions, setSessions] = useState<Record<string, ChatMessage[]>>({});
  // Which sessions this window has told the server it is watching. A ref, not
  // state: attaching is a side effect that must happen once per session, and
  // re-rendering on it would loop.
  const attachedRef = useRef<Set<string>>(new Set());
  // Read by answerAsk, which is registered once and would otherwise close over
  // the first render's value.
  const sessionAsksRef = useRef<Record<string, AskPrompt | null>>({});
  // Todos are session state, not message state — they describe what the agent
  // is doing *now*. Attached to a message they vanished the moment the next
  // turn started, which is exactly when a checklist earns its keep. Kept as
  // its own map rather than folded into `sessions` so `applyEvent` stays a
  // pure function over messages.
  const [sessionTodos, setSessionTodos] = useState<Record<string, TodoItem[]>>({});
  // The question a session is parked on, if any. Session state rather than
  // message state — the same split todos get, and the same one cli/live.py
  // makes: a question is something the session is waiting on, not a line in
  // the transcript.
  const [sessionAsks, setSessionAsks] = useState<Record<string, AskPrompt | null>>({});
  const [sessionOrder, setSessionOrder] = useState<string[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  // The status bar's model and context window. Same GET /config the
  // settings tab and the CLI read, so the three can't disagree.
  const [config, setConfig] = useState<AgentConfig | null>(null);
  // Whether the panel (vs. the pill) is showing. The pill itself is always
  // the real window — see collapseToPill/expandToPanel in lib/window.ts.
  const [expanded, setExpanded] = useState(false);
  // Whether the panel has ever been opened. The panel is mounted from that
  // point on and merely hidden while collapsed (see the render below), so a
  // collapse no longer destroys the terminals inside it.
  const [everExpanded, setEverExpanded] = useState(false);
  // The page the in-app link viewer is showing, if any. Lives at the top so
  // one viewer serves every surface that renders markdown — a note, a chat
  // message, a skill — and so Escape can reach it (see `dismissTop` below).
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  // Which panel view is active — lives here rather than as a Panel-local
  // useState so that it survives, and because the panel's callers set it
  // (a staged terminal command switches to the terminal view).
  const [view, setView] = useState<View>("chat");

  // Terminal tab state lives here, not inside the Panel: the frontend has to
  // remember the tab ids to keep talking to the real shell processes behind
  // them, which live in Rust and outlive any React tree.
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
  sessionAsksRef.current = sessionAsks;
  const messages = activeSessionId ? (sessions[activeSessionId] ?? []) : [];
  const todos = activeSessionId ? (sessionTodos[activeSessionId] ?? []) : [];
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
      .catch(() => setStatus({ running: false, port: 4711, busy: false, active_turns: 0, sessions: [] }));
  }, []);

  const expand = useCallback(() => {
    setExpanded(true);
    setEverExpanded(true);
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

  /** Open a tab for every shell the server is running.
   *
   *  Terminals live in the agent server — that is what lets one outlive this
   *  window and be shared with a phone — but the tab strip was still purely
   *  local, minted here and never compared against what actually exists. So a
   *  shell opened on a phone was running, watchable, and invisible: there was
   *  no tab to click. Called on launch and after a reconnect, when what we
   *  believe about the server is stale by definition.
   *
   *  Additive: it never closes a tab. A tab whose shell has exited is a thing
   *  the user can see the exit code of and restart, and yanking it out from
   *  under them would be worse than leaving it. */
  const restoreTerminals = useCallback(async () => {
    const ack = await bus().send("term.list");
    if (!ack.ok) return;
    const live = (ack.terminals ?? []) as { id: string; exited: boolean }[];
    const ids = live.filter((t) => !t.exited).map((t) => t.id);
    if (ids.length === 0) return;
    setTerminalTabs((tabs) => [...tabs, ...ids.filter((id) => !tabs.includes(id))]);
    setActiveTerminalId((current) => current ?? ids[0]);
  }, []);

  // Closing a tab both ends its real shell process (fired and forgotten —
  // nothing here needs to wait for the kill to land before dropping it from
  // the list) and picks a new active tab if the closed one was it, falling
  // back to a neighbor, or to nothing.
  //
  // Note this is the one path that genuinely ends a shell. The panel merely
  // *detaching* on unmount is not the same thing any more: terminals live in
  // the agent server, so a tab you close is gone on purpose while one that
  // merely scrolled out of view keeps running.
  const closeTerminalTab = useCallback((id: string) => {
    bus().post("term.close", { id });
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

  /** Start a chat session and make it active. The one place that creates one,
   *  so "there is always a session" holds however you got here — first launch,
   *  the + button, or closing the last tab. Returns false when the agent isn't
   *  reachable, so the caller can retry. */
  const ensureSession = useCallback(async (): Promise<boolean> => {
    try {
      const sid = await startChat();
      commitSessions((prev) => ({ ...prev, [sid]: [] }));
      setSessionOrder((order) => [...order, sid]);
      setActiveSessionId((current) => current ?? sid);
      return true;
    } catch {
      return false; // agent down — the status dot already says so
    }
  }, [commitSessions]);

  /** Open the conversations that already exist, rather than a fresh empty one.
   *
   *  Sessions live in the agent server, not in this window: one started on a
   *  phone is just as real as one started here. Minting a new id on every
   *  launch meant the app opened onto an empty chat nothing else knew about,
   *  and a conversation you had begun elsewhere was invisible unless you
   *  happened to guess its id. Falls back to creating one when there are
   *  genuinely none. */
  const restoreSessions = useCallback(async (): Promise<boolean> => {
    const ack = await bus().send("sessions");
    if (!ack.ok) return ensureSession();
    const rows = (ack.sessions ?? []) as { session_id: string; last_active_at?: number }[];
    // Most recently active first, and only a handful: the tab strip is a
    // working set, not an archive.
    const recent = rows.slice(0, MAX_RESTORED_CHATS).map((r) => r.session_id);
    if (recent.length === 0) return ensureSession();
    commitSessions((prev) => ({
      ...prev,
      ...Object.fromEntries(recent.filter((id) => !(id in prev)).map((id) => [id, []])),
    }));
    setSessionOrder((order) => [...order, ...recent.filter((id) => !order.includes(id))]);
    setActiveSessionId((current) => current ?? recent[0]);
    return true;
  }, [commitSessions, ensureSession]);

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
      setSessionTodos((prev) => {
        if (!(id in prev)) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setSessionOrder((order) => {
        const remaining = order.filter((sid) => sid !== id);
        setActiveSessionId((current) => (current !== id ? current : remaining[remaining.length - 1] ?? null));
        // Closing the last chat used to leave none at all — an empty view with
        // nothing to type into. There is always a session to talk to.
        if (remaining.length === 0) void ensureSession();
        return remaining;
      });
      // Never leave the chat view tab-less — the same guarantee
      // closeTerminalTab makes above. Without it, closing the last chat leaves
      // activeSessionId null and an input nothing can be typed into, which
      // Cmd+W turns from a deliberate click into a one-keystroke accident.
      // Deliberately outside the updater: `startChat` hits the server and is
      // not idempotent, and React is free to re-run a state updater for a
      // render it later discards.
      if (sessionOrder.length === 1 && sessionOrder[0] === id) void newChat();
    },
    [commitSessions, ensureSession],
  );

  /** Forget a conversation everywhere, rather than just closing its tab.
   *
   *  The tab goes on the announcement the server sends back, not here: that is
   *  the same path a deletion from a phone takes, so there is one behaviour
   *  rather than a local shortcut that happens to look the same. */
  const forgetChat = useCallback(
    async (id: string) => {
      const ack = await bus().send("session.delete", { session: id });
      // Refused while a turn is running — deleting underneath one would wipe
      // the history and have the turn write it straight back.
      if (!ack.ok) closeChat(id);
    },
    [closeChat],
  );

  const answerAsk = useCallback(async (sid: string, answer: string | string[]) => {
    const ask = sessionAsksRef.current[sid];
    setSessionAsks((prev) => ({ ...prev, [sid]: null }));
    await bus().send("answer", { session: sid, ask_id: ask?.id, answer, by: "desktop" });
  }, []);

  const newChat = async () => {
    const before = sessionOrder.length;
    if (await ensureSession()) {
      // ensureSession only *defaults* the active session; an explicit new chat
      // should switch to it.
      setSessionOrder((order) => {
        if (order.length > before) setActiveSessionId(order[order.length - 1]);
        return order;
      });
    }
  };

  // Cmd+T / Cmd+1-9 / Cmd+W / Cmd+Shift+[ ] over whichever tab strip the
  // current view is showing. Everything it drives already exists above; the
  // hook only reads state and calls these handlers.
  useKeyboardShortcuts({
    expanded,
    expand,
    collapse,
    view,
    setView,
    sessionOrder,
    activeSessionId,
    selectChat: setActiveSessionId,
    newChat,
    closeChat,
    terminalTabs,
    activeTerminalId,
    selectTerminal: setActiveTerminalId,
    newTerminal: openNewTerminalTab,
    closeTerminal: closeTerminalTab,
    dismissTop: () => {
      if (!linkUrl) return false;
      setLinkUrl(null);
      return true;
    },
  });

  // No `<a>` in the app is allowed to navigate the app away — it used to
  // replace the whole UI with a page that had no way back (#21).
  useLinkInterception(setLinkUrl, (url) => {
    // Fire-and-forget: `open` either launches or it doesn't, and there is no
    // second thing to try. The viewer is not involved — `mailto:` has nothing
    // to frame.
    void openExternal(url).catch(() => {});
  });

  const handleEvent = useCallback(
    (sid: string, event: AgentEvent) => {
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
      // Unknown/closed session ids are dropped — the turn still runs server-
      // side, this UI just isn't showing it anymore.
      if (!(sid in sessionsRef.current)) return;

      // `ask` and `ask_resolved` are session state, not message state.
      if (event.type === "ask") {
        setSessionAsks((prev) => ({ ...prev, [sid]: event as unknown as AskPrompt }));
        if (!expandedRef.current) setUnreadCompletion(true);
        return;
      }
      if (event.type === "ask_resolved") {
        // Answered — possibly on the phone. Take the prompt down rather than
        // leaving it answering a turn that already resumed.
        setSessionAsks((prev) => ({ ...prev, [sid]: null }));
        return;
      }
      commitSessions((prev) => ({ ...prev, [sid]: applyEvent(prev[sid], event) }));
      if (event.type === "todo") {
        setSessionTodos((prev) => ({ ...prev, [sid]: applyTodoEvent(prev[sid] ?? [], event) }));
      }
      if (event.type === "done" || event.type === "error") {
        refreshStatus();
        // If the panel is collapsed, light the green "success" dot so the user
        // knows a result is waiting — cleared when they expand to read it.
        if (!expandedRef.current) setUnreadCompletion(true);
      }
    },
    [commitSessions, refreshStatus, expand],
  );

  // Chat rides the bus, like terminals already do.
  //
  // It used to arrive down the NDJSON body of the request that started it,
  // which meant this window saw only the turns it had started itself. On the
  // bus a turn belongs to the session, so a message sent from a phone streams
  // in here as if it had been typed at this keyboard — and a session's history
  // survives a reload, because attaching replays it.
  useEffect(() => {
    const unlisten = onSessionEvent((session, _seq, event) =>
      handleEvent(session, event as AgentEvent),
    );
    return unlisten;
  }, [handleEvent]);

  // Attach to whichever sessions this window is showing, and take the
  // transcript the snapshot brings with it.
  useEffect(() => {
    const unlistenControl = onBusControl((frame) => {
      if (frame.control === "sessions_changed" && frame.change === "removed") {
        // Forgotten, here or elsewhere. Drop the tab: leaving one pointed at a
        // conversation the server no longer has is worse than it vanishing.
        const sid = String(frame.session ?? "");
        if (sid in sessionsRef.current) closeChat(sid);
        return;
      }
      if (frame.control === "sessions_changed") {
        // Adopt exactly the session that was announced, rather than re-reading
        // the directory: closing a chat tab is a UI decision that leaves the
        // conversation on the server, so a full restore would resurrect every
        // tab the user had deliberately closed.
        const sid = String(frame.session ?? "");
        if (!sid || sid in sessionsRef.current) return;
        commitSessions((prev) => ({ ...prev, [sid]: [] }));
        setSessionOrder((order) => (order.includes(sid) ? order : [...order, sid]));
        return;
      }
      if (frame.control === "terminals_changed") {
        // A shell opened or closed somewhere else. Re-read the list rather
        // than trusting the delta: it is one round trip and it converges even
        // if we missed a frame.
        void restoreTerminals();
        return;
      }
      if (frame.control !== "snapshot") return;
      const sid = String(frame.session ?? "");
      if (!(sid in sessionsRef.current)) return;
      // A snapshot is the whole transcript as of now, not a continuation:
      // rebuild rather than append, or reattaching duplicates everything.
      const events = (frame.events ?? []) as AgentEvent[];
      commitSessions((prev) => ({ ...prev, [sid]: events.reduce(applyEvent, [] as ChatMessage[]) }));
      setSessionTodos((prev) => ({ ...prev, [sid]: (frame.todos ?? []) as TodoItem[] }));
      // A session parked on a question renders it immediately: the event that
      // carried it may have been hours ago.
      setSessionAsks((prev) => ({ ...prev, [sid]: (frame.pending_ask as AskPrompt | null) ?? null }));
    });
    return unlistenControl;
  }, [commitSessions, restoreTerminals, closeChat]);

  useEffect(() => {
    let cancelled = false;
    for (const sid of sessionOrder) {
      if (attachedRef.current.has(sid)) continue;
      attachedRef.current.add(sid);
      // `wants_ask`: this window can show a question and answer it. The agent
      // is only offered the ask tools when somebody attached says so, which is
      // why a plan never appeared here — it was never asked for.
      void bus().send("attach", { session: sid, wants_ask: true }).then((ack) => {
        if (cancelled || !ack.ok) attachedRef.current.delete(sid);
      });
    }
    return () => {
      cancelled = true;
    };
  }, [sessionOrder]);

  // A dropped socket means the server no longer has us attached.
  useEffect(() => onBusReconnect(() => {
    const sessions = [...attachedRef.current];
    attachedRef.current.clear();
    for (const sid of sessions) {
      attachedRef.current.add(sid);
      void bus().send("attach", { session: sid, wants_ask: true });
    }
    // The server may have gained terminals while we were away — a phone can
    // open one, and the desktop should not have to be restarted to see it.
    void restoreTerminals();
  }), [restoreTerminals]);

  // Dictation event subscription — driven by the Fn-key gesture monitor and
  // the cpal recording thread (both Rust-side). Routes transcribed text to the
  // focused input and auto-expands the panel when recording starts so the
  // VoiceIndicator is visible.
  useEffect(() => {
    let unlistenDictation: (() => void) | undefined;
    let unlistenDownload: (() => void) | undefined;
    let unlistenUi: (() => void) | undefined;
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

    // A tap of Fn while collapsed. Deliberately just `expand()` with no
    // setView — you land back on whatever view you left, and the panel's
    // view state already survives a collapse.
    onUiCommand((action) => {
      if (action === "expand") expand();
    }).then((u) => {
      if (cancelled) u();
      else unlistenUi = u;
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
      unlistenUi?.();
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
    fetchConfig().then(setConfig).catch(() => {});
    const poll = setInterval(refreshStatus, 1500);
    // The agent server may still be starting; keep trying rather than leaving
    // the chat with no session to send to.
    let cancelled = false;
    const retry = setInterval(() => {
      if (cancelled) return;
      void restoreSessions().then((ok) => {
        if (ok) clearInterval(retry);
      });
    }, 2000);
    void restoreSessions().then((ok) => {
      if (ok) clearInterval(retry);
    });
    void restoreTerminals().then(() => {
      // Only mint one when the server has none: opening a shell nobody asked
      // for, every launch, is how you end up with a drawer full of them.
      setTerminalTabs((tabs) => {
        if (tabs.length === 0) openNewTerminalTab();
        return tabs;
      });
    });
    return () => {
      cancelled = true;
      clearInterval(retry);
      clearInterval(poll);
    };
  }, [refreshStatus, openNewTerminalTab, restoreTerminals, restoreSessions]);

  const send = async (text: string) => {
    const sid = activeSessionId;
    if (!sid || busy || !text.trim()) return;
    setUnreadCompletion(false); // new turn starting → busy blue takes over
    // Deliberately not rendered optimistically. The `user` event comes back
    // over the bus a moment later and opens the exchange itself — drawing it
    // here as well would show every message you send twice, and only for the
    // device that sent it. The bus is the one source of truth for what a
    // session contains, which is the whole reason two devices can share one.
    try {
      // `ask` is advertised: this window can show a question and answer it,
      // and the agent is only offered the ask tools when somebody can.
      const ack = await bus().send("prompt", { session: sid, instruction: text, origin: "app" });
      if (!ack.ok) throw new Error(ack.error ?? "the agent did not accept that");
    } catch (err) {
      // The prompt never reached the agent, so no `user` event is coming to
      // carry it: put the failure on screen ourselves, with the text that was
      // lost, rather than swallowing what the user typed.
      commitSessions((prev) => ({
        ...prev,
        [sid]: [
          ...(prev[sid] ?? []),
          { id: crypto.randomUUID(), role: "user", content: text, steps: [], thinking: false },
          { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: false, error: String(err) },
        ],
      }));
    }
  };

  return (
    <main className="h-full w-full bg-transparent p-0">
      {/* Mounted from the first expand onward and hidden — not unmounted —
          while collapsed. Unmounting tore down every terminal's xterm instance
          along with the buffer it had rendered. The shell and whatever TUI is
          running in it (daimon's own, Claude Code) never stopped, but they have
          no reason to reprint a screen they already drew, so re-expanding
          showed a blank or half-drawn terminal until something forced a
          redraw — which is why resizing the window "fixed" it.

          Kept out of the DOM until the first expand so a session that never
          opens the panel pays nothing for it. */}
      {everExpanded && (
        <div className="relative h-full w-full" style={{ display: expanded ? "block" : "none" }}>
        <Panel
          expanded={expanded}
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
          config={config}
          onCloseChat={closeChat}
          onForgetChat={forgetChat}
          ask={activeSessionId ? (sessionAsks[activeSessionId] ?? null) : null}
          onAnswerAsk={(answer) => {
            if (activeSessionId) void answerAsk(activeSessionId, answer);
          }}
          onAddChat={newChat}
          messages={messages}
          todos={todos}
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
        {/* Over the panel rather than inside it, so one viewer covers every
            view. The wrapper carries the panel's own corner radius — the
            viewer is shared with the phone, where rounding it would be
            wrong. */}
        {linkUrl && (
          <div className="absolute inset-0 overflow-hidden rounded-[28px]">
            <LinkViewer
              url={linkUrl}
              onClose={() => setLinkUrl(null)}
              onOpenExternally={(url) => void openExternal(url).catch(() => {})}
            />
          </div>
        )}
        </div>
      )}
      {!expanded && (
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
