# SYSTEM INSTRUCTION: AUTONOMOUS LEAD ENGINEER FOR DAIMON

You are the Lead Founding Engineer tasked with building the MVP for **Daimon**—an ambient, on-device AI co-worker. Refer to `ARCHITECTURE.md` in the workspace root as your single source of truth for product vision, tech stack, and design language. It is a living document — update it when implementation reveals the design needs to change.

Your goal is to autonomously architect, implement, test, and debug each phase below until it compiles and runs on the host system, in order. Do not start a phase before the prior one is verifiably working.

---

## 1. CORE PRODUCT REQUIREMENT

Daimon must let a user hand off a real task ("apply to these 10 jobs", "research flights to Tokyo") and have it run **in the background on their own machine while they keep working on something else** — no separate desktop, no takeover, no stolen mouse/keyboard focus. The UI is a small, persistent, Wispr Flow-style floating widget, not a fullscreen app window.

---

## 2. PHASE 1 — MVP: Ambient Shell + One Background Task ✅ DONE

Scope tightly. Do not build memory, skills, or the remote gateway yet.

1. **Desktop Shell (Tauri + Rust + React/TS):**
   - System-tray/menu-bar app using Tauri v2.
   - Small, draggable, semi-transparent, always-on-top floating widget (the "pill") — not a fullscreen overlay.
   - Global hotkey toggles between collapsed pill and an expanded pipeline view.
   - Minimalist dark theme, high-contrast typography, crisp status animations.

2. **Background Workspace (Execution Layer):**
   - Rust-native manager for a Docker container running a headless browser (Playwright) + shell.
   - Lifecycle ops: create/resume workspace, `exec` inside it, stream stdout/stderr and browser state back to the daemon.
   - Verify concretely: the agent can complete a real multi-step browser task (navigate, fill a form, submit) with zero visible impact on the user's actual screen, mouse, or keyboard.

3. **Orchestration (LangGraph):**
   - Single agent node: receive instruction → plan → execute steps in the background workspace → report result.
   - No subagents, no skill library yet — get one task working end-to-end first.

4. **Streaming:**
   - Live "Thinking... / Running: <step> / Done" status streamed from workspace to widget in real time.

5. **Input:**
   - Text input on the widget. Stub the voice input path (Deepgram/Whisper) but don't wire it up yet.

**Phase 1 is done when:** a user can type an instruction into the pill, watch live status update while they use other apps normally, and get a completed result — with the background workspace never touching their foreground session.

---

## 3. PHASE 2 — Memory & Skills ✅ DONE

- Local memory store (SQLite + FTS/embeddings) recording task history and user context.
- Agent retrieves relevant memory when planning a new task.
- Skill library: after a novel task succeeds, persist it as a reusable, parameterized skill.

Shipped as: `agents/src/memory.ts` (SQLite + FTS5, bind-mounted host-side so it
survives container recreation, not just restarts), retrieval wired into the
system prompt in `agents/src/run.ts`, and a `save_skill` tool the agent calls
at its own judgment for genuinely reusable patterns.

## 4. PHASE 3 — Concurrent Multi-Task Execution ✅ DONE

Right now there is exactly one background workspace (`daimon-workspace-default`,
one fixed container, one fixed port, one shared Playwright browser page).
`ARCHITECTURE.md` §5 already states the target design — "each active
task/project gets its own background workspace" — Phase 1 just simplified
that down to a single shared one to get one task working end-to-end first.
This phase finishes what was already the stated architecture, so that
multiple instructions can genuinely run at once instead of one blocking
the next.

**Backend:**
- Generalize `workspace.rs` from one fixed-name container to a pool of
  per-task containers (e.g. `daimon-workspace-<task-id>`), each with a
  dynamically allocated host port instead of the current fixed
  `127.0.0.1:4711` binding, so N containers can run concurrently without
  colliding.
- Per-task containers isolate the Playwright browser as a side effect
  (today's single shared container means two concurrent tasks would fight
  over one browser page — separate containers each get their own).
