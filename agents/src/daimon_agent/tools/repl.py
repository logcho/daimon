"""The kernel REPL — PrimeAgent's kernel-as-tool, per-session.

A real Jupyter kernel (ipykernel via jupyter_client) keeps Python state across
calls in one session: `x = 1` then `x + 1` works. Each session owns one
manager; the kernel's working directory is the workspace root. Output is
stdout/stderr/display data collected until the kernel goes idle, truncated at
8k — the same shape a code interpreter tool is expected to return.
"""

from __future__ import annotations

from pathlib import Path

from jupyter_client import AsyncKernelManager

MAX_OUTPUT_CHARS = 8000


class ReplManager:
    def __init__(self, cwd: Path, kernel_name: str = "python3") -> None:
        self._cwd = Path(cwd)
        self._kernel_name = kernel_name
        self._km: AsyncKernelManager | None = None
        self._kc = None

    async def start(self) -> None:
        if self._km is not None:
            return
        km = AsyncKernelManager(kernel_name=self._kernel_name)
        # cwd goes to start_kernel, NOT to the constructor. AsyncKernelManager
        # is a traitlets HasTraits with no `cwd` trait, so a constructor kwarg
        # is silently discarded (traitlets only warns) and the kernel inherits
        # the *server process's* directory instead of the workspace. The agent
        # then sees its file tools and its kernel disagreeing about where it
        # is, which is exactly as confusing as it sounds.
        await km.start_kernel(cwd=str(self._cwd))  # async in jupyter_client 8.x
        kc = km.client()
        kc.start_channels()
        await kc.wait_for_ready(timeout=60)  # raises RuntimeError if never ready
        self._km = km
        self._kc = kc

    async def stop(self) -> None:
        if self._km is not None:
            self._kc.stop_channels()
            await self._km.shutdown_kernel(now=True)  # async in jupyter_client 8.x
            self._km = None
            self._kc = None

    async def execute(self, code: str) -> str:
        await self.start()
        msg_id = self._kc.execute(code)  # sync on AsyncKernelClient in 8.x
        parts: list[str] = []
        while True:
            msg = await self._kc.iopub_channel.get_msg(timeout=60)
            # Only our execution's messages: the kernel idles between
            # executions too, and a stale stream/status from an earlier call
            # must not be collected or cut this one short.
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            msg_type = msg["msg_type"]
            content = msg["content"]
            if msg_type == "stream":
                parts.append(content["text"])
            elif msg_type == "execute_result":
                data = content.get("data", {})
                if "text/plain" in data:
                    parts.append(str(data["text/plain"]))
            elif msg_type == "error":
                # Record the error but keep draining to this execution's idle
                # status — breaking early leaves the iopub queue mid-stream and
                # the next execution's output gets lost.
                trace = "\n".join(content.get("traceback", []))
                parts.append(f"Error: {content.get('ename', '')}: {content.get('evalue', '')}\n{trace}")
            elif msg_type == "status" and content.get("execution_state") == "idle":
                break
        text = "".join(parts)
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n…(output truncated)"
        return text or "(no output)"


_repls: dict[str, ReplManager] = {}


def get_repl(session_id: str, workspace_root: Path) -> ReplManager:
    """Per-session managers live for the process; the server's shutdown closes
    them all."""
    if session_id not in _repls:
        _repls[session_id] = ReplManager(workspace_root)
    return _repls[session_id]


async def close_repl(session_id: str) -> None:
    """Shut down one session's kernel. A kernel is a real child process, so a
    session whose graph is evicted has to take its kernel with it."""
    repl = _repls.pop(session_id, None)
    if repl is None:
        return
    try:
        await repl.stop()
    except Exception:
        pass  # teardown is best-effort; a stuck kernel must not fail a request


async def close_all_repls() -> None:
    for repl in _repls.values():
        try:
            await repl.stop()
        except Exception:
            pass
    _repls.clear()
