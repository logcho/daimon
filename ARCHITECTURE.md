# ARCHITECTURE.md: Project Daimon

**Status:** Initial Foundation (v2 — revised after product clarification)

**Vision:** An ambient, on-device agentic co-worker — not a takeover, not a remote desktop, not a chatbot you babysit. You tell it to do something real (apply to jobs, research something, clean up a folder), it runs the task in the background on your own machine while you keep working on something else, and it surfaces itself as a small persistent status widget until you need more detail.

---

## 1. Product Philosophy

* **An extra set of hands, not a replacement.** The agent works *alongside* the user in parallel — the user should never have to stop what they're doing, hand over the machine, or spin up a separate desktop just to give the agent room to work.
* **Ambient, Wispr Flow-style presence.** A small, unobtrusive floating widget lives on screen at all times — not a fullscreen overlay, not a window you have to manage. It expands on demand for detail and collapses back to a glanceable status pill.
* **Non-disruptive autonomy.** Because the agent runs on the *same* computer the user is actively using, its actions (browsing, filling forms, running commands) must never steal the mouse, keyboard focus, or bring another app to the foreground. All "hands-on" work happens in a background workspace the user never has to look at.
* **Persistent and reachable.** Tasks, memory, and learned skills survive restarts. The user can check in or issue new instructions from a chat channel when away from the machine, not only from the native widget.

## 2. Technical Stack

| Layer | Technology | Purpose |
| --- | --- | --- |
| **Desktop Shell** | Tauri v2 (Rust/React) | Native window management, low footprint, system tray. |
| **Ambient UI** | React + Tailwind, custom floating widget | Wispr Flow-style persistent pill: glanceable status, expands to a full pipeline view on demand. |
| **Orchestration** | LangGraph (TypeScript/Node) | State machine for multi-step agent planning, tool calling, and subagent delegation. |
| **Execution** | Docker SDK + headless browser (Playwright) | Background, invisible workspace per task/project — where the agent actually browses/types/runs shell commands without touching the user's visible screen. |
| **Memory & Skills** | Local store (SQLite + FTS/embeddings) | Cross-session memory of user context and task history; a growing library of reusable learned skills. |
| **Gateway (remote access)** | Lightweight bridge service (Telegram/Slack/Discord to start) | Lets the user check status or send new instructions from a chat app when away from the desktop. Secondary to the native widget, not a replacement for it. |
| **Communication** | IPC (Tauri Commands) | Secure bridge between UI frontend and Rust daemon. |
| **Voice/Input** | whisper.cpp (`whisper-rs`) + `cpal`, plus a macOS `objc2`/`NSEvent` Fn-key hook | On-device, open-source speech-to-text — either a standard hotkey (`CommandOrControl+Shift+D`) or the bare Fn key toggles recording, transcribes locally, fills the widget's chat input. No cloud API/account. See `PROMPT.md` Phase 5. |
| **Cloud Backend** | Modal / Fly.io | Optional hosting for long-running or hibernating background workspaces. |
| **Integrations** | OAuth2 (Gmail, Outlook/Microsoft Graph) + MCP client | Connected-account linking so tasks can act on the user's real email/calendar, plus a standard protocol for pulling in third-party tool servers as additional agent tools. Planned — see `PROMPT.md` Phase 6. |

---

## 3. System Architecture

The system follows a **four-part model**: Shell, Daemon, Orchestrator, and the Memory/Gateway layer.

### A. The Shell (Ambient UI)

* **Widget:** A small, semi-transparent, draggable floating pill — always present, low profile, styled after Wispr Flow rather than a traditional app window.
* **Trigger:** Global hotkey (e.g. `Cmd+Shift+Space`) expands/collapses the pill into a full "Thought-Action-Result" pipeline view.
* **Multi-task aware:** The pill can track and let the user switch between several concurrently running background tasks ("3/10 applications submitted", "researching flights...").

### B. The Daemon (Rust Backend)

* **Workspace Lifecycle Manager:** Owns the background workspaces (Docker containers running a headless browser + shell) — one per active task or project, created on demand and resumable, not always torn down after a single run.
* **IPC Handler:** Bridges frontend requests to the orchestrator and to workspace state.
* **Gateway Supervisor:** Manages the optional remote-access bridge process.
* **Automation Scheduler (Phase 10, implemented):** a background loop (30s poll) that fires recurring instructions (cron-scheduled, e.g. a daily brief) on their own, with no user interaction — each firing reuses the Phase 8 session machinery but auto-tears-down its container once the run completes, since an unattended recurring job doesn't need the "stay open until closed" continuation an interactive chat does. Also picks up automation-creation requests the agent itself submits from inside a session (see (C) below) — the container has no reverse channel to call back into the daemon, so this goes through a bind-mounted directory the same way vault notes do, not a new network listener.

### C. The Orchestrator (LangGraph Engine)

