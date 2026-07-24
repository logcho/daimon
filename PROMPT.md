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

**Reliability fixes, reported directly: agent responses reading as long/badly
formatted, and a reproducible web_search → read_page → open_url loop.**
Investigated by actually reading `agents/src/browser.ts`/`tools.ts` and the
frontend's rendering code rather than guessing at prompt-tuning first — found
two real, mechanical causes, not just an LLM reasoning weakness:

1. **The loop's root cause:** `browser.ts` had `open_url`, `read_page`,
   `click`, `fill`, *and* `web_search` all sharing one single Playwright
   page. Every `web_search` call silently navigated that shared page away
   from wherever `open_url` had just left it, onto DuckDuckGo's own results
   page. If the model then called `read_page` (an easy ordering mistake —
   it takes no argument, so nothing about the call itself says which page
   it means), it got DuckDuckGo's results text back instead of the page it
   actually wanted, which reads as "nothing changed" — exactly the signal
   that drives a model to retry the same sequence. Fixed by splitting
   `search()` onto its own dedicated page (`getSearchPage()`), separate from
   `getContentPage()` (used by everything else) — `web_search` can now run
   any number of times without ever disturbing what `read_page`/`click`/
   `fill` are looking at.
2. **A second, independent layer of defense against looping in general:**
   `tools.ts`'s `buildDaimonTools` gained a per-turn repetition guard —
   `open_url`/`web_search`/`click`/`fill_field` track a `Map` of
   `tool:JSON(args)` seen so far this turn; an exact repeat short-circuits
   the real action (no wasted network request) and returns a direct
   correction as the tool's result instead ("you've already tried this
   exact thing, try something different or report what you've found"),
   fed back through the same channel the model already reads results from.
   `read_page` (which takes no arguments, so a plain args-based check can't
   tell "same static page" from "changed after a click") instead compares
   the actual `(url, text)` content against the last read, so a legitimate
   re-read after a real page change is never blocked.
3. **The formatting complaint's root cause wasn't the model's writing at
   all — it was the display.** `PipelinePanel.tsx` rendered `turn.result`/
   `turn.error` in a plain `<p>` with no markdown renderer and no
   `whitespace-pre-wrap`, so any real newlines the model wrote collapsed
   into one run-on line, and any markdown syntax (`#`, `**bold**`, bullets)
   showed up as literal stray characters instead of formatting — the model
   wasn't writing badly, the UI was discarding its structure. Fixed with
   `whitespace-pre-wrap` on both elements, plus a new explicit section in
   `graph.ts`'s `SYSTEM_PROMPT` telling the model its result renders in a
   small chat bubble, not a document: plain text only (no markdown, since
   nothing renders it), short and conversational, no restating the
   instruction back, no narrating tool use unless asked how something was
   done.

Verified: `cd agents && npm run typecheck` clean, `npm run build` (root)
clean. Not yet confirmed by an actual live task exercising the fixed loop or
judging the new response style firsthand — that needs a human running a
real multi-step browsing task, same honesty caveat as everywhere else in
this file.

**The loop persisted after the page-split fix above, tried again by the
user — the exact-argument repeat guard from that first pass turned out too
weak on its own.** Researched rather than guessed at a second attempt:
searched for how ReAct-style tool-use loops are conventionally kept from
stagnating (Anthropic's own "Building Effective Agents" essay, plus
several independent write-ups on detecting/breaking stuck agent loops).
Confirmed the specific gap: exact `(tool, args)` hashing — what the first
pass implemented — is the documented *baseline*, but a model that's
actually stuck rarely repeats byte-identical arguments; it rephrases the
query slightly each time, which produces a "new" key every single time and
slips straight past that check without ever triggering it. The consistently
recommended fix on top of exact-match hashing is (1) a **hard, enforced
budget** on the whole category of repetitive actions that actually
*refuses* to keep executing past a cap, not just a prompt-level nudge, and
(2) catching **near**-duplicate attempts via similarity, not just string
equality.

Implemented both in `agents/src/tools.ts`, still scoped to the specific
tools implicated (`web_search`/`open_url`/`read_page`):
- `RESEARCH_TOOL_BUDGET = 10`, a combined hard cap across all three —
  once exceeded, every further call to any of them stops doing real work
  entirely (no network request, no page load) and only ever returns the
  same forceful stop instruction. This is enforced at the tool
  implementation level, not left to the model's own judgment to notice
  it's stuck (which, per the reported loop, it demonstrably didn't).
- A plain lowercase word-set Jaccard-similarity check for `web_search`
  specifically (`wordSet`/`jaccardSimilarity` in `tools.ts`) — any new
  query ≥60% word-overlap with one already tried this turn is treated as
  the same underlying attempt and short-circuited with a correction,
  rather than being allowed through just because the exact string differs.

`graph.ts`'s `SYSTEM_PROMPT` gained one line making the hard budget's
existence explicit to the model, so hitting it reads as an expected
constraint ("use these deliberately") rather than a confusing dead end.

Separately, addressed a real UI complaint from the same loop being visible
live: the "Thinking..." loading indicator is emitted *first*,
chronologically, at the very start of a turn (see `run.ts`) — so it always
lands at array index 0 of `turn.steps`, with every real tool-call step
appended *after* it. Rendered in plain array order, that pinned the
loading indicator at the *top* of a list that kept growing underneath it,
while auto-scroll kept the viewport pinned to the newest (bottom) step —
so the one thing telling the user it was still working scrolled out of
view after a couple of tool calls, which is confusing regardless of
whether the agent is actually looping or just doing legitimate
multi-step work. Fixed in `PipelinePanel.tsx` by splitting `turn.steps`
into the "Thinking" pseudo-step and everything else, and rendering the
former *after* the tool-step list rather than wherever it happens to sit
in array order — so it now stays anchored at the bottom, exactly where
the eventual result will appear, no matter how many tool steps pile up
above it.

