"""The HTTP server — port of `legacy/agents/src/server.ts`.

A single aiohttp process the future Rust daemon spawns and supervises.
`/health` for liveness; `POST /task` streams NDJSON TaskEvents exactly as the
legacy contract (the daemon proxies the stream to the UI). One graph per
process with a per-session turn lock, startup vault-note indexing, and a
last-ditch error emit so nothing escapes as an unhandled rejection.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from . import live_frames
from .browser import aclose_browser, build_browser
from .config import Settings
from .events import error_event
from .graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from .memory import MemoryStore
from .model import ModelRouter
from .run import run_turn
from .skills.injector import discover_skills
from .tools import build_tools
from .tools.repl import close_all_repls

NDJSON = "application/x-ndjson"


def index_existing_vault_notes(settings: Settings, memory: MemoryStore) -> int:
    """Startup scan of whatever notes are already in the vault (most
    importantly ones the *user* wrote directly into a real Obsidian vault) so
    they're searchable from the very first task. Re-indexing is harmless —
    index_note is an upsert — so this is a plain full scan, like legacy."""
    vault = Path(settings.resolved_workspace_dir)
    if not vault.exists():
        return 0
    count = 0
    for path in vault.rglob("*.md"):
        try:
            memory.index_note(str(path.relative_to(vault)), path.read_text(encoding="utf-8", errors="replace"))
            count += 1
        except Exception as exc:  # one bad note must not stop the scan
            print(f"[daimon-agent] failed to index vault note {path}: {exc}", file=sys.stderr)
    return count


async def build_default_graph(settings: Settings, *, memory: MemoryStore | None = None) -> tuple[Any, Any]:
    """The process graph: real tools (shared browser/search/repl), the
    checkpointer, and the Pro-model router. Returns (graph, checkpointer);
    the caller closes the checkpointer at shutdown. `memory` is the app's one
    store — never open a second connection to the same DB."""
    router = ModelRouter(settings)
    tools = build_tools(settings, memory=memory, session_id="server")
    checkpointer = await make_sqlite_checkpointer(settings.checkpoints_db)
    graph = build_graph(settings, router, tools, checkpointer=checkpointer)
    return graph, checkpointer


async def create_app(
    settings: Settings | None = None,
    *,
    graph_builder: Any = build_default_graph,
    memory: MemoryStore | None = None,
) -> web.Application:
    settings = settings or Settings.from_env()
    app = web.Application()
    app["settings"] = settings
    app["graph"] = None
    app["checkpointer"] = None
    app["memory"] = memory or MemoryStore(settings.memory_db)
    app["locks"] = {}
    app["skills"] = []

    async def startup(app: web.Application) -> None:
        graph, checkpointer = await graph_builder(settings, memory=app["memory"])
        app["graph"] = graph
        app["checkpointer"] = checkpointer
        app["skills"] = discover_skills(settings.resolved_skills_dir)
        indexed = index_existing_vault_notes(settings, app["memory"])
        print(f"[daimon-agent] indexed {indexed} existing vault note(s) on startup", file=sys.stderr)
        print(f"[daimon-agent] listening on :{settings.port}", file=sys.stderr)

    async def cleanup(app: web.Application) -> None:
        await close_checkpointer(app["checkpointer"])
        await aclose_browser()
        await close_all_repls()
        app["memory"].close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def task(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return web.Response(status=400, text="instruction is required")
        session_id = str(body.get("session_id") or "default")
        print(f"[daimon-agent] received task: {instruction}", file=sys.stderr)

        response = web.StreamResponse(headers={"content-type": NDJSON})
        await response.prepare(request)

        # Events must be emitted from sync graph-node context in order, so the
        # emit closure enqueues and a single writer task drains the queue.
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        async def writer() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                await response.write(chunk)

        writer_task = asyncio.create_task(writer())

        def emit(event: dict) -> None:
            queue.put_nowait(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")

        def frame_task(emit_fn) -> asyncio.Task | None:
            return live_frames.start(emit_fn, build_browser(settings), settings)

        lock = app["locks"].setdefault(session_id, asyncio.Lock())
        async with lock:  # one turn per session at a time, like legacy's queue
            try:
                await run_turn(
                    instruction,
                    session_id,
                    settings,
                    emit,
                    graph=app["graph"],
                    memory=app["memory"],
                    skills=app["skills"],
                    live_frames=frame_task,
                )
            except Exception as exc:  # last-ditch backstop, port of server.ts's .catch
                print(f"[daimon-agent] unhandled error running task: {exc}", file=sys.stderr)
                try:
                    emit(error_event(str(exc)))
                except Exception:
                    pass  # response may already be closed
            finally:
                await queue.put(None)
                await writer_task
                await response.write_eof()
        return response

    app.router.add_get("/health", health)
    app.router.add_post("/task", task)
    return app


def main() -> None:
    settings = Settings.from_env()
    # run_app awaits the app coroutine inside its own loop, so startup hooks
    # (checkpointer, browser) bind to the running loop.
    web.run_app(create_app(settings), host="127.0.0.1", port=settings.port)
