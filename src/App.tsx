import { useCallback, useEffect, useState } from "react";
import { Pill } from "./components/Pill";
import { PipelinePanel } from "./components/PipelinePanel";
import { useHotkeyToggle } from "./hooks/useHotkeyToggle";
import { collapseToPill, expandToPanel } from "./lib/window";
import { endSession, onSessionStatus, sendMessage, startSession } from "./lib/api";
import { applySessionEvent } from "./lib/sessionEvents";
import type { Session } from "./types";
import type { UnlistenFn } from "@tauri-apps/api/event";

function App() {
  const [expanded, setExpanded] = useState(false);
  const [sessions, setSessions] = useState<Record<string, Session>>({});
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

  useEffect(() => {
    collapseToPill();
  }, []);

  // Registered once for the app's lifetime — see onSessionStatus for why this
  // has to be a single routed subscription rather than one per session.
  useEffect(() => {
    let unlisten: UnlistenFn | undefined;
    let cancelled = false;

    onSessionStatus((sessionId, event) => {
      setSessions((prev) => {
        const session = prev[sessionId];
        if (!session) return prev;
        return { ...prev, [sessionId]: applySessionEvent(session, event) };
      });
    }).then((fn) => {
      if (cancelled) fn();
      else unlisten = fn;
    });

    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  const expand = useCallback(() => {
    setExpanded(true);
    expandToPanel();
  }, []);

  const collapse = useCallback(() => {
    setExpanded(false);
    collapseToPill();
  }, []);

  const toggle = useCallback(() => {
    setExpanded((prev) => {
      const next = !prev;
      if (next) expandToPanel();
      else collapseToPill();
      return next;
    });
  }, []);

  useHotkeyToggle(toggle);

  const submitInstruction = useCallback(
    async (instruction: string) => {
      if (activeSessionId) {
        const turnId = crypto.randomUUID();
        setSessions((prev) => {
          const session = prev[activeSessionId];
          if (!session) return prev;
          return {
            ...prev,
            [activeSessionId]: {
              ...session,
              turns: [...session.turns, { id: turnId, instruction, steps: [] }],
            },
          };
        });
        await sendMessage(activeSessionId, instruction);
        return;
      }

      const sessionId = await startSession(instruction);
      const turnId = crypto.randomUUID();
      setSessions((prev) => ({
        ...prev,
        [sessionId]: { id: sessionId, turns: [{ id: turnId, instruction, steps: [] }] },
      }));
      setActiveSessionId(sessionId);
    },
    [activeSessionId]
  );

  // Clears the active selection so the next submit starts a brand-new
  // session instead of continuing whatever's currently open — the only way
  // to ever have more than one session resident at once.
  const startNewSession = useCallback(() => {
    setActiveSessionId(null);
  }, []);

  const closeSession = useCallback(
    async (sessionId: string) => {
      await endSession(sessionId);
      setSessions((prev) => {
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
      setActiveSessionId((prev) => (prev === sessionId ? null : prev));
    },
    []
  );

  const sessionList = Object.values(sessions);
  const activeSession = (activeSessionId && sessions[activeSessionId]) || null;

  return (
    <main className="h-full w-full bg-transparent p-0">
      {expanded ? (
        <PipelinePanel
          sessions={sessionList}
          activeSession={activeSession}
          onSelectSession={setActiveSessionId}
          onNewSession={startNewSession}
          onCloseSession={closeSession}
          onCollapse={collapse}
          onSubmit={submitInstruction}
        />
      ) : (
        <Pill sessions={sessionList} onExpand={expand} />
      )}
    </main>
  );
}

export default App;
