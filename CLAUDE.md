# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phase 1 (ambient shell + one background task) and Phase 2 (memory & skills) are both built and verified — see `PROMPT.md` for what that covers concretely and what's still ahead (Phase 3: remote gateway, Phase 4: voice/multi-task/subagents). The landing page (`website/`) also exists as a separate Astro project. Don't treat this file's "target" framing as literal for those areas — check what's actually on disk before assuming a path doesn't exist yet.

## Source of truth

- `ARCHITECTURE.md` is the authoritative design document for the product — stack choices, system architecture, data flow, and security model. Any implementation work should conform to it, and it should be updated if implementation reveals the design needs to change.
- `PROMPT.md` is the founding execution brief (written as a standing instruction to an autonomous agent) describing the Phase 1 MVP build order and operating directives. Treat its "Immediate Execution Steps" as the current build sequence when doing greenfield implementation work here.

## Product summary

Daimon is an ambient, on-device AI co-worker — not a takeover, not a remote desktop, not a chatbot to babysit. A user hands off a real task (e.g. "apply to these jobs") and it runs in the background on their own machine while they keep working on something else. The UI is a small, persistent, Wispr Flow-style floating widget (a "pill"), not a fullscreen overlay or app window.

Planned stack (see `ARCHITECTURE.md` §2 for full detail):
- **Desktop shell:** Tauri v2 (Rust backend, React/TypeScript frontend, Tailwind)
- **Ambient UI:** a small draggable floating pill, expandable into a full pipeline view
- **Orchestration:** LangGraph (TypeScript/Node) for agent planning/state as a graph, with subagent delegation and a growing skill library
- **Execution:** Docker SDK + headless browser (Playwright) providing each task/project its own background workspace
- **Memory & skills:** local SQLite + FTS/embeddings store for cross-session context and reusable learned skills
- **Gateway:** optional bridge to chat platforms (Telegram/Slack/Discord) for remote check-in/control
- **Communication:** Tauri IPC commands bridging the React frontend and the Rust daemon
- **Voice/input:** Deepgram/Whisper streaming transcription
- **Cloud backend:** Modal/Fly.io for optional hosting of long-running/hibernating workspaces

## Architecture model

Four-part model — keep this separation when adding code:

1. **Shell (frontend):** the ambient pill widget (React + Tailwind), toggled/expanded by a global hotkey into a "Thought-Action-Result" pipeline view. No direct host, Docker, or workspace access — everything goes through Tauri IPC.
2. **Daemon (Rust backend):** owns the Tauri IPC handlers, the lifecycle of background workspaces (Docker containers running a headless browser + shell — created on demand, resumable, not always ephemeral), and the optional gateway process.
3. **Orchestrator (LangGraph engine):** decomposes user intent into a graph of actions using a standardized tool library (`read_file`, `shell_exec`, `browse`, `fill_form`, `web_search`, ...) that execute inside a background workspace; pulls relevant memory/skills into context; persists checkpoints so tasks resume across restarts.
4. **Memory/Gateway layer:** durable cross-session memory and skill store, plus an opt-in bridge exposing the same agent/session to external chat channels.

Build phases (see `PROMPT.md` for the authoritative, detailed breakdown — do not skip ahead to a later phase before the current one's "done when" criterion is verified):
- **Phase 1:** ambient pill + hotkey + a single background workspace running one task end-to-end, with live status streamed to the widget. No memory, skills, or gateway yet.
- **Phase 2:** persistent memory + skill library.
- **Phase 3:** remote gateway (starting with Telegram), scoped per channel to read-only status vs. full control.
- **Phase 4:** real voice input, multi-task tracking in the pill, subagent delegation.

Task loop (full, end-state — see `ARCHITECTURE.md` §4 for detail): input (voice/text, from the widget or a remote channel) → LangGraph plan, informed by memory/skills → daemon creates/resumes a background workspace → agent executes steps invisibly inside it → status streams live to the pill (and any connected channel) → progress checkpoints continuously → on completion, result surfaces and new skills/facts are written to memory.

## Non-disruption invariants

These constraints come from `ARCHITECTURE.md` §5 and should hold for any code touching execution or IPC. They exist because the agent runs on the *same machine* the user is actively using — this is the core product requirement, not an incidental security nicety:

- The agent never moves the user's real cursor, steals keyboard focus, or brings another app to the foreground. All GUI-style work (browsing, form-filling) happens inside a background/headless browser instance, invisible to the user's actual screen.
- Each active task/project gets its own background workspace; workspaces don't share state except through the explicit memory store.
- Secrets are injected into the relevant workspace at runtime only — never written to disk in plaintext, never forwarded to a remote gateway channel.
- Remote channels are opt-in per workflow and explicitly scoped (read-only status vs. full control) — never full control by default.

## Target directory structure

```text
/daimon
├── src-tauri/          # Rust backend: IPC handlers, workspace/container manager (built)
├── src/                # React frontend: ambient pill UI + expanded pipeline view (built)
├── agents/             # LangGraph agent server + SQLite memory/skill store (built)
├── memory/             # Local SQLite DB, gitignored — bind-mounted into the workspace container
├── sandbox/            # Dockerfile for the background workspace image (built)
├── website/            # Public landing page, Astro — separate project, own package.json (built)
├── gateway/             # Remote channel bridge (Telegram/Slack/etc.) — Phase 3, not built yet
├── ARCHITECTURE.md      # Source of truth
└── package.json
```

Follow the phase order in `PROMPT.md` for anything not built yet rather than inventing an alternative sequence.

## Project-specific Claude Code skills

Two skills live in `.claude/skills/` for this repo specifically:

- **`daimon-design-system`** — the established colors/typography/spacing/motion conventions across both frontends (app + website), including a real glow-clipping bug worth knowing before adding a `box-shadow` to anything. Load before touching UI code.
- **`daimon-web-verify`** — the correct typecheck/build commands for whichever of the two independent frontend projects (`src/`+`src-tauri/` vs `website/`) you touched, and how to clean up dev servers/containers spun up for verification. Load after changing frontend code, before calling it done.
