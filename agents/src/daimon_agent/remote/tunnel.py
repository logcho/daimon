"""An optional public tunnel, for reaching the gateway without Tailscale.

Modelled on the PinchTab auto-start in `server.py`: shell out, scrape what we
need from the output, and never let a failure here stop the thing it is
supposed to be helping. A gateway that cannot open a tunnel is still a working
gateway on loopback.

This is the least safe way to run remote control, and the CLI says so. A quick
tunnel is a world-reachable URL, so the bearer token becomes the only gate —
which is why tunnel mode shortens the pairing window and keeps pairing itself
on the LAN by default.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys

#: cloudflared prints the assigned hostname to stderr, mixed in with its logs.
_URL = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")

#: How long to wait for that line before giving up. Generous: it involves
#: registering with Cloudflare's edge.
STARTUP_TIMEOUT_S = 30.0


class Tunnel:
    """A running `cloudflared` child, and the URL it was given."""

    def __init__(self) -> None:
        self.url: str | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @staticmethod
    def available() -> str | None:
        return shutil.which("cloudflared")

    async def start(self, port: int) -> str | None:
        binary = self.available()
        if binary is None:
            print(
                "[daimon-remote] cloudflared not found — install it "
                "(`brew install cloudflared`) or use --lan instead",
                file=sys.stderr,
            )
            return None

        self._proc = await asyncio.create_subprocess_exec(
            binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        async def scrape() -> str | None:
            assert self._proc is not None and self._proc.stderr is not None
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return None  # it exited without ever telling us a hostname
                match = _URL.search(line)
                if match:
                    return match.group(0).decode()

        try:
            self.url = await asyncio.wait_for(scrape(), timeout=STARTUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            print("[daimon-remote] cloudflared did not report a URL in time",
                  file=sys.stderr)
            await self.stop()
            return None

        if self.url is None:
            print("[daimon-remote] cloudflared exited before opening a tunnel",
                  file=sys.stderr)
            await self.stop()
        return self.url

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        self.url = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
