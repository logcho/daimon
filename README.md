# Daimon

A Jarvis-style general-purpose assistant for your own computer — ambient, on-device, non-disruptive. Good at coding, web browsing, and research.

## Layout

| Folder | What | Status |
|---|---|---|
| `agents/` | The agent implementation — LangGraph harness (Python, open core) | Active |
| `app/` | The Tauri chat app (closed source) | In progress — chat only for now |
| `legacy/` | Archived previous implementation (Tauri pill app + Node agent server) | Frozen reference |

`agents/` is the open side of daimon's open-core split: the LangGraph orchestrator, the tool library, and the skill schema. It is a standalone Python project — `uv sync` + run tests from inside it.

## Quick start

### Agent harness (`agents/`)

```bash
cd agents
uv sync                          # creates .venv
uv sync --extra anthropic        # optional: adds the Anthropic provider
uv run pytest tests              # 413 pass (2 browser tests need a live PinchTab)
```

Configuration lives in `agents/.env` (copy `.env.example`). **API keys go there** — never in chat or in the repo. Running `uv run daimon` with no key configured starts a short setup: pick a provider, paste its key (masked, and never echoed into the transcript), choose your models from what that provider actually offers.

- `uv run daimon` — the interactive CLI
- `uv run daimon "…"` — one-shot turn (result on stdout, progress on stderr)
- `uv run daimon-agent` — HTTP server on `127.0.0.1:4711` (`GET /health`, `POST /task` → NDJSON event stream, `POST /resume` to answer a question)
- `scripts/setup-pinchtab.sh start` — PinchTab browser sidecar (optional, for browser tools)
- `scripts/optimize_skill.py` / `scripts/smoke_e2e.py` — skill optimizer and end-to-end smoke

DeepSeek is the default provider. Either model role takes a `provider:model` spec, so the main agent can run on a stronger model while sub-agents stay cheap — pick them in `/setup`, in the app's settings, or directly:

```bash
DAIMON_MODEL=anthropic:claude-sonnet-5 DAIMON_FLASH_MODEL=deepseek-chat uv run daimon
```

#### The CLI

A full-screen interface: the transcript scrolls in its own pane, in-flight work (running tools, sub-agents, the todo list) shows below it, and the prompt and status bar stay pinned to the bottom. The status bar carries tokens, cost, elapsed time, and context usage.

| Key | |
|---|---|
| `Enter` / `Alt+Enter` | submit / newline |
| `↑` `↓` | previous inputs |
| wheel, `PgUp`/`PgDn`, `Shift+↑↓` | scroll the transcript |
| `Ctrl-End` | jump to the newest output |
| `Esc` | cancel the running turn (the server cancels the work too) |
| `Ctrl-C` | cancel, or exit when idle |
| `/plan` | plan mode — the agent presents a plan and waits before changing anything |
| `/skills` | list the library; `/skills <name>` reads one, `/skills <query>` filters |
| `/skills find <topic>` | search ~15k published skills; `/skills install <slug>` adds one |
| `/setup` | guided setup — provider, key, models (runs itself on first launch) |
| `/config` | what's configured right now (read-only) |
| `/tools`, `/help` | |

Scrolling up to read something keeps you there while output arrives; submitting anything snaps back to the newest.

Long tasks run unattended. The graph's step cap is a loop guard, not a budget, so hitting it shows a `↻ continuing` line rather than stopping — `DAIMON_MAX_STEPS` is the real ceiling, and reaching it makes the agent ask whether to keep going.

#### Skills

Reusable procedures as `<name>/SKILL.md`, in two libraries: `vault/skills/` follows you between projects, and a repo's `.daimon/skills/` travels with the code and can be committed. A project skill shadows a vault one of the same name.

Loading is progressive, as in Claude Code: the prompt carries only each skill's name and one-line description, and the agent calls `read_skill` when one applies — so a library of fifty costs fifty lines, not fifty bodies. The agent writes them itself with `save_skill` when it works out something worth reusing. Browse them with `/skills`, or in the app's skills tab.

You can also install published ones. `/skills find <topic>` searches the [claudeskills.info](https://claudeskills.info) registry (~15k skills, including Anthropic's official set), and `/skills install <slug>` adds one. The agent can search too — it will suggest a skill rather than reinvent a procedure — but **installing is always yours**: a skill is instructions the agent will follow and often scripts it may run, so the confirmation shows the file list, flags executables, and names the licence before anything is written.

Installed skills are directories, not single files. Anthropic's `pdf` skill ships two reference documents and eight scripts; `read_skill` lists what's bundled and reads any of it.

When the agent asks a question, the options appear inline: pick with `1`-`9` or `↑↓`+`Enter`, `e` to answer in your own words, `Esc` to skip.

### Chat app (`app/`)

```bash
cd app
npm install
npm run tauri dev
```

The app spawns and supervises its own agent server (`uv run daimon-agent` with cwd `agents/`), so `agents/.env` must hold the API key. In dev the spawn relies on `uv` being on PATH; bundling Python into the app is future work.