- Memory stays shared across all of them: every container still bind-mounts
  the same host `memory/` directory (SQLite's WAL mode is built for this —
  concurrent readers plus one writer at a time is fine at our write
  frequency).
- **Teardown/pool policy — decided:** tear down each task's container
  immediately once its event stream ends (success, error, or a
  workspace-connection failure), no idle keep-alive. This means the number
  of live containers is always exactly the number of tasks actually in
  flight — never more — so there's no separate concurrency cap or reaper
  to build either. The tradeoff, accepted deliberately: every task pays a
  fresh container-start cost (image build is still cached process-wide via
  the existing `OnceCell`, so this is a `docker run` + health check, not a
  rebuild) rather than reusing a warm one. Revisit with a real idle-timeout
  pool only if that per-task startup latency turns out to matter in
  practice — don't build it preemptively.
- `start_task` already returns a per-task UUID and tags every event with
  it, so the event-routing side of this was accidentally already built for
  concurrency in Phase 1 — the gap is entirely in workspace.rs's
  one-container assumption.

**Frontend:**
- Replace the single `task: Task | null` state in `App.tsx` with a
  collection of concurrently tracked tasks.
- Pill: reflect that more than one task may be running (e.g. a count, or
  cycling status) instead of assuming exactly zero or one.
- Panel: a session list/switcher so the user can start a new task without
  losing visibility into ones already running, and flip between their live
  logs.

**Done when:** a user can fire off two unrelated instructions back-to-back
without waiting for the first to finish, watch both progress independently
with no cross-task interference, and switch between their live status in
the UI.

**Status: implemented.** `workspace.rs` now derives a per-task container
name and lets Docker assign the host port (`-p 127.0.0.1::4711`, read back
via `docker inspect`); `task.rs` tears the container down unconditionally
after the stream ends. The frontend replaced `task: Task | null` with a
`Record<string, Task>` plus an `activeTaskId`, routed through one
app-lifetime `task-status` listener in `App.tsx` (the old per-submit
listen/unlisten would have silently orphaned any task still running when a
second one started — the real bug this phase had to fix, not just a
missing feature). The pill shows a count badge + cycling tooltip once more
than one task is running; the panel gained a horizontal task-switcher chip
row above the transcript, shown whenever more than one task exists.

## 5. PHASE 4 — Remote Gateway

- Bridge service connecting the same agent/session to one chat platform (start with Telegram).
- Status mirrors to the channel; new instructions can be issued from it.
- Explicit per-channel scoping: read-only status vs. full control — never full control by default.

Worth having Phase 3 land first: a gateway that can only run one task at a
time (because the backend can only run one task at a time) is a much
weaker feature than one built on top of real concurrency from the start.

## 6. PHASE 5 — Voice & Polish

- Wire up real-time voice transcription (Deepgram/Whisper) into the widget.
- Subagent delegation for independent parallel sub-steps.

Note: a chunk of the "polish" half of this phase landed early, out of order,
in response to direct feedback rather than waiting for its turn — the pill is
now an icon-only corner widget with animated expand/collapse, a chat-style
panel layout, a themed scrollbar, a terminal-esque visual language, and a
Settings view. Multi-task tracking (originally listed here) moved to Phase 3
since it's really the frontend half of a backend problem, not a polish item.
What's still outstanding here is voice input and subagent delegation.

## 7. PHASE 6 — Integrations, Connected Accounts & Onboarding

Today onboarding is "paste an `ANTHROPIC_API_KEY` into Settings" (shipped in
the in-app API-key-config work). Real tasks — "apply to these jobs", "triage
my inbox" — need the agent to act with the user's actual accounts, and a
first-run flow that makes connecting them feel as easy as tools like Hermes
make it, not a config file the user has to go find.

- **OAuth2 account linking:** Gmail (Google OAuth2) and Outlook
  (Microsoft Graph OAuth2) as the first two providers — read/send email,
  calendar. Authorization-code + PKCE, consent screen opened in the
  system browser, not an embedded webview (avoids embedded-webview
  password capture and matches how other native OAuth clients do this).
