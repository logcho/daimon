import { useCallback, useEffect, useRef, useState } from "react";
import { agentStatus, sendMessage, startChat } from "./api";
import { applyEvent, onSessionStatus } from "./sessionEvents";
import type { AgentStatus, ChatMessage, SessionStatusPayload } from "./types";
import { ChatInput } from "./components/ChatInput";
import { ErrorBanner } from "./components/ErrorBanner";
import { MessageList } from "./components/MessageList";
import { Toolbar } from "./components/Toolbar";

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<AgentStatus | null>(null);

  // The active session id lives in a ref so the single event listener never
  // races a session switch (the legacy placeholder-session race, sidestepped).
  const sessionRef = useRef<string | null>(null);

  const refreshStatus = useCallback(() => {
    agentStatus().then(setStatus).catch(() => setStatus({ running: false, adopted: false, port: 4711 }));
  }, []);

  const handleEvent = useCallback((payload: SessionStatusPayload) => {
    if (payload.session_id !== sessionRef.current) return;
    setMessages((m) => applyEvent(m, payload.event));
    if (payload.event.type === "done" || payload.event.type === "error") {
      setBusy(false);
      refreshStatus();
    }
  }, [refreshStatus]);

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

  // On mount: report agent status and pre-create a chat session.
  useEffect(() => {
    refreshStatus();
    startChat().then((sid) => {
      sessionRef.current = sid;
    }).catch(() => {});
  }, [refreshStatus]);

  const newChat = async () => {
    setBusy(false);
    setMessages([]);
    try {
      sessionRef.current = await startChat();
    } catch {
      // keep the old session; the status dot shows the agent is down
    }
  };

  const send = async (text: string) => {
    const sid = sessionRef.current;
    if (!sid || busy || !text.trim()) return;
    setBusy(true);
    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: "user", content: text, steps: [], thinking: false };
    const asstMsg: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", steps: [], thinking: true };
    setMessages((m) => [...m, userMsg, asstMsg]);
    try {
      await sendMessage(sid, text);
    } catch (err) {
      // Invoke failed before the stream could start (agent down, etc.).
      setMessages((m) => {
        const last = m[m.length - 1];
        if (last?.role === "assistant") {
          return [...m.slice(0, -1), { ...last, thinking: false, error: String(err) }];
        }
        return m;
      });
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col bg-slate-50">
      <Toolbar status={status} onNewChat={newChat} disabled={busy} />
      <MessageList messages={messages} />
      <ChatInput onSend={send} disabled={busy} />
      <ErrorBanner message={status?.running === false ? "Agent server is offline — start it with `uv run daimon-agent` (cwd agents/) or check agents/.env" : null} />
    </div>
  );
}
