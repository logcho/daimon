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
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
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
from .envfile import patch_env_file
from .events import TERMINAL_TYPES, error_event
from .graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from .memory import MemoryStore
from .model import ModelRouter
from .providers import (
    KNOWN_PROVIDERS,
    list_models,
    parse_spec,
    provider_available,
    provider_key,
)
from .run import resume_turn, run_turn
from .skills.injector import discover_skills
from .skills.registry import RegistryError, SkillRegistry, install_bundle
from .tools import build_tools
from .tools.files import forget_reads
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


def _discover(settings: Settings) -> list:
    """The merged skill library: the user's global one plus this project's."""
    return discover_skills(settings.resolved_skills_dir, settings.project_skills_dir)


def adopt_legacy_skills(settings: Settings) -> int:
    """Copy skills from the old vault-relative location into the global one.

    The library used to live under the vault, which resolved against the
    server's cwd. Moving it to `~/.daimon/skills` would otherwise orphan
    whatever a user had already built up — they'd open the app to an empty
    list and reasonably conclude their skills were gone.

    Copies rather than moves, so reverting this change loses nothing, and only
    when the global library is empty — which makes it a one-time event by
    construction, with no flag to keep in sync.
    """
    target = settings.resolved_skills_dir
    legacy = settings.vault_dir / "skills"
    if legacy == target or not legacy.is_dir():
        return 0
    if any(target.glob("*/SKILL.md")):
        return 0  # already populated — never overwrite

    copied = 0
    for skill_md in sorted(legacy.glob("*/SKILL.md")):
        try:
            shutil.copytree(skill_md.parent, target / skill_md.parent.name)
            copied += 1
        except (OSError, shutil.Error) as exc:  # one bad skill mustn't stop the rest
            print(f"[daimon-agent] could not adopt skill {skill_md.parent.name}: {exc}",
                  file=sys.stderr)
    if copied:
        print(f"[daimon-agent] copied {copied} skill(s) from {legacy} to {target}",
              file=sys.stderr)
    return copied


async def build_default_graph(settings: Settings, *, memory: MemoryStore | None = None) -> tuple[Any, Any, Any]:
    """Build the agent graph with the unified tool set. Returns (graph, checkpointer, router)."""
    router = ModelRouter(settings)
    tools = build_tools(settings, memory=memory, session_id="server")
    checkpointer = await make_sqlite_checkpointer(settings.resolved_checkpoints_db)
    graph = build_graph(settings, router, tools, checkpointer=checkpointer)
    return graph, checkpointer, router


async def _idle_shutdown_loop(
    turn_registry: dict,
    idle_timeout: float,
    *,
    poll_interval: float | None = None,
    sleep: Any = asyncio.sleep,
    kill: Any = os.kill,
) -> None:
    """Poll `turn_registry` and self-terminate (SIGTERM) once `idle_timeout`
    seconds have passed with no active turns. Polls rather than using one
    deadline per turn, because the deadline itself moves every time a turn
    starts or ends — a `reg["count"] == 0` window between two turns of an
    active conversation must not trip this early.

    `poll_interval`/`sleep`/`kill` are overridable for tests only; production
    code (`create_app`'s `startup` hook) never passes them, so the effective
    poll interval is always `min(60, max(idle_timeout / 10, 5))` and the
    real `os.kill`."""
    if idle_timeout <= 0:
        return  # 0 disables auto-shutdown
    interval = poll_interval if poll_interval is not None else min(60.0, max(idle_timeout / 10, 5.0))
    while True:
        await sleep(interval)
        async with turn_registry["lock"]:
            idle_for = time.monotonic() - turn_registry["last_active_at"]
            should_stop = turn_registry["count"] == 0 and idle_for >= idle_timeout
        if should_stop:
            print(
                f"[daimon-agent] idle for {idle_for:.0f}s "
                f"(limit {idle_timeout:.0f}s) — shutting down",
                file=sys.stderr,
            )
            kill(os.getpid(), signal.SIGTERM)
            return