- **Token storage:** access/refresh tokens go in the OS keychain (macOS
  Keychain / Windows Credential Manager), never written to disk in
  plaintext — the same rule `ARCHITECTURE.md` §5 already states for task
  secrets, extended to cover long-lived connected-account tokens.
- **Scoped, revocable, per-account:** Settings gains a "Connected
  Accounts" section — connect/disconnect per provider, see granted
  scopes, request the minimum scope a task actually needs rather than
  blanket access up front.
- **Onboarding rework:** first-run flow walks through (1) API key/model
  config, (2) optional account connections with a plain explanation of
  what each grants, (3) a first-task suggestion — instead of dropping the
  user straight into an empty pill. Sequence one decision at a time,
  contextually, the way Hermes-style onboarding does, rather than a
  single settings wall.
- **MCP as a tool-library extension:** alongside the built-in tool set
  (`read_file`, `shell_exec`, `browse`, ...), let the orchestrator load
  additional tools from external MCP servers the user configures (e.g. a
  Gmail MCP server, a Linear MCP server) — the same shape as Claude
  Code's own MCP support. Additive to the Phase 2 skill library, not a
  replacement: skills are learned/parameterized task patterns; MCP
  servers are raw tool sources.

Worth sequencing after the gateway (Phase 4) and voice/subagents (Phase
5): connected accounts and MCP meaningfully raise what a single task can
do, and are more valuable once tasks can run concurrently and be checked
in on remotely. Do not start this phase early.

## 8. PHASE 7 — CLI Companion

A terminal-based companion to the desktop app, in the spirit of tools
like Hermes/OpenClaw — lets the same agent/session be driven from a
terminal for users who live there, without needing the floating widget
open.

- Read-only status and full-control modes, mirroring the Phase 4
  gateway's per-channel scoping model rather than inventing a separate
  permission scheme.
- Shares the same daemon/orchestrator session as the desktop app — not a
  second independent agent — so task state, memory, and skills stay
  unified regardless of which surface issued the instruction.
- Scope and design intentionally deferred until Phases 4-6 land; noted
  here so the idea isn't lost, not to be started early.

## 9. PHASE 8 — Continuable Sessions ✅ DONE

Today `start_task` is one-shot: every instruction gets a brand-new agent
invocation with no memory of anything except what the separate FTS
memory/skill store can fuzzy-recall (Phase 2) — there's no way to send a
follow-up message into an already-running or already-finished task and
have it continue the same conversation. Requested directly by the user:
chats should be sessions that can be continued, not single fire-and-forget
commands.

**Workspace continuity — decided:** a session's background workspace
(container + headless browser) stays alive between messages, so a
follow-up genuinely continues from where the browser left off — same
logged-in page, same open tab — rather than starting a fresh browser each
message. This reverses Phase 3's "tear down immediately, no idle
keep-alive" policy for sessions specifically: a session's container now
stays resident (and its Chromium instance idle-but-alive) for as long as
the session itself is open, teardown moving from "after every message" to
"when the session ends." Accepted deliberately, same way Phase 3 accepted
its own tradeoff in the other direction — revisit only if resource cost
from long-idle sessions turns out to matter in practice.

**Status: implemented.** `src-tauri/src/task.rs` became `session.rs`:
`start_session`/`send_message`/`end_session` replace `start_task`, and the
event channel is `session-status` (`{session_id, event}`). `workspace.rs`
gained a `SESSION_PORTS` in-memory cache (`session_id -> port`) —
`ensure_session_workspace` returns a cached port with zero Docker calls if
the session's container is already up, only creating one on a session's
first message; `end_session_workspace` is now the *only* thing that tears
a container down (plus a best-effort sweep of every still-open session on
app exit, via a `tauri::RunEvent::Exit` handler). The old
`RunFailure::Stalled` / grace-period-before-teardown logic from the
hang-detection pass was simplified away — nothing auto-tears-down on a
stall anymore, so there was nothing left to delay teardown *of*; the 90s
stall-detection timeout itself was kept as-is.