Verified: `cd agents && npm run typecheck` clean, `npm run build` (root)
clean. Both the strengthened loop guard and the UI reposition are backed
by a concrete, reasoned diagnosis (not a shot in the dark) — but neither
is proven by live use yet. This is also very unlikely to be the *last*
word on agent loop-avoidance: if a real task still gets stuck within the
new 10-call research budget, that's a sign the underlying task genuinely
needs a different strategy (e.g. an explicit up-front plan/checklist
before the tool loop starts, closer to how coding-agent harnesses
structure multi-step work) rather than a tighter numeric cap — worth
revisiting with that framing specifically if the hard budget alone isn't
enough.

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
What's still outstanding here is subagent delegation.

**Voice input — decided and landing out of order, partial slice:**
requested directly, using an open-source engine rather than Deepgram —
local **whisper.cpp** via the `whisper-rs` Rust bindings, fully on-device,
no API key/account. Two things were explicitly deferred rather than
attempted in one pass:
- **The trigger key.** The user's actual ask was the bare Fn key
  (matching real Wispr Flow), but that isn't something any existing
  crate/plugin exposes — even a specialized macOS low-level key-intercept
  plugin explicitly filters the Fn modifier flag out, since it's a
  hardware-level signal, not a normal hotkey. Shipped in two steps rather
  than attempted all at once: first a standard hotkey
  (`CommandOrControl+Shift+D`) through the same
  `@tauri-apps/plugin-global-shortcut` mechanism `useHotkeyToggle` already
  uses (toggle-to-record/toggle-to-stop, not push-to-talk — that
  distinction can't be made with a plugin that only sees key-down, not
  hold-vs-release), to prove out the recording/transcription pipeline
  itself first. **Now also implemented:** the real bare-Fn-key trigger,
  additive alongside the standard hotkey (not a replacement) — a
  macOS-only native `NSEvent` global monitor (`src-tauri/src/fn_key.rs`,
  via the `objc2`/`objc2-app-kit`/`block2` crates) watching `.flagsChanged`
  events for a rising edge on the `Function` modifier bit, wired to the
  exact same `toggle_dictation_internal` function the standard hotkey's
  IPC command calls — one shared implementation, not two parallel ones.
  Requires macOS Accessibility permission to receive any events at all;
  status is surfaced in Settings (with a deep link to the right System
  Settings pane) rather than the daemon silently doing nothing forever if
  it's ungranted, the same "don't repeat the diagnosability mistake"
  lesson the standard-hotkey path had to learn the hard way.
