"""The HTTP server — port of `legacy/agents/src/server.ts`.

A single aiohttp process the future Rust daemon spawns and supervises.
`/health` for liveness; `POST /task` streams NDJSON TaskEvents exactly as the
legacy contract (the daemon proxies the stream to the UI). One graph per
process with a per-session turn lock, startup vault-note indexing, and a
last-ditch error emit so nothing escapes as an unhandled rejection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from dataclasses import replace
from dotenv import dotenv_values

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
from .tools.search import aclose_search_provider
from .tools.web import aclose_web_fetcher

NDJSON = "application/x-ndjson"
PINCHTAB_HEALTH_TIMEOUT_S = 5.0
PINCHTAB_SETUP_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "setup-pinchtab.sh"


async def _pinchtab_healthy(settings: Settings) -> bool:
    """True if PinchTab is reachable and responding on the configured base."""
    try:
        headers = {}
        if settings.pinchtab_token:
            headers["Authorization"] = f"Bearer {settings.pinchtab_token}"
        async with httpx.AsyncClient(timeout=PINCHTAB_HEALTH_TIMEOUT_S) as client:
            resp = await client.get(f"{settings.pinchtab_base}/health", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False


async def _ensure_pinchtab(settings: Settings) -> Settings:
    """Auto-start PinchTab if it isn't healthy, then return settings with
    updated pinchtab_base/token. Only touches PinchTab keys — caller-provided
    overrides (tmp paths, port, etc.) are preserved."""

    if await _pinchtab_healthy(settings):
        return settings

    script = str(PINCHTAB_SETUP_SCRIPT)
    if not Path(script).exists():
        print(f"[daimon-agent] PinchTab not running and {script} not found — "
              f"browser tools will be unavailable", file=sys.stderr)
        return settings

    print(f"[daimon-agent] PinchTab not healthy on {settings.pinchtab_base} — "
          f"auto-starting via {script}", file=sys.stderr)
    try:
        # Kill orphaned instances first so we don't accumulate them.
        await _stop_pinchtab()

        proc = await asyncio.create_subprocess_exec(
            script, "start",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=60.0,
        )
        if proc.returncode != 0:
            print(f"[daimon-agent] PinchTab setup failed (exit {proc.returncode}): "
                  f"{stderr.decode()}", file=sys.stderr)
            return settings

        # Only read PinchTab keys from the env file — don't reload everything
        # (that would lose caller-provided overrides like test tmp paths).
        pinchtab_env = dotenv_values(Path.cwd() / ".daimon" / "pinchtab.env")
        new_base = pinchtab_env.get("PINCHTAB_BASE") or settings.pinchtab_base
        new_token = pinchtab_env.get("PINCHTAB_TOKEN") or settings.pinchtab_token
        new_settings = replace(settings, pinchtab_base=new_base, pinchtab_token=new_token)
        print(f"[daimon-agent] PinchTab started on {new_settings.pinchtab_base}", file=sys.stderr)
        return new_settings
    except asyncio.TimeoutError:
        print("[daimon-agent] PinchTab setup timed out", file=sys.stderr)
        return settings
    except Exception as exc:
        print(f"[daimon-agent] PinchTab setup error: {exc}", file=sys.stderr)
        return settings


async def _stop_pinchtab() -> None:
    """Gracefully stop the PinchTab server if the setup script exists, and
    remove stale env/pid files so the next auto-start begins from a clean
    slate."""
    script = str(PINCHTAB_SETUP_SCRIPT)
    if not Path(script).exists():
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            script, "stop",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except Exception:
        pass  # best-effort — don't block shutdown


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


async def build_default_graph(settings: Settings, *, memory: MemoryStore | None = None) -> tuple[Any, Any, Any]:
    """Build the agent graph with the unified tool set. Returns (graph, checkpointer, router)."""
    router = ModelRouter(settings)
    tools = build_tools(settings, memory=memory, session_id="server")
    checkpointer = await make_sqlite_checkpointer(settings.checkpoints_db)
    graph = build_graph(settings, router, tools, checkpointer=checkpointer)
    return graph, checkpointer, router


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
    app["router"] = ModelRouter(settings)  # lazy: only constructed when called
    # Turn registry for the /status endpoint — counts active turns across all
    # sessions (both app-initiated and CLI-initiated) so the activity signal
    # dot on the pill covers *every* source of work.
    app["turn_registry"] = {"count": 0, "sessions": Counter(), "lock": asyncio.Lock()}

    async def startup(app: web.Application) -> None:
        nonlocal settings
        # Build the graph first — PinchTab auto-start runs in the background
        # so the server is reachable on /health immediately.
        graph, checkpointer, router = await graph_builder(settings, memory=app["memory"])
        app["graph"] = graph
        app["checkpointer"] = checkpointer
        app["router"] = router
        app["skills"] = discover_skills(settings.resolved_skills_dir)
        print(f"[daimon-agent] listening on :{settings.port}", file=sys.stderr)

        # Vault-note indexing runs in the background — with a large vault
        # (e.g. 50k+ notes in an Obsidian vault) it can take minutes, and
        # the server must be reachable while it works.  Search over vault
        # notes is a progressive feature; turns that need it will find
        # whatever has been indexed so far.
        loop = asyncio.get_running_loop()

        async def _vault_index_bg() -> None:
            try:
                count = await loop.run_in_executor(
                    None, index_existing_vault_notes, settings, app["memory"],
                )
                print(
                    f"[daimon-agent] indexed {count} existing vault note(s) on startup",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"[daimon-agent] vault indexing failed: {exc}",
                    file=sys.stderr,
                )

        asyncio.ensure_future(_vault_index_bg())

        # PinchTab auto-start runs in the background — it can take 10-60s
        # and we don't want to block the server for that.  Browser tools
        # will return errors until PinchTab is ready.
        async def _pinchtab_bg() -> None:
            try:
                updated = await asyncio.wait_for(
                    _ensure_pinchtab(settings), timeout=90.0,
                )
                app["settings"] = updated
            except asyncio.TimeoutError:
                print(
                    "[daimon-agent] PinchTab auto-start timed out after 90s — "
                    "browser tools will be unavailable",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"[daimon-agent] PinchTab auto-start failed: {exc} — "
                    f"browser tools will be unavailable",
                    file=sys.stderr,
                )
        asyncio.ensure_future(_pinchtab_bg())

    async def cleanup(app: web.Application) -> None:
        await close_checkpointer(app["checkpointer"])
        await aclose_browser()
        await aclose_web_fetcher()
        await aclose_search_provider()
        await close_all_repls()
        app["memory"].close()
        await _stop_pinchtab()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    async def health(request: web.Request) -> web.Response:
        s = request.app["settings"]
        return web.json_response({
            "ok": True,
            "workspace": str(s.resolved_workspace_dir.resolve()),
        })

    async def task(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return web.Response(status=400, text="instruction is required")
        session_id = str(body.get("session_id") or "default")
        # agent parameter is accepted for backward compat but ignored —
        # there is a single unified agent now.
        print(f"[daimon-agent] received task: {instruction}", file=sys.stderr)

        graph = app["graph"]

        response = web.StreamResponse(headers={"content-type": NDJSON})
        await response.prepare(request)

        # Events must be emitted from sync graph-node context in order, so the
        # emit closure enqueues and a single writer task drains the queue.
        queue: asyncio.Queue[tuple[bytes, bool] | None] = asyncio.Queue()
        # `terminal_emitted` — a done|error event was enqueued (done|error is
        # exclusive per the contract). The writer closes the body right after
        # the terminal event itself, so the client's stream ends at the result
        # instead of staying open through reflection/teardown (a connection
        # that dies in that window made the client's final read fail with
        # hyper's "incomplete message", surfacing as a spurious stream error
        # after the result). The terminal-ness rides on the *chunk*, not this
        # flag: the flag is set as soon as the terminal event is enqueued,
        # which can happen while earlier chunks are still queued — checking
        # the flag after every write would drop those earlier chunks' tails
        # and the done itself. FIFO + synchronous emit keeps everything before
        # the terminal event ahead of it in the queue.
        terminal_emitted = False

        async def writer() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    break
                chunk, is_terminal = item
                await response.write(chunk)
                if is_terminal:
                    break

        writer_task = asyncio.create_task(writer())

        def emit(event: dict) -> None:
            nonlocal terminal_emitted
            is_terminal = event.get("type") in ("done", "error")
            if is_terminal:
                terminal_emitted = True
            queue.put_nowait(
                (json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n", is_terminal)
            )

        def frame_task(emit_fn) -> asyncio.Task | None:
            return live_frames.start(emit_fn, build_browser(settings), settings)

        # Register the turn globally *before* acquiring the per-session lock —
        # a turn queued behind another for the same session is still activity
        # and the pill should show busy while it waits. The /status endpoint
        # reads this registry to answer "is anything running."
        reg = app["turn_registry"]
        async with reg["lock"]:
            reg["count"] += 1
            reg["sessions"][session_id] += 1

        lock = app["locks"].setdefault(session_id, asyncio.Lock())
        async with lock:  # one turn per session at a time, like legacy's queue
            try:
                await run_turn(
                    instruction,
                    session_id,
                    settings,
                    emit,
                    graph=graph,
                    memory=app["memory"],
                    skills=app["skills"],
                    router=app["router"],
                    live_frames=frame_task,
                )
            except Exception as exc:  # last-ditch backstop, port of server.ts's .catch
                print(f"[daimon-agent] unhandled error running task: {exc}", file=sys.stderr)
                if not terminal_emitted:  # done|error is exclusive per the contract
                    try:
                        emit(error_event(str(exc)))
                    except Exception:
                        pass  # response may already be closed
            finally:
                # De-register the turn so /status stops reporting busy.
                async with reg["lock"]:
                    reg["count"] -= 1
                    if reg["sessions"][session_id] <= 1:
                        del reg["sessions"][session_id]
                    else:
                        reg["sessions"][session_id] -= 1

                await queue.put(None)
                await writer_task
                # The terminal chunk has been written; the client ends its
                # read at the done|error line and closes the connection right
                # after (stream-end-is-the-result contract), so this final
                # chunk-footer write can race the close — a reset here is
                # expected, not an error. Best-effort, like the terminal
                # write above.
                with contextlib.suppress(ClientConnectionResetError):
                    await response.write_eof()
        return response

    async def status(request: web.Request) -> web.Response:
        """Global busy state — the pill polls this to drive its activity dot.
        Covers both app-initiated and CLI-initiated turns because they share
        the same server process."""
        reg = request.app["turn_registry"]
        async with reg["lock"]:
            return web.json_response({
                "running": True,
                "busy": reg["count"] > 0,
                "active_turns": reg["count"],
                "sessions": sorted(reg["sessions"]),
            })

    async def workspace_handler(request: web.Request) -> web.Response:
        """GET /workspace — return the server's resolved workspace directory."""
        s = request.app["settings"]
        return web.json_response({"workspace": str(s.resolved_workspace_dir.resolve())})

    async def tools_handler(request: web.Request) -> web.Response:
        """GET /tools — return the tool list for the unified agent."""
        graph = request.app["graph"]
        tools = getattr(graph, "tools", []) if graph is not None else []
        result = []
        for t in tools:
            name = getattr(t, "name", "")
            desc = getattr(t, "description", "")
            if name:
                result.append({"name": name, "description": desc})
        return web.json_response(result)

    async def config_handler(request: web.Request) -> web.Response:
        """GET /config — non-secret agent configuration for the app's settings tab."""
        s = request.app["settings"]
        pinchtab_ok = await _pinchtab_healthy(s)
        return web.json_response({
            "model": s.model,
            "flash_model": s.resolved_flash_model,
            "api_base": s.api_base or "(default)",
            "api_key_configured": bool(s.api_key),
            "workspace": str(s.resolved_workspace_dir.resolve()),
            "vault": str(s.vault_dir.resolve()),
            "port": s.port,
            "live_frames": s.live_frames,
            "reflect": s.reflect,
            "compaction_chars": s.compaction_chars,
            "temperature": s.temperature,
            "max_tokens": s.max_tokens,
            "pinchtab_base": s.pinchtab_base,
            "pinchtab_healthy": pinchtab_ok,
        })

    async def config_update_handler(request: web.Request) -> web.Response:
        """POST /config — update configuration (currently: api_key only).
        Writes to the .env file so the change survives restarts."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")

        api_key = body.get("api_key")
        if api_key is not None:
            if not isinstance(api_key, str) or not api_key.strip():
                return web.Response(status=400, text="api_key must be a non-empty string")
            env_path = Path.cwd() / ".env"
            _patch_env_file(env_path, "DEEPSEEK_API_KEY", api_key.strip())
            # Update in-memory settings so the change takes effect immediately.
            request.app["settings"] = replace(
                request.app["settings"], api_key=api_key.strip()
            )
            # Rebuild the router so model calls use the new key.
            request.app["router"] = ModelRouter(request.app["settings"])
            return web.json_response({"ok": True})

        return web.Response(status=400, text="no config fields to update")

    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/workspace", workspace_handler)
    app.router.add_get("/tools", tools_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_post("/config", config_update_handler)
    app.router.add_post("/task", task)
    return app


def _patch_env_file(path: Path, key: str, value: str) -> None:
    """Update or append a KEY=VALUE line in a dotenv file, preserving comments."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        updated = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
                new_lines.append(f"{key}={value}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={value}")
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        path.write_text(f"{key}={value}\n", encoding="utf-8")


def main() -> None:
    settings = Settings.from_env()
    # When the CLI spawned us it set DAIMON_PIDFILE — write our pid + port so
    # `daimon --stop` and concurrent spawns can find us. The app and manual
    # runs never set it, so they stay unmanaged (each owner kills only its
    # own). Writing it here (not in the spawner) kills the double-spawn race:
    # the pidfile always names the live server, so a stale one is
    # dead-by-definition and the next ensure/--stop just removes it.
    pidfile = os.environ.get("DAIMON_PIDFILE")
    if pidfile:
        path = Path(pidfile)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n{settings.port}\n", encoding="utf-8")
    # run_app awaits the app coroutine inside its own loop, so startup hooks
    # (checkpointer, browser) bind to the running loop.
    web.run_app(create_app(settings), host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    # `python -m daimon_agent.server` must actually serve — this is what the
    # CLI's spawn uses (sys.executable resolves in the tool venv). Without
    # the guard the module imports and exits 0 silently.
    raise SystemExit(main())