async def create_app(
    settings: Settings | None = None,
    *,
    graph_builder: Any = build_default_graph,
    memory: MemoryStore | None = None,
    pidfile: Path | None = None,
) -> web.Application:
    settings = settings or Settings.from_env()
    app = web.Application()
    app["settings"] = settings
    app["graph"] = None
    app["checkpointer"] = None
    app["memory"] = memory or MemoryStore(settings.resolved_memory_db)
    app["locks"] = {}
    app["skills"] = []
    app["router"] = ModelRouter(settings)  # lazy: only constructed when called
    # Removed on clean shutdown (see cleanup()) so a self-exited (idle
    # timeout, --stop, SIGTERM) server never leaves a stale pidfile behind.
    app["pidfile"] = pidfile
    # Turn registry for the /status endpoint — counts active turns across all
    # sessions (both app-initiated and CLI-initiated) so the activity signal
    # dot on the pill covers *every* source of work. `last_active_at` drives
    # idle auto-shutdown (see _idle_shutdown_bg in startup()).
    app["turn_registry"] = {
        "count": 0,
        "sessions": Counter(),
        "lock": asyncio.Lock(),
        "last_active_at": time.monotonic(),
    }

    async def startup(app: web.Application) -> None:
        nonlocal settings
        # Build the graph first — PinchTab auto-start runs in the background
        # so the server is reachable on /health immediately.
        graph, checkpointer, router = await graph_builder(settings, memory=app["memory"])
        app["graph"] = graph
        app["checkpointer"] = checkpointer
        app["router"] = router
        adopt_legacy_skills(settings)
        app["skills"] = _discover(settings)
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

        # Self-terminate after sustained inactivity — a per-workspace server
        # has nothing else watching it once the terminal that spawned it
        # closes.
        asyncio.ensure_future(_idle_shutdown_loop(app["turn_registry"], settings.idle_timeout_s))

    async def cleanup(app: web.Application) -> None:
        await close_checkpointer(app["checkpointer"])
        await aclose_browser()
        await aclose_web_fetcher()
        await aclose_search_provider()
        await close_all_repls()
        # The read registry is process-lived like the kernels, and outlives the
        # files it describes — a stale stamp would refuse an edit on the next
        # run for a change that happened while nothing was watching.
        forget_reads()
        app["memory"].close()
        await _stop_pinchtab()
        pidfile = app.get("pidfile")
        if pidfile is not None:
            with contextlib.suppress(OSError):
                pidfile.unlink()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    async def health(request: web.Request) -> web.Response:
        s = request.app["settings"]
        return web.json_response({
            "ok": True,
            "workspace": str(s.resolved_workspace_dir.resolve()),
        })

    async def _stream_turn_response(
        request: web.Request,
        session_id: str,
        run: Any,  # Callable[[Callable[[dict], None]], Awaitable[Any]]
    ) -> web.StreamResponse:
        """Run one unit of turn work and stream its TaskEvents as NDJSON.

        Shared by /task (a new turn) and /resume (continuing a turn that
        suspended on an interrupt): they differ only in what `run` does, so
        everything subtle about the streaming lives here once — the ordered
        queue, the terminal-chunk close, the turn registry, and the
        per-session lock.
        """
        response = web.StreamResponse(headers={"content-type": NDJSON})
        await response.prepare(request)

        # Events must be emitted from sync graph-node context in order, so the
        # emit closure enqueues and a single writer task drains the queue.
        queue: asyncio.Queue[tuple[bytes, bool] | None] = asyncio.Queue()
        # `terminal_emitted` — a done|error|ask event was enqueued (they are
        # mutually exclusive per the contract). The writer closes the body
        # right after the terminal event itself, so the client's stream ends at
        # the result instead of staying open through reflection/teardown (a
        # connection that dies in that window made the client's final read fail
        # with hyper's "incomplete message", surfacing as a spurious stream
        # error after the result). The terminal-ness rides on the *chunk*, not
        # this flag: the flag is set as soon as the terminal event is enqueued,
        # which can happen while earlier chunks are still queued — checking
        # the flag after every write would drop those earlier chunks' tails
        # and the done itself. FIFO + synchronous emit keeps everything before
        # the terminal event ahead of it in the queue.
        terminal_emitted = False
        # Set when the client hangs up mid-turn (the user pressed Esc, or the
        # app quit). The writer notices first, because it is the only thing
        # touching the socket — it cancels the work rather than letting an
        # abandoned turn keep burning tokens.
        client_gone = asyncio.Event()

        async def writer() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    break
                chunk, is_terminal = item
                try:
                    await response.write(chunk)
                except (ConnectionResetError, ClientConnectionResetError):
                    client_gone.set()
                    break
                if is_terminal:
                    break

        writer_task = asyncio.create_task(writer())

        def emit(event: dict) -> None:
            nonlocal terminal_emitted
            is_terminal = event.get("type") in TERMINAL_TYPES
            if is_terminal:
                terminal_emitted = True
            queue.put_nowait(
                (json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n", is_terminal)
            )

        # Register the turn globally *before* acquiring the per-session lock —
        # a turn queued behind another for the same session is still activity
        # and the pill should show busy while it waits. The /status endpoint
        # reads this registry to answer "is anything running."
        reg = app["turn_registry"]
        async with reg["lock"]:
            reg["count"] += 1
            reg["sessions"][session_id] += 1
            reg["last_active_at"] = time.monotonic()

        lock = app["locks"].setdefault(session_id, asyncio.Lock())
        async with lock:  # one turn per session at a time, like legacy's queue
            work = asyncio.create_task(run(emit))
            disconnect = asyncio.create_task(client_gone.wait())
            try:
                # Race the work against the client hanging up. run_turn owns
                # its own error handling, so a cancel here is the only way the
                # work ends early.
                await asyncio.wait(
                    {work, disconnect}, return_when=asyncio.FIRST_COMPLETED
                )
                if not work.done():
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await work
                    print(
                        f"[daimon-agent] client disconnected — cancelled turn "
                        f"for session {session_id}",
                        file=sys.stderr,
                    )
                else:
                    await work  # re-raise anything run_turn let escape
            except Exception as exc:  # last-ditch backstop, port of server.ts's .catch
                print(f"[daimon-agent] unhandled error running task: {exc}", file=sys.stderr)
                if not terminal_emitted:  # done|error|ask is exclusive per the contract
                    try:
                        emit(error_event(str(exc)))
                    except Exception:
                        pass  # response may already be closed
            finally:
                disconnect.cancel()
                # De-register the turn so /status stops reporting busy.
                async with reg["lock"]:
                    reg["count"] -= 1
                    if reg["sessions"][session_id] <= 1:
                        del reg["sessions"][session_id]
                    else:
                        reg["sessions"][session_id] -= 1
                    reg["last_active_at"] = time.monotonic()

                await queue.put(None)
                await writer_task
                # The terminal chunk has been written; the client ends its
                # read at the done|error|ask line and closes the connection
                # right after (stream-end-is-the-result contract), so this
                # final chunk-footer write can race the close — a reset here is
                # expected, not an error. Best-effort, like the terminal
                # write above.
                with contextlib.suppress(ClientConnectionResetError, ConnectionResetError):
                    await response.write_eof()
        return response

    def _frame_task(emit_fn) -> asyncio.Task | None:
        return live_frames.start(emit_fn, build_browser(app["settings"]), app["settings"])

    async def task(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return web.Response(status=400, text="instruction is required")
        session_id = str(body.get("session_id") or "default")
        # What the client can render. "ask" is the one that matters: the agent
        # is only offered ask_user/present_plan when somebody is there to
        # answer, so a one-shot CLI or the chat app never gets asked a question
        # it would hang on.
        raw_caps = body.get("capabilities")
        capabilities = [str(c) for c in raw_caps] if isinstance(raw_caps, list) else []
        mode = body.get("mode") if body.get("mode") in ("plan", "normal") else "normal"
        # agent parameter is accepted for backward compat but ignored —
        # there is a single unified agent now.
        print(f"[daimon-agent] received task: {instruction}", file=sys.stderr)

        graph = app["graph"]
        # Re-scan every turn rather than trusting the startup scan: a skill the
        # agent saved a moment ago, or one the user just dropped into the
        # folder, has to be usable now. It's a directory glob.
        app["skills"] = _discover(app["settings"])

        async def run(emit) -> Any:
            return await run_turn(
                instruction,
                session_id,
                app["settings"],
                emit,
                graph=graph,
                memory=app["memory"],
                skills=app["skills"],
                router=app["router"],
                live_frames=_frame_task,
                capabilities=capabilities,
                mode=mode,
            )

        return await _stream_turn_response(request, session_id, run)

    async def resume(request: web.Request) -> web.StreamResponse:
        """POST /resume — answer a question the agent asked and continue the
        turn from exactly where it suspended. The checkpointer holds the state,
        so this survives the client reconnecting or the turn being answered
        much later."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")
        session_id = str(body.get("session_id") or "default")
        if "answer" not in body:
            return web.Response(status=400, text="answer is required")
        answer = body["answer"]
        graph = app["graph"]

        async def run(emit) -> Any:
            return await resume_turn(
                answer,
                session_id,
                app["settings"],
                emit,
                graph=graph,
                memory=app["memory"],
                router=app["router"],
                live_frames=_frame_task,
            )

        return await _stream_turn_response(request, session_id, run)

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

    async def skills_handler(request: web.Request) -> web.Response:
        """GET /skills — the merged library. Scanned per request so a skill the
        agent just saved shows up without a restart."""
        skills = _discover(request.app["settings"])
        return web.json_response([
            {
                "name": s.name,
                "description": s.description,
                "source": s.source,
                "path": str(s.path) if s.path else "",
            }
            for s in skills
        ])

    async def skill_handler(request: web.Request) -> web.Response:
        """GET /skills/{name} — one skill's full SKILL.md."""
        name = request.match_info.get("name", "")
        for skill in _discover(request.app["settings"]):
            if skill.name == name and skill.path is not None:
                try:
                    return web.json_response({
                        "name": skill.name,
                        "description": skill.description,
                        "source": skill.source,
                        "path": str(skill.path),
                        "content": skill.path.read_text(encoding="utf-8"),
                    })
                except OSError as exc:
                    return web.Response(status=500, text=f"could not read skill: {exc}")
        return web.Response(status=404, text=f'no skill named "{name}"')

    # --- the public skill registry ------------------------------------------
    # Thin passthroughs: the CLI stays a thin client, and the cache lives in
    # one process instead of one per client.

    def _registry(settings_obj: Settings) -> SkillRegistry:
        return SkillRegistry(settings_obj.registry_cache_dir)

    async def registry_search_handler(request: web.Request) -> web.Response:
        query = request.query.get("q", "").strip()
        if not query:
            return web.Response(status=400, text="q is required")
        try:
            hits = await _registry(request.app["settings"]).search(
                query,
                limit=int(request.query.get("limit", 20)),
                featured=request.query.get("featured") in ("1", "true"),
            )
        except RegistryError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        return web.json_response([hit.as_dict() for hit in hits])

    async def registry_item_handler(request: web.Request) -> web.Response:
        """Resolve a slug to its repo and the skills inside it — an entry is a
        repo, and a repo may hold many skills."""
        try:
            entry, paths = await _registry(request.app["settings"]).resolve(
                request.match_info.get("slug", "")
            )
        except RegistryError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        return web.json_response({**entry.as_dict(), "skills": paths})

    async def registry_preview_handler(request: web.Request) -> web.Response:
        """The bundle's manifest *without* writing anything — what the install
        confirmation shows the user before they decide."""
        slug = request.match_info.get("slug", "")
        skill_dir = request.query.get("path", "")
        registry = _registry(request.app["settings"])
        try:
            entry = await registry.item(slug)
            if not skill_dir:
                paths = await registry.skill_paths(entry.repo)
                skill_dir = paths[0] if len(paths) == 1 else ""
            if not skill_dir:
                return web.Response(status=400, text="path is required for a multi-skill repo")
            bundle = await registry.fetch_bundle(entry, skill_dir)
        except RegistryError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        skill_md = next((f for f in bundle.files if f.path == "SKILL.md"), None)
        return web.json_response({
            **bundle.as_dict(),
            "skill_md": skill_md.content if skill_md else "",
        })

    async def skills_install_handler(request: web.Request) -> web.Response:
        """Write a bundle into a library. Only reached after the client has
        shown the user what's in it — the agent has no path to this."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")
        slug = str(body.get("slug") or "")
        skill_dir = str(body.get("path") or "")
        if not slug or not skill_dir:
            return web.Response(status=400, text="slug and path are required")

        settings_obj = request.app["settings"]
        root = (
            settings_obj.project_skills_dir
            if str(body.get("scope", "")).lower() == "project"
            else settings_obj.resolved_skills_dir
        )
        registry = _registry(settings_obj)
        try:
            entry = await registry.item(slug)
            bundle = await registry.fetch_bundle(entry, skill_dir)
            written = install_bundle(bundle, root)
        except RegistryError as exc:
            return web.json_response({"error": str(exc)}, status=502)

        memory_store = request.app["memory"]
        if memory_store is not None and bundle.description:
            memory_store.upsert_skill(bundle.name, bundle.description)
        return web.json_response({
            "name": bundle.name,
            "installed": written,
            "scope": "project" if root == settings_obj.project_skills_dir else "vault",
        })

    # --- the vault ----------------------------------------------------------
    # Served rather than read from disk by each client. The app used to resolve
    # the vault path itself and looked at an entirely different directory, so
    # it showed a stale set of notes — which would have made "delete" delete
    # the wrong files.

    def _vault_file(settings_obj: Settings, relative: str) -> Path:
        """Resolve a client-supplied path inside the vault, or raise.

        Same discipline as `workspace.Confinement`: resolve first, then check
        containment, so `..` and symlinks can't walk out. Used for folders as
        well as notes — this is pure path resolution; whether the target has to
        be a `.md` file is the caller's rule.
        """
        root = Path(settings_obj.vault_dir).resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise ValueError("path is outside the vault")
        return target

    def _is_internal(relative: Path) -> bool:
        """Paths the vault views must not show.

        `skills` has its own view, and dot-directories are plumbing rather
        than notes — `.daimon/memory` holds this workspace's own databases and
        would otherwise show up in the tree as a folder the user can't
        meaningfully use but can delete.
        """
        return any(part == "skills" or part.startswith(".") for part in relative.parts)

    async def vault_list_handler(request: web.Request) -> web.Response:
        """GET /vault — every note, newest first.

        Recursive: the agent writes to `vault/notes/`, and a top-level-only
        listing showed none of them.
        """
        root = Path(request.app["settings"].vault_dir)
        if not root.is_dir():
            return web.json_response([])
        notes = []
        for path in root.rglob("*.md"):
            if _is_internal(path.relative_to(root)):
                continue  # skills have their own view; dot-dirs are plumbing
            stat = path.stat()
            notes.append({
                "name": str(path.relative_to(root)),
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            })
        notes.sort(key=lambda n: n["modifiedAt"], reverse=True)
        return web.json_response(notes)

    async def vault_folders_handler(request: web.Request) -> web.Response:
        """GET /vault/folders — every folder, so the client can show empty ones.

        The note listing is `rglob("*.md")`, so a folder you just made and
        haven't written to yet appears nowhere in it — it would vanish on the
        next refresh. Folders are listed separately rather than inferred from
        note paths for exactly that reason.
        """
        root = Path(request.app["settings"].vault_dir)
        if not root.is_dir():
            return web.json_response([])
        folders = sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_dir() and not _is_internal(path.relative_to(root))
        )
        return web.json_response(folders)

    async def vault_folder_create_handler(request: web.Request) -> web.Response:
        """POST /vault/folders — create a folder (parents included)."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        path_str = body.get("path")
        if not isinstance(path_str, str) or not path_str.strip():
            return web.json_response({"error": "path must be a non-empty string"}, status=400)
        try:
            target = _vault_file(request.app["settings"], path_str.strip())
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if target.is_file():
            return web.json_response({"error": "a note already has that name"}, status=400)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        root = Path(request.app["settings"].vault_dir).resolve()
        return web.json_response({"ok": True, "path": str(target.relative_to(root))})

    async def vault_folder_delete_handler(request: web.Request) -> web.Response:
        """DELETE /vault/folders/{path} — remove a folder.

        A folder with notes in it is refused unless `?recursive=1`: deleting a
        folder is one click, but it can take an arbitrary number of notes with
        it, and that is not the same decision as deleting one note.
        """
        name = request.match_info.get("path", "")
        try:
            target = _vault_file(request.app["settings"], name)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        root = Path(request.app["settings"].vault_dir).resolve()
        if target == root:
            return web.json_response({"error": "cannot delete the vault root"}, status=400)
        if not target.is_dir():
            return web.json_response({"error": "no such folder"}, status=404)
        contained = [p for p in target.rglob("*.md")]
        recursive = request.query.get("recursive") in ("1", "true", "yes")
        if contained and not recursive:
            return web.json_response(
                {"error": f"folder is not empty ({len(contained)} note(s))", "notes": len(contained)},
                status=409,
            )
        memory_store = request.app["memory"]
        for note in contained:
            if memory_store is not None:
                with contextlib.suppress(Exception):
                    memory_store.delete_note(str(note.relative_to(root)))
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "deleted": name, "notes": len(contained)})

    async def vault_move_handler(request: web.Request) -> web.Response:
        """POST /vault/move — move or rename a note *or* a folder.

        Moving is how a vault actually gets organised, and it is not a
        write-then-delete: the memory index is keyed by name, so the old
        entries have to go or `recall` keeps citing paths that no longer
        exist. Moving a folder re-keys every note underneath it for the same
        reason.
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        src_name, dst_name = body.get("from"), body.get("to")
        if not isinstance(src_name, str) or not isinstance(dst_name, str):
            return web.json_response({"error": "from and to must be strings"}, status=400)
        try:
            src = _vault_file(request.app["settings"], src_name)
            dst = _vault_file(request.app["settings"], dst_name)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        root = Path(request.app["settings"].vault_dir).resolve()
        if src == root:
            return web.json_response({"error": "cannot move the vault root"}, status=400)
        if not src.exists():
            return web.json_response({"error": "no such note or folder"}, status=404)
        if dst.exists():
            return web.json_response({"error": f"{dst_name} already exists"}, status=409)

        is_folder = src.is_dir()
        if not is_folder and dst.suffix.lower() != ".md":
            return web.json_response({"error": "note names must end in .md"}, status=400)
        # Moving a folder into itself (or into its own descendant) would move
        # the destination out from under the move as it runs.
        if is_folder and (dst == src or src in dst.parents):
            return web.json_response({"error": "cannot move a folder into itself"}, status=400)

        # Note names to re-key, gathered before the move while they still
        # resolve.
        moved_notes = (
            [str(p.relative_to(root)) for p in src.rglob("*.md")] if is_folder else [src_name]
        )
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)

        memory_store = request.app["memory"]
        if memory_store is not None:
            for old in moved_notes:
                # The new name is the old one with the source prefix swapped
                # for the destination.
                new = dst_name if not is_folder else f"{dst_name}/{old[len(src_name) + 1:]}"
                new_path = root / new
                with contextlib.suppress(Exception):
                    memory_store.delete_note(old)
                    if new_path.is_file():
                        memory_store.index_note(
                            new, new_path.read_text(encoding="utf-8", errors="replace")
                        )

        if is_folder:
            return web.json_response({"ok": True, "path": dst_name, "notes": len(moved_notes)})
        stat = dst.stat()
        return web.json_response({
            "ok": True,
            "name": dst_name,
            "sizeBytes": stat.st_size,
            "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        })

    async def vault_read_handler(request: web.Request) -> web.Response:
        try:
            path = _vault_file(request.app["settings"], request.match_info.get("name", ""))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))
        if not path.is_file():
            return web.Response(status=404, text="no such note")
        return web.json_response({
            "name": request.match_info.get("name", ""),
            "content": path.read_text(encoding="utf-8", errors="replace"),
        })

    async def vault_write_handler(request: web.Request) -> web.Response:
        """PUT /vault/{name} — create or overwrite a note, and reindex it.

        One endpoint for both create and update: the vault is a directory of
        files, so "create" is just a write to a name that doesn't exist yet,
        and making the client pick between two endpoints would only invite
        getting it wrong. Parent directories are created so `folder/note.md`
        works without a separate mkdir step.
        """
        name = request.match_info.get("name", "")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content")
        if not isinstance(content, str):
            return web.json_response({"error": "content must be a string"}, status=400)
        try:
            path = _vault_file(request.app["settings"], name)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        # Only markdown: the listing is `rglob("*.md")`, so anything else
        # would be written but never shown again.
        if path.suffix.lower() != ".md":
            return web.json_response({"error": "note names must end in .md"}, status=400)
        if path.is_dir():
            return web.json_response({"error": "that name is a directory"}, status=400)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        memory_store = request.app["memory"]
        if memory_store is not None:
            # Best-effort, exactly as the delete path: a failed reindex costs
            # recall accuracy, not the user's note.
            with contextlib.suppress(Exception):
                memory_store.index_note(name, content)
        stat = path.stat()
        return web.json_response({
            "ok": True,
            "name": name,
            "sizeBytes": stat.st_size,
            "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        })

    async def vault_delete_handler(request: web.Request) -> web.Response:
        """DELETE /vault/{name} — remove a note, and its memory index entry.

        Leaving the FTS row behind would keep `recall` surfacing a note that no
        longer exists, which reads as the agent making things up.
        """
        name = request.match_info.get("name", "")
        try:
            path = _vault_file(request.app["settings"], name)
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))
        if not path.is_file():
            return web.Response(status=404, text="no such note")
        try:
            path.unlink()
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        memory_store = request.app["memory"]
        if memory_store is not None:
            with contextlib.suppress(Exception):
                memory_store.delete_note(name)
        return web.json_response({"ok": True, "deleted": name})

    async def skill_delete_handler(request: web.Request) -> web.Response:
        """DELETE /skills/{name} — remove a skill directory and everything in
        it (an installed skill is a directory of files, not one file)."""
        name = request.match_info.get("name", "")
        for skill in _discover(request.app["settings"]):
            if skill.name != name or skill.path is None:
                continue
            directory = skill.path.parent
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                return web.json_response({"error": str(exc)}, status=500)
            memory_store = request.app["memory"]
            if memory_store is not None:
                with contextlib.suppress(Exception):
                    memory_store.delete_skill(name)
            request.app["skills"] = _discover(request.app["settings"])
            return web.json_response({"ok": True, "deleted": name, "source": skill.source})
        return web.Response(status=404, text=f'no skill named "{name}"')

    async def models_handler(request: web.Request) -> web.Response:
        """GET /models — what every provider offers, with why it may be
        unusable. One implementation for the CLI and the app, so a model
        offered in one is offered in the other."""
        s = request.app["settings"]
        out: dict[str, Any] = {}
        for name in KNOWN_PROVIDERS:
            models, source = await list_models(name, s)
            out[name] = {
                "models": models,
                "source": source,
                "installed": provider_available(name),
                "key_configured": bool(provider_key(name, s)),
            }
        return web.json_response(out)

    async def config_handler(request: web.Request) -> web.Response:
        """GET /config — non-secret agent configuration for the app's settings tab."""
        s = request.app["settings"]
        pinchtab_ok = await _pinchtab_healthy(s)
        # api_key may be set via the Settings object OR directly in the OS
        # environment (e.g. the SDK's own fallback) — check both so the
        # settings tab shows the real state.
        key_configured = bool(s.api_key or os.environ.get("DEEPSEEK_API_KEY"))
        anthropic_key = bool(
            s.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        return web.json_response({
            "model": s.model,
            "flash_model": s.resolved_flash_model,
            "provider": s.provider,
            # What this install can actually run, and which keys it holds —
            # the two failure modes (package missing vs. key missing) are
            # different problems with different fixes.
            "providers": {
                name: {
                    "installed": provider_available(name),
                    "key_configured": {
                        "deepseek": key_configured,
                        "anthropic": anthropic_key,
                    }[name],
                }
                for name in KNOWN_PROVIDERS
            },
            "api_base": s.api_base or "(default)",
            "api_key_configured": key_configured,
            "workspace": str(s.resolved_workspace_dir.resolve()),
            "vault": str(s.vault_dir.resolve()),
            "port": s.port,
            "live_frames": s.live_frames,
            "reflect": s.reflect,
            "compaction_chars": s.compaction_chars,
            # The status bar's "ctx N%" denominator — reported so the
            # bar reads one source rather than half server, half local.
            "context_window": s.context_window,
            "temperature": s.temperature,
            "max_tokens": s.max_tokens,
            "pinchtab_base": s.pinchtab_base,
            "pinchtab_healthy": pinchtab_ok,
        })

    def _key_field(value: str) -> str:
        """Which provider a pasted key belongs to.

        Anthropic keys start `sk-ant-`; DeepSeek's are plain `sk-`. Detecting
        it here means the CLI's `/setup <key>` and the app's single key field
        both just work, and nobody has to be told which box to use.
        """
        return "anthropic_api_key" if value.startswith("sk-ant-") else "api_key"

    def _model_problem(spec: str, settings_obj: Settings) -> str | None:
        """Why this model spec can't run, or None.

        Checked before accepting, because a model is only reachable if its
        provider is installed *and* keyed — and the two have different fixes.
        Accepting an unusable spec would leave every later turn failing at the
        API call with nothing pointing back here.
        """
        provider, _ = parse_spec(spec, settings_obj.provider)
        if provider not in KNOWN_PROVIDERS:
            return f"unknown provider '{provider}' — expected one of {', '.join(KNOWN_PROVIDERS)}"
        if not provider_available(provider):
            return f"the {provider} provider isn't installed — run `uv sync --extra {provider}`"
        has_key = {
            "deepseek": bool(settings_obj.api_key or os.environ.get("DEEPSEEK_API_KEY")),
            "anthropic": bool(
                settings_obj.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
            ),
        }[provider]
        if not has_key:
            return f"no {provider} API key is set — add one first"
        return None

    async def config_update_handler(request: web.Request) -> web.Response:
        """POST /config — update API keys and model selection.
        Writes to the .env file so the change survives restarts."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json body")

        # field name in the request → (Settings attribute, env var)
        writable = {
            "api_key": ("api_key", "DEEPSEEK_API_KEY"),
            "anthropic_api_key": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            "model": ("model", "DAIMON_MODEL"),
            "flash_model": ("flash_model", "DAIMON_FLASH_MODEL"),
        }
        # `key` is the provider-agnostic field: paste a key, we work out whose.
        raw_key = body.get("key")
        if isinstance(raw_key, str) and raw_key.strip():
            body = {**body, _key_field(raw_key.strip()): raw_key.strip()}

        updates: dict[str, str] = {}
        for field_name, (attr, env_var) in writable.items():
            value = body.get(field_name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                return web.Response(
                    status=400, text=f"{field_name} must be a non-empty string"
                )
            updates[attr] = value.strip()

        if not updates:
            return web.Response(status=400, text="no config fields to update")

        # Validate models against the settings the keys in *this* request
        # produce, so setting a key and a model together works in one call.
        candidate = replace(request.app["settings"], **updates)
        for field_name in ("model", "flash_model"):
            attr = writable[field_name][0]
            if attr in updates:
                problem = _model_problem(updates[attr], candidate)
                if problem is not None:
                    return web.json_response(
                        {"error": f"can't use {updates[attr]}: {problem}"}, status=400
                    )

        for field_name, (attr, env_var) in writable.items():
            if attr in updates:
                patch_env_file(Path.cwd() / ".env", env_var, updates[attr])
                os.environ[env_var] = updates[attr]

        # Update in-memory settings so the change takes effect immediately, and
        # rebuild the router so model calls use the new key/model.
        request.app["settings"] = candidate
        request.app["router"] = ModelRouter(candidate)
        return web.json_response({"ok": True, "updated": sorted(updates)})

    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/workspace", workspace_handler)
    app.router.add_get("/tools", tools_handler)
    app.router.add_get("/skills", skills_handler)
    app.router.add_post("/skills/install", skills_install_handler)
    app.router.add_get("/skills/{name}", skill_handler)
    app.router.add_get("/registry/search", registry_search_handler)
    app.router.add_get("/registry/item/{slug}", registry_item_handler)
    app.router.add_get("/registry/preview/{slug}", registry_preview_handler)
    app.router.add_get("/vault", vault_list_handler)
    # Registered BEFORE the `/vault/{name:.*}` catch-all below — aiohttp
    # resolves in registration order, so these would otherwise be read as a
    # request for a note literally named "folders" or "move". Note names always
    # end in .md, so nothing real is shadowed.
    app.router.add_get("/vault/folders", vault_folders_handler)
    app.router.add_post("/vault/folders", vault_folder_create_handler)
    app.router.add_delete("/vault/folders/{path:.*}", vault_folder_delete_handler)
    app.router.add_post("/vault/move", vault_move_handler)
    app.router.add_get("/vault/{name:.*}", vault_read_handler)
    app.router.add_put("/vault/{name:.*}", vault_write_handler)
    app.router.add_delete("/vault/{name:.*}", vault_delete_handler)
    app.router.add_delete("/skills/{name}", skill_delete_handler)
    app.router.add_get("/models", models_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_post("/config", config_update_handler)
    app.router.add_post("/task", task)
    app.router.add_post("/resume", resume)
    return app



def main() -> None:
    settings = Settings.from_env()
    # When the CLI spawned us it set DAIMON_PIDFILE — write our pid + port so
    # `daimon --stop` and concurrent spawns can find us. The app and manual
    # runs never set it, so they stay unmanaged (each owner kills only its
    # own). Writing it here (not in the spawner) kills the double-spawn race:
    # the pidfile always names the live server, so a stale one is
    # dead-by-definition and the next ensure/--stop just removes it.
    # Passed through to create_app so cleanup() removes it on any clean exit
    # (idle timeout, --stop, SIGTERM) — not just when a spawner overwrites it.
    pidfile_env = os.environ.get("DAIMON_PIDFILE")
    pidfile_path: Path | None = None
    if pidfile_env:
        pidfile_path = Path(pidfile_env)
        pidfile_path.parent.mkdir(parents=True, exist_ok=True)
        pidfile_path.write_text(f"{os.getpid()}\n{settings.port}\n", encoding="utf-8")
    # run_app awaits the app coroutine inside its own loop, so startup hooks
    # (checkpointer, browser) bind to the running loop.
    web.run_app(
        create_app(settings, pidfile=pidfile_path), host="127.0.0.1", port=settings.port
    )


if __name__ == "__main__":
    # `python -m daimon_agent.server` must actually serve — this is what the
    # CLI's spawn uses (sys.executable resolves in the tool venv). Without
    # the guard the module imports and exits 0 silently.
    raise SystemExit(main())
