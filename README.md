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
uv run pytest tests              # 110 pass (2 browser tests need a live PinchTab)
```

Configuration lives in `agents/.env` (copy `.env.example`). **`DEEPSEEK_API_KEY` goes there** — never in chat or in the repo.

- `uv run daimon-chat "…"` — one-shot CLI turn
- `uv run daimon-agent` — HTTP server on `127.0.0.1:4711` (`GET /health`, `POST /task` → NDJSON event stream)
- `scripts/setup-pinchtab.sh start` — PinchTab browser sidecar (optional, for browser tools)
- `scripts/optimize_skill.py` / `scripts/smoke_e2e.py` — skill optimizer and end-to-end smoke

### Chat app (`app/`)

```bash
cd app
npm install
npm run tauri dev
```

The app spawns and supervises its own agent server (`uv run daimon-agent` with cwd `agents/`), so `agents/.env` must hold the API key. In dev the spawn relies on `uv` being on PATH; bundling Python into the app is future work.