- **Where dictation lands.** Real Wispr Flow injects transcribed text
  into whatever app currently has focus, system-wide. Decided against
  that for now: it needs simulating keystrokes into other applications,
  which cuts directly against `ARCHITECTURE.md` §5's "never steals
  keyboard focus" invariant, and is a materially larger undertaking than
  what was actually asked for here. Dictation instead fills Daimon's own
  chat input (expanding the panel if it's collapsed) — the user still
  reviews/edits before sending, same as typing.

**Status: implemented** (subagent delegation is still the one item left
outstanding in this phase). New `src-tauri/src/voice.rs`:
`get_voice_model_status`/`download_voice_model` (streams `ggml-base.en.bin`,
~148MB, from the public whisper.cpp HF mirror to a `.part` file, renamed
into place only once complete — a crash mid-download can't leave a
truncated file that would fail confusingly deep inside whisper.cpp later)
and `toggle_dictation` (idle → spins up a dedicated OS thread owning the
`cpal` microphone stream for its whole life, since `cpal::Stream` isn't
reliably `Send`-safe to hand across threads on every backend; recording →
stops that thread, resamples the captured buffer to 16kHz mono via linear
interpolation, and runs `whisper-rs` inference via `spawn_blocking`).
`WhisperContext` is loaded once and cached, but deliberately not touched
until a model file is confirmed present. A real hazard surfaced and got
fixed during testing: an ACL-reachability test that didn't guard against
the model file already existing on disk would have let `toggle_dictation`
fall through into actually opening the live microphone during an
unattended `cargo test` run — exactly the kind of thing
`ARCHITECTURE.md` §5 exists to prevent. Fixed with a test-only guard that
stashes/restores the model file around that specific test.

Frontend: `useDictationHotkey.ts` mirrors `useHotkeyToggle.ts`'s
registration pattern for `CommandOrControl+Shift+D`; a result fills the
chat input via a `pendingDraft` prop into `PipelinePanel` (forcing the
`chat` tab active if another tab was showing) rather than lifting `draft`
state up to `App.tsx`; the pill shows a distinct recording/transcribing
indicator so dictation stays visible even fully collapsed; Settings
gained a fourth section for the one-time model download.

Real microphone capture and real speech transcription accuracy were not
verified end-to-end (no live mic input available in the environment that
built this) — what was verified: a real HTTPS download landing the actual
model file, `WhisperContext` loading it successfully, and inference
running to completion on a real 16kHz mono synthetic buffer without
crashing. Worth actually trying the hotkey for real before considering
this fully proven.

The Fn-key hook has the same category of gap: `accessibility_trusted()`
was confirmed to genuinely round-trip through real FFI (not just avoid
crashing), and the crate API shapes (`objc2-app-kit`'s `NSEvent`/
`NSEventMask`/`NSEventModifierFlags`) matched what was researched with no
adjustment needed. **Since confirmed working for real by the user** —
the global monitor does fire on a real physical Fn key press.

Built on top of that, once the simple rising-edge toggle was confirmed
working: real gesture semantics specific to Fn (the standard hotkey keeps
its simple toggle, unchanged) —
- **Hold-to-talk:** press and hold → records while held; release → stops
  and transcribes immediately.
- **Double-tap-to-lock:** two quick taps → recording continues hands-free
  (shown as a distinct two-dot indicator next to the sound-wave visual,
  both on the pill and the panel header) until a single subsequent press
  stops it and transcribes.
- **Haptic feedback** via `NSHapticFeedbackManager` on a Force Touch
  trackpad — three distinct patterns (`Generic`/`LevelChange`/`Alignment`)
  for recording-starts / recording-stops / lock-engages, so the two
  transitions with no other physical confirmation (especially lock
  engaging, where there's no held key anymore) get one anyway.

The gesture disambiguation (`src-tauri/src/fn_key.rs`'s `GesturePhase`
state machine — tap vs. hold vs. double-tap, with a generation-counter
guard against a stale timer acting on a superseded gesture) is
deliberately kept as pure, synchronously-testable logic separate from the
real `NSEvent`/timer plumbing, and has real unit-test coverage of the
transition table including the race the state machine's own doc comment
calls out (a press landing after the double-tap window has technically
elapsed but before its timeout fired). What still needs a human with a
real trackpad: whether the three haptic patterns actually feel
distinguishable, and whether the ~280ms/~400ms timing constants feel
right in practice (both are plain constants, no settings surface, easy to
retune if they don't).

**Reliability fix, found and fixed after real usage:** the Fn-key trigger
was inconsistent, and specifically couldn't stop a double-tap-locked
recording. Root cause: `NSEvent`'s **global** monitor only receives events
posted to *other* applications, by Apple's own documented behavior — it
never fires for events directed at Daimon's own window. Since the panel
auto-expands (and can grab focus) the moment recording starts, a Fn press
meant to stop an already-running recording would often land on Daimon's
own now-focused window and never reach a global-only listener. Fixed by
also installing a **local** monitor for the same event mask, sharing one
continuous edge-detection/gesture state with the global monitor (not two
independent copies) — see `fn_key.rs`'s `handle_flags_changed_event`,
called by both installers, so a gesture that starts while another app has
focus and ends after Daimon's own window gained it (a completely normal
sequence given the auto-expand behavior) still resolves correctly. Also
fixed: a blank/silent transcription (silence, a very brief accidental
trigger) no longer surfaces as a usable dictation result on either side —
`voice.rs` won't emit a `"result"` event for empty/whitespace-only text,
and the frontend independently won't let an empty `pendingDraft` clobber
whatever was already typed. That emptiness check alone turned out to be
insufficient in practice, though: whisper.cpp doesn't reliably return an
empty string for silence, it's known to hallucinate filler text ("Thank
you.") or emit bracketed non-speech annotations it was trained to use for
exactly this (`[BLANK_AUDIO]`, `(silence)`) — neither of which is caught
by a plain `trim().is_empty()` check. Fixed properly by using whisper.cpp's
own per-segment no-speech-probability signal (`transcribe()` now returns a
`TranscriptionResult` with per-segment `no_speech_probability`, not a bare
`String`) as the primary gate — notably, whisper.cpp computes this
probability but doesn't act on it internally (its own `no_speech_thold`
parameter is documented as unimplemented as of the version in use), so the
filtering has to happen at the call site. A bracket-pattern check
(rejecting a transcript that's *nothing but* one `[...]`/`(...)`-wrapped
annotation) runs alongside it as defense-in-depth, for a hallucinated
annotation that happens to score a lower probability than the threshold
(`0.6`, whisper.cpp's own documented default). Every segment's probability
is now logged on each transcription, specifically so the threshold can be
tuned against real usage later if it turns out to be too strict/loose.

**Other UX fixes/additions from the same round:** the chat input became
an auto-growing textarea (Enter sends, Shift+Enter for a newline) — it
was a single-line `<input>` that silently clipped long dictated text off
the visible edge, which read as "it didn't paste in" when it actually
had, just invisibly. Synthesized sound effects (Web Audio API, no asset
files) now play at the same three moments that already get distinct
haptics — recording starts, recording stops/transcription begins, and
double-tap lock engages — since haptics only reach machines with a Force
Touch trackpad and sound reaches everyone. Also fixed: the auto-grow
textarea's height calculation only re-ran when the *text* changed, but
the panel's own expand animation resizes the real native window (and
therefore the textarea's width) gradually over ~220ms, independently of
React's render cycle — measuring `scrollHeight` while that animation was
still mid-flight (very reproducible specifically via the Fn-key
auto-expand path) locked in an inflated height at a stale, too-narrow
width with nothing to correct it afterward. Fixed with a `ResizeObserver`
on the textarea itself, recalculating whenever its actual rendered width
changes for any reason, not just when its content does. The dictation
result filling the input now also grabs focus and places the cursor at
the end, so Enter sends immediately without an extra click first — the
whole point of dictating is not touching the keyboard/mouse afterward.

**Known, deliberately deferred edge case: the macOS Character Viewer.**
Tapping the bare Fn key also opens macOS's own Character Viewer/emoji
picker by default — a system-level shortcut, not something Daimon's
*observation-only* `NSEvent` monitor can suppress (a monitor watches
events, it can't consume them the way an event tap could). Current fix:
disable it manually (System Settings → Keyboard → "Press 🌐 key to" → "Do
Nothing"), documented right in Daimon's own Settings next to the Fn-key
status. **Worth revisiting later, though, based on how Wispr Flow's own
docs describe their defaults:** their push-to-talk trigger is also the
bare Fn key, but their *hands-free* toggle (the equivalent of this
project's double-tap-to-lock) defaults to **Fn+Space**, not a double-tap
of Fn alone. That's a real clue about *why* their setup doesn't need to
fight this conflict: macOS's Character-Viewer shortcut fires on a quick,
standalone Fn tap specifically — a genuine hold-to-talk press likely
clears whatever duration threshold the OS itself uses to distinguish "a
tap" from "a hold," so push-to-talk alone may not trigger it much in
practice, but this project's double-tap-to-lock gesture is *built* from
two quick taps — exactly the input shape the OS shortcut also responds
to. Switching the lock gesture from double-tap to a chord (e.g. Fn+Space,
matching Wispr) would likely sidestep the conflict architecturally,
without needing a CGEventTap rewrite or asking the user to disable a
system feature. Not attempted in this pass — noted here for whenever
this gets revisited, not urgent enough to interrupt other work for.

**A second, unrelated reliability bug found right after the above, which
took three attempts to actually fix:** after a dictated result filled the
chat input, the very next keystroke — Enter, to send immediately — often
didn't reach Daimon at all, even though the input visually looked
focused. First attempt: awaited the window-level `setFocus()` properly
(fixing a real fire-and-forget race against a same-frame
`requestAnimationFrame`) — insufficient. Second attempt: added an
explicit `-[NSApplication activate]` call
(`src-tauri/src/window_focus.rs`'s `activate_and_focus_window` command),
on the theory that `tauri::ActivationPolicy::Accessory` (no Dock
icon/Cmd+Tab entry) apps can have their *window* become nominally key
without the *application* itself ever becoming genuinely active —
insufficient too, and confirmed why after actually reading tao's source:
tao's own `Window::set_focus()` already calls `activateIgnoringOtherApps:`
internally (`tao-0.35.3/src/platform_impl/macos/util/async.rs`), so
app-level activation was never the missing piece. Third attempt, and the
one that's actually right: found by reading `tauri-runtime-wry`'s
dispatcher directly rather than guessing again —
`WebviewWindow::set_focus()` only sends `WindowMessage::SetFocus`
(`makeKeyAndOrderFront:`); it never sends the *separate*
`WebviewMessage::SetFocus`, which is the one that calls
`window.makeFirstResponder(&webview)` (see `wry::WebView::focus()`). A
key window's first responder — not anything DOM-level — is what AppKit's
event dispatch uses to route real keystrokes; without it, `el.focus()` in
JS only ever set `document.activeElement` inside a webview the OS wasn't
actually listening to yet, which looks identical to "focus isn't working"
no matter how correct the window/app-activation half is. Fixed by also
calling the webview's own `set_focus()` — reachable via
`WebviewWindow`'s `AsRef<Webview<R>>` impl, a genuinely different call
from `Window`'s despite the identical method name
(`focus_window_and_webview` in `window_focus.rs`) — alongside the
existing window-level call, used both by the dictation-focus path and by
`expandToPanel()` (which had the same latent gap, just unreported since a
real mouse click on the pill — the normal way to expand — already carries
its own OS focus semantics that a purely programmatic call doesn't). Same
honesty caveat as the Fn-key work itself: real keyboard-focus routing
can't be proven by an automated test, only by a human actually trying it
against a genuinely-foregrounded other app.

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

**Reliability bug found after real usage: turns that occasionally finished
with no visible result at all** — the "Thinking" step would resolve but no
answer ever appeared, indistinguishable from the agent silently doing
nothing. Root cause, found by actually reading `@langchain/anthropic`'s
message types rather than guessing: `AIMessage.content` isn't always a
plain string — LangChain's own type for it is `string | Array<ContentBlock>`,
and the Anthropic integration only collapses a response down to a plain
string when it's a *single* text block. Any assistant turn that mixes a
text block with something else in the same response (Claude fairly often
narrates a tool call — "Let me check that page." — alongside the actual
`tool_use` block) comes back as an array instead, and `runTurn`'s old
`typeof message.content === "string"` check silently discarded that text
rather than capturing it as `finalResult`. Usually invisible (a *later*
plain-string message in the same turn would overwrite it), but a real,
reproducible "nothing comes back" whenever the model's actual final,
tool-call-free closing message happened to be one of these arrays instead
of collapsing to a string. Fixed with `extractText()` in `agents/src/run.ts`,
which handles both shapes (concatenating any `type: "text"` blocks out of
an array). Also hardened the empty-result fallback itself: it was
`finalResult || "Task complete."`, a plain truthiness check — a
whitespace-only `finalResult` (e.g. a closing message that's just a stray
newline) is truthy in JS, so it would have sailed past the fallback and
rendered as a turn that looks completely blank, which is exactly the
reported symptom too. Now `finalResult.trim() || "Task complete."`.

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

## 12. PHASE 11 — Claude Code Terminal Integration ✅ BUILT (needs more live use to call fully verified)

A way to drive **Claude Code** itself from inside Daimon — a real embedded
terminal, its own tab alongside chat/vault/automations/settings, with
voice dictation able to type into it the same way it fills the chat input
today. Originally scoped as planning-only; the "where does Claude Code
run" open question below was then resolved directly by the user, and the
phase was built.

This is a different, more concrete idea than the "Open question" already
sitting in `ARCHITECTURE.md` §3C about whether Claude Code/the Claude
Agent SDK could *replace* (or run alongside) the LangGraph orchestrator —
that question is about Daimon's own agent's planning engine. This phase
is narrower and additive: a literal terminal surface, not a change to how
Daimon's existing sessions/agent work.

**Resolved: directly on the host**, not inside a sandboxed workspace
container — explicit user decision, made specifically because the whole
point is running the user's *actual* Claude Code CLI against their
*actual* project files exactly as if they'd opened Terminal.app. This is
the first exception to `ARCHITECTURE.md` §5's "every execution surface is
an isolated Docker workspace" invariant, called out explicitly rather than
silently — `src-tauri/src/terminal.rs`'s own module doc comment documents
why this specific feature is incompatible with that model while the
non-disruption invariants themselves (never move the cursor, never steal
focus, never foreground another app) still hold.

**Status: implemented.** `src-tauri/src/terminal.rs` — a single global PTY
instance (one tab, not one-per-session) via the `portable-pty` crate,
spawning the user's own login shell (`$SHELL`, falling back per-platform)
rather than the `claude` binary directly, so the tab behaves like a normal
terminal (the user can `cd` around, run other commands, and launch
`claude` themselves — Claude Code's own REPL isn't a shell and has no `cd`
of its own). Output streams to the frontend base64-encoded over
`terminal-output` (base64 specifically so a multi-byte UTF-8 sequence
split across two PTY reads doesn't get corrupted by a lossy per-chunk
string conversion — decoded back to a `Uint8Array` on the frontend and fed
straight to xterm.js, which has its own streaming parser built exactly for
this); a `terminal-exited` event fires when the shell process ends, with a
restart affordance in the UI. `write_to_terminal`/`resize_terminal` round
out the contract; `get_claude_cli_status` is a plain non-invasive `claude
--version` probe surfaced in Settings as setup guidance (not a gate — the
tab always opens regardless). Frontend: `TerminalPanel.tsx` (`@xterm/xterm`
+ `@xterm/addon-fit`, themed to the app's one accent color), added as a
5th tab in `PipelinePanel.tsx`, kept mounted-but-hidden once opened rather
than torn down on tab-switch (preserves scrollback). Voice dictation
routes into the terminal instead of the chat draft when that tab is
active, and no longer force-switches away from it when a recording starts.

**Reliability fix, found after real usage: interactive elements (Claude
Code's own slash-command menu among them) sometimes silently not
rendering.** Root cause: `portable_pty::CommandBuilder`'s base environment
is just whatever Daimon's own process happened to inherit (see the crate's
`get_base_env`) — fine when launched from a dev shell that already has a
sane environment, but a real double-click-launched app bundle gets
launchd's bare environment instead, which has no `TERM` at all (no
controlling terminal) and may be missing `LANG`/PATH entries a login shell
would normally set up. An Ink/React-based TUI (which Claude Code's
slash-command interface is) checks `TERM` to decide what it can safely
draw — unset or `dumb` makes it fall back to a degraded, non-interactive
render, which looks exactly like "some things just don't show up" rather
than an obvious error. Fixed in `start_terminal` (`terminal.rs`): spawns
the shell with `-l` (login shell, so `.zprofile`/`.zlogin` — where PATH
additions like Homebrew's shellenv often live — get sourced, not just
`.zshrc`), and explicitly sets `TERM=xterm-256color` and
`COLORTERM=truecolor` unconditionally (this is genuinely, always correct
here, since xterm.js — the emulator on the other end of the pty — *is*
xterm-256color-compatible), plus `LANG=en_US.UTF-8` as a fallback only if
completely unset (never overriding a real, deliberately-configured
locale).

**Second reliability fix, same session: typed characters not visibly
appearing, plus the interactive box UI (input border, slash-command list,
mode indicator, spinner) not rendering at all.** Two real, distinct gaps
found in `TerminalPanel.tsx`, both a consequence of the same
mounted-but-hidden-via-`display:none` design (§ above, chosen to preserve
scrollback across tab switches):
1. xterm.js's own hidden `<textarea>` (the actual DOM element that
   receives keystrokes — the same underlying mechanism as the chat input's
   own focus bug this app already hit and fixed once, see the
   `window_focus.rs` entry above) never got real DOM focus just from the
   tab becoming active — nothing calls `.focus()` on it automatically the
   way the chat textarea's `autoFocus` does for itself. Typing right after
   switching to the tab could silently go nowhere.
2. A `display:none` container measures as zero-size, so whatever
   `fitAddon.fit()` last computed (at mount, or before the tab was hidden)
   is stale the moment it's shown again — meaning the shell, and anything
   running inside it, could easily still be working from the original
   80x24 placeholder size rather than the panel's real dimensions. A TUI
   that sizes a bordered input box, an autocomplete dropdown, or a status
   line relative to the terminal's reported width/height will misrender or
   simply not draw those elements if that reported size is wrong.

Investigated by inspecting the installed `claude` CLI binary directly
(`strings` on the compiled executable) rather than guessing — confirmed
it explicitly checks `TERM` (already fixed above) and does its own
environment/terminal-identification work, and its interactive UI is a
custom double-buffered renderer (not a stock Ink app), which makes
accurate, live terminal-size reporting specifically load-bearing for it in
a way a plainer TUI might tolerate being wrong about. Fixed by adding an
`active` prop threaded from `PipelinePanel.tsx` (`view === "terminal"`):
an effect keyed on it re-runs `fitAddon.fit()` + `resizeTerminal()` +
`term.focus()` every time the tab actually becomes the active one, not
just once at mount.

That fix (plus a follow-up adding `activateAndFocusWindow()` before
`term.focus()`, and temporary `console.debug` logging on both ends of the
data path) didn't resolve it — the user's follow-up narrowed the symptom
precisely: **basic shell use is completely fine (typing echoes normally,
commands run); only Claude Code's own interface, once launched, is
missing its box UI, slash-menu, and mode indicator.** That single detail
ruled out the focus/sizing theory (which would have affected the shell
too) and pointed at something specific to how Claude Code decides what it
can render.

**Third investigation, and the real root cause:** rather than guess
again, searched the `claude` binary's strings for its terminal
capability-negotiation logic and found it parses responses for
`kittyKeyboard`, `da1`/`da2` (device attributes), and `cursorPosition`
queries — i.e. it actively probes the terminal's capabilities at startup,
including the [Kitty keyboard protocol][kitty]. Confirmed via a web search
that **xterm.js 6.0.0 (the version this project had installed) doesn't
implement the Kitty keyboard protocol at all** — support only merged
upstream in January 2026 and, as of this writing, only exists in 6.1.0
pre-release betas, not yet a stable release. Verified directly (not
assumed) by downloading and inspecting the `6.1.0-beta.291` tarball for
the relevant code before committing to the upgrade. Claude Code's
custom-renderer UI evidently requires that protocol to safely enable its
richer interactive elements — without a response to its capability query,
it reasonably falls back to a plain-text mode, while ordinary character
I/O (which never depended on that query) works completely normally. This
is a genuine capability gap in the terminal emulation library itself, not
a bug in this project's own integration code.

**Attempted fix, then rolled back after it made things worse:** pinned
`@xterm/xterm` to the exact pre-release `6.1.0-beta.291` and enabled
`vtExtensions: { kittyKeyboard: true }` in the `Terminal` constructor.
After a full app restart to actually pick up the change (a first test
without restarting wouldn't have — the `Terminal` instance is created
inside a mount-once effect, deliberately not re-created on every tab
switch, so a stale still-running session would never have picked up a
constructor-option change without a genuine remount), the terminal tab
went from "basic shell works, Claude Code's rich UI doesn't" to
**rendering nothing at all** — a real regression, and a concrete
illustration of the exact risk that was flagged when choosing to use a
pre-release dependency in the first place. Reverted immediately rather
than continuing to push on it: `@xterm/xterm` back to `^6.0.0` (stable),
`vtExtensions` option removed. Confirmed via `npm run build` producing
the identical output bundle hash as before the beta was ever introduced —
a genuine full revert, not a partial one.

**Net state as of this rollback:** back to the working-but-incomplete
baseline (shell I/O fully functional, Claude Code's box UI/slash-menu/mode
indicator/spinner still not rendering). The Kitty-keyboard-protocol
theory is still the best lead found so far (grounded in actually reading
the `claude` binary's own capability-negotiation code, not a guess), but
it is **not confirmed as the fix** — the beta that would prove or disprove
it has its own apparent rendering bug that needs to be understood (or a
different xterm.js beta/build tried) before attempting this again. Not
re-attempted in this pass. Whoever picks this up next should not just
re-apply the same beta pin — dig into *why* that specific beta renders a
blank terminal first (check its GitHub issues/milestone for known
regressions, or bisect against a different beta build) rather than
re-triggering the same regression.

Not yet independently confirmed by extended live use beyond the reported
bugs and the fixes/rollback above — typing, resizing, and running real
Claude Code sessions inside the tab is the kind of thing that needs a
human actually doing it, same honesty caveat as every other
native/interactive feature in this file.

Separately, with the input-box rendering issue still unresolved but the
terminal otherwise confirmed working end-to-end (conversation history and
Claude Code's own logo/icon do render, per direct user report), the user
asked for **multiple concurrent terminal tabs** — widened from the
original single-global-instance design. `terminal.rs`'s state went from a
`Mutex<Option<ActiveTerminal>>` singleton to a `Mutex<HashMap<String,
ActiveTerminal>>` keyed by a caller-supplied `id` (minted by the frontend
per open tab, same pattern chat session ids already use); all four
existing commands gained an `id: String` parameter, plus a genuinely new
one, `close_terminal(id)`, since ending one tab now needs somewhere to go
besides "wait for the whole app to quit" — `kill_terminal_on_exit` still
sweeps every remaining entry on app exit. `terminal-output`/`terminal-exited`
events both gained an `id` field so the frontend's single shared
subscription (mirroring `onSessionStatus`) can route each event to the
right xterm instance. Frontend: `TerminalPanel` takes an `id` prop and
filters incoming events to its own id; `PipelinePanel.tsx` now tracks
`terminalTabs: string[]` + `activeTerminalId` instead of a single
`hasOpenedTerminal` boolean, renders one `TerminalPanel` per open tab
(each kept mounted-but-hidden once created, same scrollback-preservation
reasoning as before, just per-tab now), and grew a terminal-tab chip row
mirroring the existing chat-session chip row (a chip per tab with a "×",
plus a "+" to open another) — shown only while the terminal view itself
is active. Dictation routing targets whichever terminal tab is currently
`activeTerminalId`, not just "the terminal view" broadly.

**Before retrying the xterm.js beta a second time, researched whether it
was actually safe to — it wasn't.** Found [xtermjs/xterm.js#5894][5894], a
bug reported independently (not by this project) specifically in "macOS
WKWebView (Tauri 2)" — this app's exact runtime — in the same 6.1.0-beta
line the Kitty protocol fix requires. That's real evidence this beta line
has genuine compatibility problems in exactly this environment, not just
bad luck from the one earlier attempt. Decision (user's call, given the
tradeoff): don't re-touch the xterm.js version; instead attempt a
narrower, version-risk-free workaround.

**Workaround implemented in `TerminalPanel.tsx` (frontend-only, no
xterm.js version change):** rather than waiting for xterm.js to itself
answer Claude Code's Kitty-protocol capability query, Daimon now answers
it directly. Every chunk of real PTY output is scanned (as a plain ASCII
string view of the same bytes already being fed to `term.write()`
unmodified) for the literal query sequence `CSI ? u` (`\x1b[?u`) — the
standard [Kitty keyboard protocol][kitty] capability probe. If seen, a
standards-compliant reply, `CSI ? 0 u` (`\x1b[?0u` — "yes, understood; no
enhancements currently active", matching the shape `claude`'s own response
parser expects, confirmed via the same `strings`-based investigation as
before: `/^\x1b\[\?(\d+)u$/`), is written straight back into that
terminal's PTY via the existing `writeToTerminal`, exactly as if a real
Kitty-capable terminal had answered on its own. This is purely additive —
it only ever fires on that one specific, narrow byte pattern, so it can't
regress anything that worked before (unlike the version bump, there's no
plausible way for this to make things worse than the current baseline).

**Confirmed insufficient by live testing.** The synthesized Kitty-protocol
reply alone did not make Claude Code's input box/slash-menu/mode indicator
appear. The capability query it answers is real and the reply is
correctly formatted (both independently verified via the `claude` binary's
own `strings` output — not a guess), so either Claude Code's renderer
gates its richer UI behind more than just this one capability (DA1/DA2, or
something not yet identified), or there's a different reason entirely that
this specific investigation hasn't surfaced. Per explicit instruction,
**this is now documented as a known limitation rather than continuing to
chase it further**: the embedded terminal tab is fully usable for normal
shell work and for running `claude` itself (conversation history and
output render correctly), but Claude Code's interactive chrome (bordered
input box, slash-command autocomplete, mode indicator, loading spinner)
does not currently render inside it. The workaround code itself is left in
place (`TerminalPanel.tsx`'s Kitty-query interception) since it's
harmless/purely additive even though it didn't resolve the issue on its
own — no reason to revert something that isn't causing harm, per the
user's own "revert if it was better before" framing (nothing got worse).
Whoever revisits this next should treat both the xterm.js version question
(§ above — still blocked on upstream WKWebView/Tauri compatibility) and
this capability-negotiation gap as open, related questions, not assume
either one alone is the fix.

**The user then found the real explanation for the original "can't see
what I'm typing" report themselves, by testing more carefully:** it was
never a rendering/focus/protocol bug at all — the terminal's actual
content (including the input line) was extending below the visible
window bounds, so the bottom portion was simply never on-screen to begin
with. This also explained a second, related visual bug: terminal content
was spilling past the panel's rounded corners, showing square window
corners where round ones should be. Root cause: the outer panel container
(`PipelinePanel.tsx`'s `rounded-[28px] ... flex h-full w-full flex-col`
wrapper) had no `overflow-hidden` — nothing was clipping child content to
the rounded rectangle at all, so anything that grew past the container's
bounds (an untrimmed terminal, in particular) rendered straight through
into the transparent window margin around it instead of being cut off at
the visible edge. Fixed by adding `overflow-hidden` to that container.

Chasing the same investigation surfaced a second, latent problem: none of
the fixed-height rows in the panel (header, chat-session chip row,
terminal-tab chip row, chat input form) had `shrink-0` — under any
vertical space pressure, flexbox is free to compress a flex child below
its own padding/content size by default, which is exactly why earlier
attempts to fix "chip row is too cramped" by just increasing padding
numbers kept not visibly landing: the browser was allowed to squeeze
right back past whatever was specified. Fixed by adding `shrink-0` to
every one of those fixed rows and `min-h-0` to the flex-growing content
areas that should actually be the ones absorbing available space (chat
history, the active terminal panel) — the correct, durable version of
what several prior padding-only attempts were trying to do.

**Separate, more serious bug, also just reported: collapsing the panel to
the pill widget destroyed running terminal tabs — the user watched a live
`claude` session end when they only meant to minimize.** Root cause: in
`App.tsx`, the whole `PipelinePanel` component fully **unmounts** whenever
`expanded` goes false (`{expanded ? <PipelinePanel/> : <Pill/>}`) — that's
been true since the very first pill/panel toggle was built, and chat
sessions were already designed around it correctly from the start
(`sessions`/`activeSessionId` live in `App.tsx`, which never unmounts, not
in `PipelinePanel`). Terminal tabs, added later in this same phase, were
never given the same treatment — `terminalTabs`/`activeTerminalId`/
`pendingTerminalInput` all lived as `PipelinePanel`-local `useState`, so
collapsing threw all of it away. The real host shell process almost
certainly kept running on the Rust side the whole time (nothing tears it
down on unmount — `TerminalPanel`'s cleanup effect disposes the xterm
instance and unsubscribes listeners, but never calls `closeTerminal`), but
the frontend lost every reference to that terminal's `id` the moment
`PipelinePanel` unmounted, so there was no way back to it — indistinguishable
from the process having actually been killed. Fixed by lifting
`terminalTabs`/`activeTerminalId`/`pendingTerminalInput` (plus
`openNewTerminalTab`/`closeTerminalTab`/`consumePendingTerminalInput`) out
of `PipelinePanel.tsx` and into `App.tsx`, passed down as props — the
exact same architecture chat sessions already used, just applied
consistently this time. Collapsing to the pill now genuinely just hides
the panel; every terminal tab's real shell process (and the frontend's
ability to reconnect to it) survives across any number of collapse/expand
cycles, exactly like an already-open chat session does.

**Two follow-up refinements to the terminal tab's visual sizing, both
frontend-only:** (1) the terminal's own root container in
`TerminalPanel.tsx` was missing `min-h-0` — as a flex child of a
`flex-col` parent, `flex-1` alone still defaults to `min-height: auto`,
meaning the container refused to shrink below its own content's natural
size even when the parent had less room to give it. The panel's earlier
`overflow-hidden` fix (added for the rounded-corner bleed-through bug just
above) had incidentally turned this from a *visible* overflow (content
spilling past the rounded corners) into an *invisible* one (content
clipped at the bottom, unnoticed until reported directly) — same
underlying sizing bug, just a different symptom depending on what else
was fixed at the time. Fixed by adding `min-h-0` there too. (2) The
terminal tab's chip row was, at the user's own request, first made more
generous (more padding) when it looked cramped, then brought back down to
match the chat-session chip row's exact sizing (`py-2` row / `py-1`
chips) once the actual cutoff bug above was understood to be a structural
sizing issue, not a spacing one — the row height itself was never really
the cause.

**Third fix, same root cause as the terminal-tabs-dying-on-collapse bug
above, just for a different piece of state:** which tab
(chat/vault/automations/terminal/settings) was showing also reset to
"chat" on every collapse/expand cycle, for the exact same reason —
`view` was a `PipelinePanel`-local `useState<View>("chat")`, and
`PipelinePanel` fully unmounts on collapse. Reported directly ("when I
collapse the terminal tab and reopen it, I want it to go back to the tab
I was on"). Fixed the same way: `View` (the tab-name union type) moved
from a `PipelinePanel`-local type alias into `types.ts` so both files can
share it, and `view`/`setView` moved into `App.tsx`, passed down as
`view`/`onViewChange` props — the same lift already applied to
`terminalTabs`/`activeTerminalId` earlier in this section. Collapsing to
the pill and reopening now always returns to whichever tab was actually
open, not just for the terminal tab specifically but for any of them.

[kitty]: https://sw.kovidgoyal.net/kitty/keyboard-protocol/
[5894]: https://github.com/xtermjs/xterm.js/issues/5894

**First real "agent controls Daimon's own app" capability, added deliberately
narrow.** Requested directly: "tell the agent to go into my terminal, go
into my Desktop directory, and open [Claude Code]... then go to that tab."
This is architecturally new — every existing agent tool (`open_url`,
`fill_field`, `web_search`, ...) only ever touches the agent's own
sandboxed Docker workspace; nothing before this could reach back out and
drive the host Tauri app's actual UI at all. Scoped deliberately via two
explicit decisions before building anything, matching how every other
open-ended phase in this project has been scoped:
- **Stage, don't execute.** The agent can open a terminal tab and type a
  command into it, but never presses Enter on the user's behalf — same
  "review before it's acted on" principle already used for voice
  dictation landing in an input without auto-submitting. A misheard or
  misinterpreted instruction can, at worst, type something wrong into a
  terminal for the user to notice and clear, never actually run it.
- **Terminal control only, for now** — not a general "agent can drive any
  part of Daimon's UI" tool. Prove this one narrow case first; extend the
  pattern to other UI surfaces (vault, automations, ...) later if it's
  actually useful, rather than over-designing a general mechanism up
  front.

**Mechanism (no new IPC surface needed — reused the existing session
event pipe end-to-end):** confirmed by reading `session.rs`'s
`run_and_stream` that the Rust daemon already forwards *any* event type
from the agent container to the frontend verbatim
(`emit(app, session_id, event)` fires unconditionally regardless of which
`match event.get("type")` arm logged it) — so a brand-new event type
requires zero Rust-side changes. Added a 4th `TaskEvent` variant,
`UiActionEvent` (`agents/src/events.ts`): `{ type: "ui_action", action:
"open_terminal_with_command", command: string }`. `agents/src/tools.ts`'s
`daimonTools` (previously a static array built once at module load, with
no access to any per-turn callback) became `buildDaimonTools(emit)`, a
factory called fresh each turn in `graph.ts`'s `buildAgent(memoryContext,
emit)` — needed because the new `open_terminal_with_command` tool has to
push this event as a side effect, not just return a string to the LLM
like every other tool. `SYSTEM_PROMPT` gained one clause clarifying the
tool's stage-only scope and telling the model to combine multi-step
requests into one `&&`-joined command line rather than calling it
repeatedly.

On the frontend, `WorkspaceEvent` (`src/lib/api.ts`) gained the matching
`ui_action` variant. `App.tsx`'s single `onSessionStatus` subscription
branches on it *before* ever touching session/turn state — mints a new
terminal tab id, adds it to `terminalTabs`, makes it active, stages the
command in a new `initialTerminalCommands: Record<string, string>` map
(keyed by tab id — deliberately separate from `pendingTerminalInput`,
which routes a *dictation* result into whichever tab is already active;
this is instead a one-shot "type this once a freshly-opened tab's shell
is actually ready" value), switches `view` to `"terminal"`, and calls
`expand()` in case the panel was collapsed when the instruction ran.
`sessionEvents.ts`'s `applyToTurn` got an explicit `ui_action` fallback
(returns the turn unchanged — it's not chat/turn content, just a trigger
for the side effect above).

The one real subtlety, worth flagging for future work in this area: a
genuine race condition between spawning the new tab's PTY and staging text
into it. `write_to_terminal` errors ("no active terminal — start one
first") if it lands before that tab's `start_terminal` has finished on the
Rust side — naively firing both as independent fire-and-forget calls
could silently drop the staged command depending on which IPC round trip
happened to resolve first. Fixed in `TerminalPanel.tsx` by genuinely
sequencing them (`await startTerminal(id)` before ever issuing
`writeToTerminal(id, initialCommand)`) inside the tab's mount effect,
rather than two parallel calls that merely usually work in the right
order.

Verified: `cd agents && npm run typecheck` clean, `npm run build` (root)
clean, `cargo check`/`cargo test` unaffected (47 passed, 1 pre-existing
ignored) — confirming the Rust side genuinely needed no changes, as
predicted from reading its forwarding logic up front rather than assuming.
Not yet confirmed by an actual live voice/chat instruction exercising the
full path end-to-end — that's the next thing this needs before calling it
done, same honesty standard as every other feature in this file.

---

## 13. OPERATIONAL DIRECTIVES & AUTONOMY GUIDELINES

- **Full Execution Authority:** Create files, install dependencies (`npm`, `cargo`, etc.), initialize git repositories, run build commands (`cargo check`, `tauri dev`, `npm run build`), read compiler/runtime errors, and fix them self-correctively.
- **Incremental Commits:** Commit at every functional milestone with clear, semantic messages (e.g. `feat(tauri): floating pill widget + hotkey toggle`, `feat(workspace): headless browser task execution in Docker`).
- **Production Quality:** Modular, typed, well-structured code. No placeholder pseudo-code — complete implementation logic and real error handling.
- **Build Validation:** Never mark a step complete until the project compiles and runs cleanly, and the phase's concrete "done when" criterion has been manually verified.
- **Stay in scope:** Do not pull work from a later phase into an earlier one, even if it seems easy — the phasing exists to keep each milestone independently verifiable.

---

## 14. IMMEDIATE EXECUTION STEPS (Phase 1)

1. Confirm `ARCHITECTURE.md` reflects current understanding; flag and resolve any conflicts before writing code.
2. Scaffold the project (Tauri v2 + Rust + React + TypeScript + Tailwind).
3. Implement the floating pill widget with hotkey toggle and expanded pipeline view.
4. Implement the Docker-backed background workspace manager (headless browser + shell) and its IPC bridge.
5. Wire a single LangGraph agent node to plan and execute one task end-to-end inside the workspace, streaming status to the widget.
6. Manually verify the Phase 1 "done when" criterion, then run the initial developer build.

Begin implementation now.
