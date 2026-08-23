"""Finding the agent servers running on this machine.

Nothing new is invented here. `client.py` already keeps a run dir per
workspace, keyed by a hash of its resolved path, holding a `workspace.txt`
sidecar with the literal path and a pidfile with the server's pid and port —
built for `daimon --stop` and `--list`, and explicitly designed to stay
`ls`-enumerable so that "list every running workspace server" could be added
later without a layout change. This is that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from ..client import _pid_alive, _read_pid, _read_port, default_run_root

#: A dead server must not stall the session list behind a connect timeout.
PROBE_TIMEOUT_S = 1.5


@dataclass(frozen=True)
class Workspace:
    """One live agent server."""

    key: str          # the run dir's name: sha256(path)[:16]
    path: str         # the workspace directory it confines to
    port: int
    pid: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def bus_url(self) -> str:
        return f"{self.base_url}/bus/ws"

    @property
    def name(self) -> str:
        """What a phone shows in a workspace list. The directory's own name,
        which is what the user calls the project."""
        return Path(self.path).name or self.path


def _candidates(root: Path) -> list[tuple[str, str, int, int]]:
    """Run dirs that name a live process, without touching the network."""
    found: list[tuple[str, str, int, int]] = []
    if not root.is_dir():
        return found
    for run_dir in sorted(root.iterdir()):
        pidfile = run_dir / "daimon-agent.pid"
        sidecar = run_dir / "workspace.txt"
        if not pidfile.exists():
            continue
        pid, port = _read_pid(pidfile), _read_port(pidfile)
        if pid is None or port is None or not _pid_alive(pid):
            continue
        try:
            path = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else ""
        except OSError:
            path = ""
        found.append((run_dir.name, path or str(run_dir), port, pid))
    return found


async def _healthy(session: aiohttp.ClientSession, port: int) -> str | None:
    """The workspace a server reports, or None if it isn't answering.

    A live pid is not enough: the pidfile outlives a server being SIGKILLed,
    and a recycled pid belongs to something else entirely. `/health` also
    returns the workspace it actually confines to, which beats trusting the
    sidecar — the server is the authority on its own root.
    """
    try:
        async with session.get(
            f"http://127.0.0.1:{port}/health",
            timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            return str(body.get("workspace") or "") or None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def discover(
    session: aiohttp.ClientSession, *, root: Path | None = None
) -> list[Workspace]:
    """Every agent server currently answering on this machine."""
    candidates = _candidates(root or default_run_root())
    if not candidates:
        return []
    probes = await asyncio.gather(
        *(_healthy(session, port) for _key, _path, port, _pid in candidates)
    )
    live: list[Workspace] = []
    for (key, path, port, pid), workspace in zip(candidates, probes):
        if workspace is None:
            continue
        live.append(Workspace(key=key, path=workspace or path, port=port, pid=pid))
    return live
