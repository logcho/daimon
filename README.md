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
uv run pytest tests              # 314 pass (2 browser tests need a live PinchTab)
```

Configuration lives in `agents/.env` (copy `.env.example`). **`DEEPSEEK_API_KEY` goes there** — never in chat or in the repo.

- `uv run daimon` — the interactive CLI
- `uv run daimon "…"` — one-shot turn (result on stdout, progress on stderr)
- `uv run daimon-agent` — HTTP server on `127.0.0.1:4711` (`GET /health`, `POST /task` → NDJSON event stream, `POST /resume` to answer a question)

DeepSeek is the default provider. Either model role takes a `provider:model` spec, so the main agent can run on a stronger model while sub-agents stay cheap:

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
| `/tools`, `/setup`, `/help` | |

Scrolling up to read something keeps you there while output arrives; submitting anything snaps back to the newest.

When the agent asks a question, the options appear inline: pick with `1`-`9` or `↑↓`+`Enter`, `e` to answer in your own words, `Esc` to skip.
- `scripts/setup-pinchtab.sh start` — PinchTab browser sidecar (optional, for browser tools)
- `scripts/optimize_skill.py` / `scripts/smoke_e2e.py` — skill optimizer and end-to-end smoke

### Chat app (`app/`)

```bash
cd app
npm install
npm run tauri dev
```

The app spawns and supervises its own agent server (`uv run daimon-agent` with cwd `agents/`), so `agents/.env` must hold the API key. In dev the spawn relies on `uv` being on PATH; bundling Python into the app is future work.