* **Agent Node:** Receives user intent (from the widget or a remote channel) and decomposes it into a graph of actions, pulling relevant memory and skills into context.
* **Tooling:** Standardized library of tools that operate inside a background workspace (`read_file`, `shell_exec`, `browse`, `fill_form`, `web_search`).
* **Subagents:** Independent sub-steps (e.g. checking several job boards at once) can be delegated to parallel subagents.
* **Skill Library:** After successfully handling a novel task, the agent can persist a reusable, parameterized "skill" for future reuse.
* **State:** Persistent `Checkpoint` storage so long-running tasks (a multi-hour job-application run) survive app or machine restarts.
* **Open question, not yet decided:** whether Claude Code / the Claude Agent SDK could serve as (or alongside) this LangGraph engine for planning and tool orchestration. Noted here so the idea isn't lost; evaluate deliberately against LangGraph rather than swapping the stack row above without a real comparison. A narrower, additive idea in the same neighborhood — an embedded terminal running the real `claude` CLI directly, voice-dictation-integrated, not a change to this orchestrator — is planned separately as `PROMPT.md` Phase 11; the two don't need to be resolved together.
* **Sessions, not one-shot tasks (Phase 8, implemented):** a session's background workspace stays alive between messages, so a follow-up genuinely continues (same browser/page state) rather than starting fresh — the "tear down immediately" policy from Phase 3 now applies only to teardown-on-session-end (explicit, or app exit), not after every message. See `PROMPT.md` Phase 8.
* **Agent-created automations (Phase 10, implemented):** a `create_automation` tool lets the agent register a recurring instruction mid-conversation, not only through a settings form — it writes a pending request into a bind-mounted `automations/` directory (same shape as a vault note) for the daemon's scheduler to validate and promote.

### D. Memory & Skills

* Durable, queryable record of past tasks, user context (e.g. resume, preferences), and learned skills.
* Retrieval via FTS/embeddings; periodic LLM summarization keeps it from growing unbounded.
* **Vault notes (Phase 9, implemented)** are a third indexed category alongside task history and skills — a file/notes vault (a real Obsidian vault the user points Daimon at, or a Daimon-native folder if unconfigured) bind-mounts into each session's container next to the existing memory mount, with dedicated `write_note`/`read_note`/`list_notes` tools; note content is indexed into the same FTS store and pulled into planning context like any other memory. Browsing (listing/reading files) happens directly against the host filesystem, no container needed. See `PROMPT.md` Phase 9.

### E. The Gateway (Remote Access)

* Optional bridge exposing the same agent/session to external chat platforms so the user can check progress or give new instructions while away from the machine.
* Opt-in per workflow, and can be scoped to read-only status vs. full control.

### F. Integrations (Connected Accounts & MCP) — planned, Phase 6

* **Connected accounts:** OAuth2 linking (Gmail, Outlook/Microsoft Graph to start) so a task can act on the user's real inbox/calendar, not just browse the open web. Authorization-code + PKCE via the system browser, not an embedded webview.
* **MCP client:** the tool library in (C) becomes extensible — in addition to the built-in tools, the orchestrator can load tools exposed by external MCP servers the user configures. Additive to the skill library, not a replacement: skills are learned/parameterized task patterns; MCP servers are raw tool sources.

---

## 4. Data Flow: The "Task Loop"

1. **Input:** User gives Daimon an instruction via the widget (voice or text), or via a connected remote channel.
2. **Plan:** LangGraph decomposes intent into a graph of actions, drawing on relevant memory/skills.
3. **Spin-up:** Daemon creates or resumes a background workspace (Docker container + headless browser) for this task/project.
4. **Execute:** Agent performs the real work — browsing, form-filling, shell commands — entirely inside the background workspace, invisible to the user's foreground screen.
5. **Stream:** Live status renders on the ambient pill (and, if connected, mirrors to a remote channel); full logs are available in the expanded view.
6. **Checkpoint:** Progress is checkpointed continuously so long tasks can pause/resume across restarts.
7. **Finalize:** On completion, the agent surfaces a summary; new reusable skills or learned facts are written to memory.

---

## 5. Non-Disruption & Isolation Guidelines

* **Foreground protection:** The agent never moves the user's real cursor, steals keyboard focus, or brings another app to the front. All GUI-style work happens inside a background/headless browser instance, not on the user's visible desktop.
* **Workspace isolation:** Each active task/project gets its own background workspace; workspaces don't share state with each other except through the explicit memory store.
* **Secrets:** Credentials needed for a task are injected into the relevant workspace at runtime only — never written to disk in plaintext, never forwarded to a remote gateway channel.
* **Remote channel scoping:** Each connected chat channel is explicitly granted read-only status or full control — never full control by default.
* **Connected-account tokens:** OAuth2 access/refresh tokens follow the same rule as task secrets — stored in the OS keychain (never plaintext on disk), scoped to the minimum the task needs, and disconnectable per-account from Settings at any time.

---

## 6. Directory Structure

```text
/daimon
├── src-tauri/          # Rust backend: IPC handlers, workspace/container manager, gateway supervisor
├── src/                # React frontend: ambient pill UI + expanded pipeline view
├── agents/             # LangGraph definitions, tool schemas, skill library
├── memory/             # Persistent memory & skills store
├── vault/               # Daimon-native notes vault (fallback when no Obsidian vault path is configured) — Phase 9, built
├── automations/         # Pending agent-created automation requests (bind-mounted, gitignored) — Phase 10, built
│                        # (automations.json — the real store — lives at the project root, also gitignored)
├── gateway/             # Remote channel bridge (Telegram/Slack/etc.)
├── cli/                 # Terminal companion sharing the daemon/orchestrator session — Phase 7, not built yet
├── sandbox/            # Background workspace templates (headless browser + shell)
├── website/            # Public landing page (Astro) — separate project, own package.json
├── ARCHITECTURE.md     # This file (Source of Truth)
└── package.json        # Dependencies
```

---

*This architecture is subject to evolution — treat it as a living source of truth, not a fixed spec, and update it whenever implementation reveals the design needs to change.*