`agents/src/run.ts`'s `runTask` became `runTurn`, threading a module-level
`conversation: BaseMessage[]` across calls (correct scope specifically
because each container is now 1:1 with a session for its whole lifetime —
no session-id keying needed at that layer). A failed turn rolls
`conversation` back to before that turn's `HumanMessage` — otherwise a
dangling unanswered human turn would break Anthropic's strict
user/assistant alternation on every later turn in the session, not just
the failed one.

Frontend: `Task` became `Session { id, turns: Turn[] }` (a `Turn` is what
`Task` used to be — instruction/steps/result/error, one per message).
`PipelinePanel` renders every turn in the active session as a growing
transcript, gained a session chip row with a close ("×") per chip and a
"+" to start a new one (required now, since sessions no longer
auto-teardown — an unclosed session is a resident container + idle
Chromium instance until explicitly closed or the app quits), and disables
the input while the active session's last turn is still in flight
(sending a second message before the first resolves would race two turns
against the agent server's shared `conversation` array).

## 10. PHASE 9 — Vaults / Obsidian Integration ✅ DONE

A file/notes vault, scoped through direct back-and-forth with the user
(previously listed under Future Ideas as unscoped — now decided):

- **Both a real Obsidian vault and a Daimon-native file browser** — not an
  either/or. The UI gets one file-browser surface (a third panel/tab
  alongside chat/settings); what backs it depends on configuration. If the
  user points Settings at an existing Obsidian vault path, the agent reads
  and writes real markdown files there (visible in their actual Obsidian,
  no special wikilink handling needed for basic compatibility — Obsidian
  treats any markdown dropped into a vault folder as a note). If
  unconfigured, the same browser is backed by a Daimon-managed default
  folder instead (`vault/` at the project root, gitignored like `memory/`)
  — same UI either way, just a different directory underneath.
- **Vault content feeds back into agent context** — extends Phase 2's
  memory/skill retrieval rather than being purely a display surface. Notes
  in the vault (whether user-authored or agent-written) get indexed into
  the existing SQLite FTS store as a third searchable category alongside
  task history and skills, and pulled into planning context the same way.
- **How the agent reaches it:** the vault directory bind-mounts into each
  session's container next to the existing `memory/` mount (see
  `workspace.rs`'s `memory_mount` for the existing pattern to follow), at
  a fixed in-container path. New agent tools (`write_note`, `read_note`,
  `list_notes`) operate on that mount, alongside the existing tool
  library.
- **Indexing trigger:** re-index a note into the FTS store immediately
  whenever `write_note` is called (keeps agent-authored notes searchable
  within the same session), plus a one-time scan-and-index pass at agent
  container startup so pre-existing notes a user already wrote directly
  in their real Obsidian vault are searchable too, not just agent-authored
  ones.
- **Browsing doesn't need a running container** — the vault directory
  lives on the host filesystem (that's what gets bind-mounted), so the
  Rust daemon can list/read it directly for the UI's file browser without
  going through a session's container at all; only the agent's own
  read/write *during* a task needs the container-side tools above.

**Status: implemented.** New `src-tauri/src/vault.rs`: `vault_path()`
resolves `OBSIDIAN_VAULT_PATH` (falling back to `vault/` at the project
root, gitignored like `memory/`), plus `get_vault_path_status`/
`set_vault_path`/`list_vault_files`/`read_vault_file` — the latter two
read the host filesystem directly, no container involved.
`workspace.rs`'s `run_container` bind-mounts that directory into
`/workspace/vault` alongside the existing `memory` mount.

`agents/src/vault.ts` gained `writeNote`/`readNote`/`listNotes` (both
sides sanitize filenames to a bare basename before touching the
filesystem — no path-traversal via a note name). Three new tools
(`write_note`/`read_note`/`list_notes`) joined the agent's tool library;
`write_note` also indexes immediately via a new `notes`/`notes_fts`
table in `agents/src/memory.ts` (upserted by filename, unlike the
append-only `tasks`/`skills` tables), and `formatMemoryContext` gained a
third "Vault notes:" section alongside task history and skills.
`server.ts` does a one-time startup scan indexing whatever's already in
the vault, so user-authored notes are searchable too, not just
agent-written ones.

Frontend: a new `vault` tab in `PipelinePanel` (`VaultPanel.tsx`) lists
files (most-recently-modified first) and shows their content on click;
`Settings.tsx` gained a matching section to view/configure the vault
path.

## 11. PHASE 10 — Cron Jobs / Automations ✅ DONE

A scheduled, recurring instruction (e.g. "daily brief every morning at
8am") that fires on its own without the user opening the app or typing
anything — and critically, requested directly: the agent itself should be
able to create one mid-conversation ("give me a daily brief on X every
morning"), not just the user configuring one by hand in a settings form.

**The real design wrinkle:** the agent runs inside a Docker container that
only ever gets called *into* over HTTP by the Rust daemon (`POST /task`)
— there's no existing reverse channel for the container to call back out
to the daemon, and opening one (a host-reachable listener the container
can hit) would be a real trust-direction reversal worth avoiding rather
than a small detail. Reuse the pattern already established for `memory/`
and `vault/` instead: a third bind-mounted directory,
`automations/` (host, gitignored) ↔ `/workspace/automations`
(container). The agent's new `create_automation` tool writes a small
pending-request JSON file into that mount; the daemon's scheduler loop
picks it up, validates it, and promotes it into the real automations
store — the container never reaches back into the daemon directly, it
just writes into a mount the daemon already owns, the same shape as a
vault note.

- **Storage:** `automations.json` at the project root (gitignored),
  a plain array of `{ id, name, instruction, schedule (cron
  expression), enabled, createdAt, lastRunAt, lastRunStatus,
  lastRunResult }`. No new database — this is a small, infrequently
  written list; a JSON file is the right amount of infrastructure for
  it, consistent with how `.env` is used for simple settings elsewhere in
  `src-tauri/`.
- **Schedule format:** standard cron syntax (`"0 8 * * *"` = daily at
  8am). The model is already good at translating "every morning at 8am"
  into that from a natural-language conversation — the tool schema just
  needs a clear description and an example, not a custom scheduling
  DSL invented for this.
- **Validation happens on both sides, for different reasons:** the Node
  agent server does a quick sanity-check parse of the cron string
  immediately when `create_automation` is called, so the agent gets fast
  feedback in-conversation if it generated something malformed, rather
  than silently submitting garbage and finding out never. The Rust
  daemon re-validates authoritatively when it actually promotes a pending
  request into `automations.json` — the source of truth, not a rubber
  stamp of whatever the container claimed.
- **Firing an automation reuses the Phase 8 session machinery** — a
  triggered automation is a real session (same `session-status` event
  channel, same container-per-session mechanism), so if the app happens
  to be open when one fires, it just shows up as a new session like any
  other. **Difference: it auto-tears-down its container once the run
  completes**, unlike an interactive session — a daily-recurring
  automation piling up open, idle Chromium instances forever (Phase 8's
  "stays open until explicitly closed" policy) would be a real resource
  leak for something that runs unattended by design and doesn't need
  continuation the way an interactive chat does.
- **Result visibility when the app wasn't open at trigger time:** the
  scheduler writes the outcome into that automation's own
  `lastRunAt`/`lastRunStatus`/`lastRunResult` fields in
  `automations.json` — the Automations UI section reads this directly, so
  "what did this morning's brief say" doesn't depend on having had the
  app open at 8am. Full per-automation run history (more than just the
  latest result) is a reasonable stretch goal, not required for this
  first slice.
- **Scope, deliberately kept tight:** create / enable-disable / delete —
  not a full instruction/schedule editor. If editing an existing
  automation's instruction or schedule turns out to matter in practice,
  that's a natural small follow-up, not something to build preemptively
  here.

**Status: implemented.** New `src-tauri/src/automation.rs`: an
`Automation` store (`automations.json`, in-memory-cache-backed like
`workspace.rs`'s `SESSION_PORTS`), `list_automations`/
`create_automation`/`set_automation_enabled`/`delete_automation`
commands, cron parsing/due-checking via the `cron` + `chrono` crates
(unlike the dependency-free RFC3339 formatting used elsewhere for a
single display string, real calendar-aware scheduling logic earns its
dependencies), and a `poll_and_fire` scheduler tick (spawned on a 30s
loop in `lib.rs`'s `run()`) that promotes pending agent-submitted
requests from the bind-mounted `automations/` directory and fires every
due, enabled automation through a new `session::run_ephemeral` — a
variant of the normal session flow that captures the final result and
**unconditionally tears its container down**, unlike an interactive
session. `workspace.rs`'s `run_container` gained the third bind mount
alongside `memory`/`vault`.

`agents/src/automation.ts` gained `requestAutomation`, validating the
cron string via `cron-parser` for fast in-conversation feedback (not
authoritative — the Rust side re-validates when promoting) before writing
a pending-request file; a new `create_automation` tool joined the
agent's tool library.

Frontend: a new `automations` tab (`AutomationsPanel.tsx`) lists
automations with an enable/disable toggle, delete, a last-run summary
line, and a creation form — refetches on every visit to the tab since the
agent can add automations mid-conversation, not only through this form.

One real incident during this phase's build, worth recording: a test
automation (schedule `* * * * *`, i.e. every minute) briefly leaked into
the shared `automations.json` a running dev instance had already loaded
into memory before it was cleaned up — since that dev process only reads
the file at startup, it kept re-firing the stray entry (real Anthropic
API calls) every minute until the process was killed and the file reset.
The tests that exercise `poll_and_fire` operate against the real
project-root `automations.json`/`automations/` (consistent with this
repo's existing philosophy of testing against real Docker workspaces
rather than mocks — see `session.rs`'s tests), which means an interrupted
test run (panic, timeout, ctrl-C) can leave a real entry behind in a way
a fully-isolated test wouldn't. Worth knowing if a future test run ever
gets interrupted: check `automations.json` for anything unexpected before
trusting it.

---

## 12. OPERATIONAL DIRECTIVES & AUTONOMY GUIDELINES

- **Full Execution Authority:** Create files, install dependencies (`npm`, `cargo`, etc.), initialize git repositories, run build commands (`cargo check`, `tauri dev`, `npm run build`), read compiler/runtime errors, and fix them self-correctively.
- **Incremental Commits:** Commit at every functional milestone with clear, semantic messages (e.g. `feat(tauri): floating pill widget + hotkey toggle`, `feat(workspace): headless browser task execution in Docker`).
- **Production Quality:** Modular, typed, well-structured code. No placeholder pseudo-code — complete implementation logic and real error handling.
- **Build Validation:** Never mark a step complete until the project compiles and runs cleanly, and the phase's concrete "done when" criterion has been manually verified.
- **Stay in scope:** Do not pull work from a later phase into an earlier one, even if it seems easy — the phasing exists to keep each milestone independently verifiable.

---

## 13. IMMEDIATE EXECUTION STEPS (Phase 1)

1. Confirm `ARCHITECTURE.md` reflects current understanding; flag and resolve any conflicts before writing code.
2. Scaffold the project (Tauri v2 + Rust + React + TypeScript + Tailwind).
3. Implement the floating pill widget with hotkey toggle and expanded pipeline view.
4. Implement the Docker-backed background workspace manager (headless browser + shell) and its IPC bridge.
5. Wire a single LangGraph agent node to plan and execute one task end-to-end inside the workspace, streaming status to the widget.
6. Manually verify the Phase 1 "done when" criterion, then run the initial developer build.

Begin implementation now.
