import { useCallback, useEffect, useState } from "react";
import { Pill } from "./components/Pill";
import { PipelinePanel } from "./components/PipelinePanel";
import { useHotkeyToggle } from "./hooks/useHotkeyToggle";
import { useDictationHotkey } from "./hooks/useDictationHotkey";
import { collapseToPill, expandToPanel } from "./lib/window";
import {
  closeTerminal,
  endSession,
  onDictationStatus,
  onSessionStatus,
  sendMessage,
  setWindowVibrancy,
  startSession,
  toggleDictation,
} from "./lib/api";
import { applySessionEvent } from "./lib/sessionEvents";
import { playLockEngagedSound, playRecordingStartSound, playRecordingStopSound } from "./lib/sound";
import type { DictationState, PendingDraft, PendingInput, Session, View } from "./types";
import type { UnlistenFn } from "@tauri-apps/api/event";

function App() {
  const [expanded, setExpanded] = useState(false);
  const [sessions, setSessions] = useState<Record<string, Session>>({});
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [dictationState, setDictationState] = useState<DictationState>("idle");
  const [dictationLocked, setDictationLocked] = useState(false);
  const [dictationMessage, setDictationMessage] = useState("");
  const [pendingDraft, setPendingDraft] = useState<PendingDraft | null>(null);

  // Which PipelinePanel tab is showing — lives here rather than as a
  // PipelinePanel-local useState for the same reason `terminalTabs` etc. do
  // below: PipelinePanel fully unmounts on collapse-to-pill, so a plain
  // local `useState<View>("chat")` reset back to "chat" on every single
  // collapse/expand, always dumping the user back on chat instead of
  // wherever they'd actually left off (the terminal tab, mid-`claude`
  // session, for instance).
  const [view, setView] = useState<View>("chat");

  // Lives here, not inside PipelinePanel, specifically so it survives a
  // collapse-to-pill cycle — PipelinePanel fully unmounts whenever `expanded`
  // goes false (see the render below), and state living inside an unmounted
  // component is gone. Terminal tabs (and the real shell processes backing
  // them) previously lived as PipelinePanel-local state, which meant
  // collapsing while e.g. `claude` was running in a terminal tab looked
  // exactly like the tab had been closed: the frontend forgot the tab's id
  // ever existed and could never reconnect, even though the backend shell
  // was likely still alive and simply orphaned. Sessions already got this
  // right from the start (see `sessions`/`activeSessionId` above) — this
  // just brings terminal tabs in line with that same pattern.
  const [terminalTabs, setTerminalTabs] = useState<string[]>([]);
  const [activeTerminalId, setActiveTerminalId] = useState<string | null>(null);
  const [pendingTerminalInput, setPendingTerminalInput] = useState<PendingInput | null>(null);

  // Keyed by terminal tab id, holding a one-shot "type this in once the
  // shell has actually started" command for tabs the *agent* opened (via a
  // `ui_action` event on the session-status stream — see onSessionStatus
  // below), as opposed to `pendingTerminalInput` above, which routes a
  // *dictation* result into whichever tab is currently active.
  const [initialTerminalCommands, setInitialTerminalCommands] = useState<Record<string, string>>({});

  useEffect(() => {
    collapseToPill();
    // Install the native macOS vibrancy material behind the window (see
    // vibrancy.rs). Done here on mount, not at Rust startup, so the window is
    // guaranteed to exist and be shown. 28px = a circle at the collapsed pill
    // size and the panel's rounded corner when expanded, so one value covers
    // both states and the effect never needs re-applying on collapse/expand.
    void setWindowVibrancy(28);
  }, []);

  // Registered once for the app's lifetime — see onSessionStatus for why this
  // has to be a single routed subscription rather than one per session.
  useEffect(() => {
    let unlisten: UnlistenFn | undefined;
    let cancelled = false;

    onSessionStatus((sessionId, event) => {
      if (event.type === "ui_action") {
        if (event.action === "open_terminal_with_command") {
          const id = crypto.randomUUID();
          setTerminalTabs((tabs) => [...tabs, id]);
          setActiveTerminalId(id);
          setInitialTerminalCommands((prev) => ({ ...prev, [id]: event.command }));
          setView("terminal");
          // The panel may currently be collapsed — voice-triggered
          // instructions (and now agent-triggered ones) run regardless of
          // pill/panel state, same as the dictation-result handler below.
          expand();
        }
        return; // never turn/chat content — nothing to fold into a session
      }
      setSessions((prev) => {
        // A real, confirmed race: `start_session`'s Rust command spawns the
        // turn (`spawn_turn`) and returns the session id essentially
        // immediately, while the spawned task goes on to actually start the
        // workspace and begin streaming `session-status` events — those two
        // things (the invoke's own Promise resolving vs. events arriving
        // over Tauri's separate event channel) have no guaranteed ordering.
        // Under load (this app spawning/tearing down many real Chrome
        // processes, which is exactly what heavy testing looks like) the
        // event channel can win, meaning early events — including this
        // turn's very first `live_frame` ticks — would arrive before
        // `submitInstruction`'s own post-`await startSession(...)` handler
        // has created `sessions[sessionId]` at all. The old code silently
        // dropped any event for a session id it didn't yet recognize
        // (`if (!session) return prev`) — exactly enough to explain "the
        // live view/recording never show anything, especially the first
        // turn" without ever surfacing an error. Synthesize a minimal
        // placeholder session/turn instead of dropping the event; the
        // instruction text isn't known from the event stream itself, so
        // `submitInstruction` fills it in afterward — see its own comment.
        const session = prev[sessionId] ?? {
          id: sessionId,
          turns: [{ id: crypto.randomUUID(), instruction: "", steps: [] }],
        };
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

  // Registered once for the app's lifetime, same reasoning as onSessionStatus
  // above — dictation can be toggled while the panel is collapsed (that's
  // the whole point of a hotkey-triggered ambient feature), so this can't be
  // a listener owned by a component that only mounts once expanded.
  useEffect(() => {
    let unlisten: UnlistenFn | undefined;
    let cancelled = false;

    onDictationStatus((event) => {
      if (event.type === "recording") {
        // A "recording" event fires *again* (with locked flipping to true)
        // when a double-tap engages hands-free mode on an already-running
        // recording — not a new recording starting, just an update to how
        // the next Fn press will be interpreted. Distinguish the two via
        // functional updates against the previous state, so the right sound
        // (start vs. lock-engaged) fires for the right one, mirroring the
        // same two moments the Rust side already gives distinct haptics.
        setDictationState((prev) => {
          if (prev !== "recording") playRecordingStartSound();
          return "recording";
        });
        setDictationLocked((prevLocked) => {
          if (event.locked && !prevLocked) playLockEngagedSound();
          return event.locked;
        });
        // Open the chatbox the moment recording starts, not just once the
        // transcribed result comes back — seeing the panel appear is itself
        // useful confirmation that the hotkey actually registered.
        expand();
      } else if (event.type === "transcribing") {
        playRecordingStopSound();
        setDictationState("transcribing");
        setDictationLocked(false);
      } else if (event.type === "result") {
        setDictationState("idle");
        setDictationLocked(false);
        setPendingDraft({ text: event.text, id: crypto.randomUUID() });
        expand();
      } else if (event.type === "no_speech") {
        // Nothing worth showing, but the display state still needs to move
        // off "transcribing" — without this it would stay stuck there
        // indefinitely after every silent/blank recording.
        setDictationState("idle");
        setDictationLocked(false);
      } else if (event.type === "error") {
        setDictationState("error");
        setDictationLocked(false);
        setDictationMessage(event.message);
        // No dedicated surface to dismiss this from when the pill is
        // collapsed — self-clear after a few seconds rather than leaving a
        // permanent red indicator for what's usually a one-off failure
        // (e.g. model not downloaded yet) the user can just retry.
        setTimeout(() => {
          setDictationState((prev) => (prev === "error" ? "idle" : prev));
        }, 4000);
      }
    }).then((fn) => {
      if (cancelled) fn();
      else unlisten = fn;
    });

    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [expand]);

  // The hotkey handler itself only needs to kick the toggle off — the
  // resulting "recording"/"transcribing"/"result"/"error" progression comes
  // back through the onDictationStatus subscription above. A rejection here
  // means the toggle never even started (e.g. the model isn't downloaded
  // yet), which reads the same to the user as a backend "error" event.
  const handleDictationToggle = useCallback(() => {
    toggleDictation().catch((err) => {
      setDictationState("error");
      setDictationMessage(err instanceof Error ? err.message : String(err));
      setTimeout(() => {
        setDictationState((prev) => (prev === "error" ? "idle" : prev));
      }, 4000);
    });
  }, []);

  useDictationHotkey(handleDictationToggle);

  const consumePendingDraft = useCallback(() => {
    setPendingDraft(null);
  }, []);

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
      setSessions((prev) => {
        // Mirror image of the placeholder-creation in the onSessionStatus
        // listener above: if this turn's events already started arriving
        // and synthesized a placeholder entry before this resolved (the
        // race described there), fill the real instruction text into that
        // same turn instead of clobbering whatever steps/liveFrame it's
        // already captured with a brand-new, empty one. Only the "no race
        // happened" case actually needs a fresh turn created here.
        const existing = prev[sessionId];
        if (existing && existing.turns.length > 0) {
          const turns = [...existing.turns];
          const lastIndex = turns.length - 1;
          turns[lastIndex] = { ...turns[lastIndex], instruction };
          return { ...prev, [sessionId]: { ...existing, turns } };
        }
        const turnId = crypto.randomUUID();
        return { ...prev, [sessionId]: { id: sessionId, turns: [{ id: turnId, instruction, steps: [] }] } };
      });
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

  const openNewTerminalTab = useCallback(() => {
    const id = crypto.randomUUID();
    setTerminalTabs((tabs) => [...tabs, id]);
    setActiveTerminalId(id);
  }, []);

  // Closing a tab both ends its real shell process (`closeTerminal`, fired
  // and forgotten — nothing here needs to wait for the kill to land before
  // dropping it from the list) and picks a new active tab if the closed one
  // was it, same "fall back to a neighbor, or to nothing" logic
  // `closeSession` above uses for chat sessions.
  const closeTerminalTab = useCallback((id: string) => {
    void closeTerminal(id);
    setTerminalTabs((tabs) => {
      const remaining = tabs.filter((tabId) => tabId !== id);
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

  const consumePendingTerminalInput = useCallback(() => {
    setPendingTerminalInput(null);
  }, []);

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
          pendingDraft={pendingDraft}
          onConsumePendingDraft={consumePendingDraft}
          dictationState={dictationState}
          dictationLocked={dictationLocked}
          dictationMessage={dictationMessage}
          view={view}
          onViewChange={setView}
          terminalTabs={terminalTabs}
          activeTerminalId={activeTerminalId}
          onSelectTerminalTab={setActiveTerminalId}
          onOpenTerminalTab={openNewTerminalTab}
          onCloseTerminalTab={closeTerminalTab}
          pendingTerminalInput={pendingTerminalInput}
          onConsumePendingTerminalInput={consumePendingTerminalInput}
          onDictateToTerminal={setPendingTerminalInput}
          initialTerminalCommands={initialTerminalCommands}
        />
      ) : (
        <Pill
          sessions={sessionList}
          onExpand={expand}
          dictationState={dictationState}
          dictationLocked={dictationLocked}
          dictationMessage={dictationMessage}
        />
      )}
    </main>
  );
}

export default App;
