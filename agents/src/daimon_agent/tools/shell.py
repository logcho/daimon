"""The shell tool — stage, don't execute, with a workspace-confined exception.

Daimon's non-disruption invariant: a shell command that could reach outside
the workspace (absolute paths elsewhere, `..`, `~`, credential verbs) is
never executed — it is staged via a `ui_action` event for the user to run
themselves. Commands whose static scan shows they stay inside the workspace
(the vault) do run, with cwd pinned to the workspace root and secrets
scrubbed from the child environment. When in doubt: stage.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from ..emitter import emit
from ..events import ui_action_event
from ..workspace import Confinement

# Verbs that handle credentials or exfiltrate data — ALWAYS staged, whatever
# else the command does. Includes the network fetchers (curl/wget) since an
# arbitrary fetch could send data out.
CREDENTIAL_VERBS = {
    "ssh", "scp", "sftp", "rsync", "telnet", "ftp", "curl", "wget",
    "login", "passwd", "sudo", "su", "doas", "kinit", "aws", "gcloud", "az",
    "gh",
}

# Verbs that publish, install, or reach package registries — a supply-chain or
# publishing step the user should approve. Staged too.
REMOTE_VERBS = {"git", "npm", "pip", "pip3", "uv", "poetry"}

STAGED_NOT_EXECUTED = (
    "I staged this command in a terminal tab instead of running it — it could "
    "reach outside the workspace ({reason}). The user can press Enter to run it "
    "if they want. To keep working here, try a command confined to the workspace."
)


def command_stays_in_workspace(command: str, root: Path) -> str | None:
    """Static scan. Returns None when the command may run, else the reason it
    must be staged instead. Conservative by design — any doubt stages."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unbalanced quotes"
    if not tokens:
        return None

    first = tokens[0]
    if first in CREDENTIAL_VERBS:
        return f"'{first}' may handle credentials or exfiltrate data"
    if first in REMOTE_VERBS:
        return f"'{first}' publishes, installs, or reaches remote hosts"

    for token in tokens:
        # `..` as a path component is an escape attempt (cat ../../etc/passwd).
        if ".." in token.split("/"):
            return f'"{token}" escapes the workspace via ..'
        # Absolute paths must live under the workspace root.
        if token.startswith("/") and not _is_under(token, root):
            return f'"{token}" is an absolute path outside the workspace'
        # `~` expands to the user's home, which is outside.
        if "~" in token:
            return f'"{token}" references the home directory'
    return None


def _is_under(token: str, root: Path) -> bool:
    try:
        resolved = Path(token).resolve()
        return resolved == root or root in resolved.parents
    except OSError:
        return False


# Secret env vars must never leak into a shell child.
_SECRET_ENV = {"DEEPSEEK_API_KEY", "PINCHTAB_TOKEN", "TAVILY_API_KEY"}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SECRET_ENV:
        env.pop(key, None)
    return env


async def run_shell(conf: Confinement, command: str, timeout_s: float = 60.0) -> str:
    reason = command_stays_in_workspace(command, conf.root)
    if reason is not None:
        emit(ui_action_event(command))
        return STAGED_NOT_EXECUTED.format(reason=reason)

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(conf.root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_child_env(),
    )
    try:
        output = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"Command timed out after {timeout_s:g}s and was killed."

    stdout = (output[0] or b"").decode("utf-8", errors="replace")
    if len(stdout) > 8000:
        stdout = stdout[:8000] + "\n…(output truncated)"
    if proc.returncode != 0:
        return f"Command exited with code {proc.returncode}:\n{stdout}"
    return stdout or "(no output)"
