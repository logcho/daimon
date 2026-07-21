# Daimon

An ambient, on-device AI co-worker. You hand it a task, it runs in the background on your own machine — inside an isolated Docker workspace, invisible to your screen and input — while you keep working on something else.

See `ARCHITECTURE.md` for the product vision and system design, and `PROMPT.md` for the phased build plan.

## Prerequisites

- [Node.js](https://nodejs.org/) 22+ and npm
- [Rust](https://www.rust-lang.org/tools/install) (stable toolchain)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running

## Setup

```sh
npm install
```

## Running the app

```sh
npm run tauri dev
```

This launches the floating pill widget. Press `Cmd+Shift+Space` (or `Ctrl+Shift+Space`) to expand it into the full pipeline view, type an instruction, and submit. The first task you run will build the background workspace's Docker image, which takes a minute or two; after that it's reused.

Without an API key (see below), tasks run a scripted demo instead of a real agent, so you can confirm the whole pipeline — Docker workspace, headless browser, live status streaming — works before wiring up a model.

## Giving Daimon a real brain: `ANTHROPIC_API_KEY`

The agent (`agents/src/graph.ts`) uses Claude via `@langchain/anthropic`. Without an API key, it falls back to a fixed demo task instead of actually reasoning about your instruction.

**Recommended: a `.env` file.** This is read directly by the app at startup, so it works no matter how you launch Daimon — no dependency on which terminal tab you happened to run a shell export in.

1. Get a key from the [Anthropic Console](https://console.anthropic.com/settings/keys).
2. In the project root:
   ```sh
   cp .env.example .env
   ```
3. Open `.env` in an editor and fill in `ANTHROPIC_API_KEY=`. This file is gitignored — it never gets committed.
4. Run `npm run tauri dev`. On startup, the terminal will print `[daimon] ANTHROPIC_API_KEY present: true` (or `false`) so you can confirm it loaded before doing anything else.

An already-exported shell environment variable always takes priority over `.env`, if you'd rather set it that way instead.

**Either way, this only matters at container creation time.** If you already ran Daimon before the key was in place, there's a stale container without it — remove it so the next task recreates it with the key present:

```sh
docker rm -f daimon-workspace-default
```

The key isn't baked into the Docker image or committed anywhere — it lives only in your local, gitignored `.env` and is passed into the container as a runtime environment variable, per the non-disruption/secrets guidelines in `ARCHITECTURE.md`.

## Verifying things manually

```sh
# frontend typecheck + build
npm run build

# rust build
cd src-tauri && cargo check

# rust tests, including an end-to-end IPC test against the live Docker workspace
cd src-tauri && cargo test

# agent server typecheck
cd agents && npm run typecheck
```

## Project layout

```text
/daimon
├── src-tauri/   # Rust backend: IPC handlers, workspace/container manager
├── src/         # React frontend: ambient pill UI + expanded pipeline view
├── agents/      # LangGraph agent server (runs inside the background workspace)
├── sandbox/     # Dockerfile for the background workspace image
├── assets/      # Brand source files (e.g. logo.png, used to regenerate src-tauri/icons)
├── ARCHITECTURE.md
└── PROMPT.md
```

`memory/` and `gateway/` (persistent memory/skills, remote chat access) are Phase 2/3 and don't exist yet — see `PROMPT.md`.

## Regenerating app icons

The app icon set in `src-tauri/icons/` is generated from `assets/logo.png`:

```sh
npx tauri icon assets/logo.png
```

Recommended IDE setup: [VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer).
