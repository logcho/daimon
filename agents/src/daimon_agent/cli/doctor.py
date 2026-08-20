"""Daimon doctor — diagnostic tool that checks every subsystem and reports
what's wrong with actionable recommendations.

Run via ``daimon --doctor`` or ``python -m daimon_agent.cli.doctor``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .. import client
from ..config import Settings

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class Status:
    OK = "✓"
    WARN = "⚠"
    FAIL = "✗"
    SKIP = "○"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    recommendation: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    cwd: str = ""
    timestamp: str = ""

    def add(self, name: str, status: str, detail: str = "", rec: str = "") -> None:
        self.checks.append(Check(name, status, detail, rec))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _read_dotenv_key(path: Path, key: str) -> str | None:
    """Read a single key from a .env-style file (KEY=value)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                val = v.strip().strip('"').strip("'")
                return val if val else None
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _check_env(report: Report) -> None:
    """DAIMON_WORKSPACE_DIR, API keys, and related env vars.

    Checks both os.environ AND the .env files the server would read
    (agents/.env and CWD/.env).
    """
    ws = os.environ.get("DAIMON_WORKSPACE_DIR")
    vault = os.environ.get("DAIMON_VAULT_DIR")
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    if ws:
        ws_path = Path(ws)
        if ws_path.is_dir():
            report.add(
                "DAIMON_WORKSPACE_DIR",
                Status.OK,
                f"set to {ws_path}",
            )
        else:
            report.add(
                "DAIMON_WORKSPACE_DIR",
                Status.FAIL,
                f"set to {ws_path} but directory does not exist",
                f"Run: mkdir -p {ws_path}  or unset DAIMON_WORKSPACE_DIR",
            )
    else:
        report.add(
            "DAIMON_WORKSPACE_DIR",
            Status.WARN,
            "not set — workspace defaults to ./vault (server-relative).",
            "The CLI sets this to os.getcwd(). If you ran daimon from the CLI "
            "and see this, the fix hasn't been loaded.",
        )

    if vault:
        report.add("DAIMON_VAULT_DIR", Status.OK, f"set to {vault}")
    else:
        report.add("DAIMON_VAULT_DIR", Status.SKIP, "not set — defaults to ./vault")

    # API key: check environ first, then .env files
    if api_key:
        masked = api_key[:4] + "…" + api_key[-4:] if len(api_key) > 8 else "****"
        report.add("DEEPSEEK_API_KEY", Status.OK, f"present in environment ({masked})")
    else:
        # Check .env files the server would read
        agents_root = Path(__file__).resolve().parents[3]  # agents/
        found_in: list[str] = []
        for env_path in (agents_root / ".env", Path.cwd() / ".env"):
            if env_path.exists():
                val = _read_dotenv_key(env_path, "DEEPSEEK_API_KEY")
                if val:
                    masked = val[:4] + "…" + val[-4:] if len(val) > 8 else "****"
                    found_in.append(f"{env_path} ({masked})")
        if found_in:
            report.add(
                "DEEPSEEK_API_KEY",
                Status.OK,
                f"found in .env: {found_in[0]}",
            )
        else:
            report.add(
                "DEEPSEEK_API_KEY",
                Status.FAIL,
                "not set in environment or .env files",
                "Add DEEPSEEK_API_KEY=<your-key> to agents/.env",
            )


def _check_settings(report: Report) -> Settings | None:
    """Parse settings and report the resolved workspace."""
    try:
        s = Settings.from_env()
    except Exception as exc:
        report.add("Settings.from_env()", Status.FAIL, str(exc))
        return None

    ws = s.resolved_workspace_dir.resolve()
    report.add(
        "Resolved workspace",
        Status.OK if ws.is_dir() else Status.WARN,
        str(ws),
        f"Run: mkdir -p {ws}" if not ws.is_dir() else "",
    )

    report.add("Model", Status.OK, s.model)
    report.add("Port", Status.OK, str(s.port))
    report.add(
        "Checkpoints DB",
        Status.OK if s.resolved_checkpoints_db.exists() else Status.WARN,
        str(s.resolved_checkpoints_db),
        "DB will be created on first turn" if not s.resolved_checkpoints_db.exists() else "",
    )
    report.add(
        "Memory DB",
        Status.OK if s.resolved_memory_db.exists() else Status.WARN,
        str(s.resolved_memory_db),
        "DB will be created on first turn" if not s.resolved_memory_db.exists() else "",
    )
    return s


async def _check_server(port: int, report: Report) -> None:
    """Check whether a server is running on *port* for this workspace. A
    workspace mismatch is no longer possible to detect here since each
    workspace has its own run dir/port — this only checks that whatever is
    on the recorded port is actually healthy."""
    try:
        timeout = aiohttp.ClientTimeout(total=3.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                if resp.status != 200:
                    report.add(
                        f"Server :{port}",
                        Status.FAIL,
                        f"/health returned HTTP {resp.status}",
                        "Check server logs for errors",
                    )
                    return
                data = await resp.json()
    except aiohttp.ClientError:
        report.add(
            f"Server :{port}",
            Status.FAIL,
            f"not reachable — no process listening on port {port}",
            "Start the server: daimon (first invocation spawns it automatically)",
        )
        return
    except asyncio.TimeoutError:
        report.add(
            f"Server :{port}",
            Status.FAIL,
            f"/health timed out — server may be hung",
            "Kill the server process and restart",
        )
        return

    ok = data.get("ok")
    server_ws = data.get("workspace")

    if ok:
        if server_ws:
            report.add(f"Server :{port}", Status.OK, f"healthy, workspace: {server_ws}")
        else:
            report.add(
                f"Server :{port}",
                Status.WARN,
                "healthy but does not report workspace (pre-0.1 server)",
                "The server is running old code. Run daimon --stop then daimon "
                "to restart with the current server.",
            )
    else:
        report.add(
            f"Server :{port}",
            Status.FAIL,
            "/health returned ok=False",
            "Kill the server and restart",
        )

    # Also check /status
    try:
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/status") as resp:
                if resp.status == 200:
                    status_data = await resp.json()
                    busy = status_data.get("busy", False)
                    active = status_data.get("active_turns", 0)
                    sessions = status_data.get("sessions", [])
                    report.add(
                        "Server status",
                        Status.OK if not busy else Status.SKIP,
                        f"busy={busy}, active_turns={active}, sessions={sessions}",
                    )
    except Exception:
        pass  # /status is optional info


def _check_pidfile(run_dir: Path, report: Report) -> None:
    """Check this workspace's CLI-owned pidfile."""
    pidfile = run_dir / "daimon-agent.pid"

    if not pidfile.exists():
        report.add("PID file", Status.SKIP, f"{pidfile} does not exist")
        return

    try:
        content = pidfile.read_text(encoding="utf-8").strip().splitlines()
        pid = int(content[0])
        port = int(content[1]) if len(content) > 1 else "?"
    except (OSError, ValueError, IndexError):
        report.add(
            "PID file",
            Status.WARN,
            f"{pidfile} exists but is corrupt",
            f"Delete: rm {pidfile}",
        )
        return

    # Check if process is alive
    try:
        os.kill(pid, 0)
        report.add("PID file", Status.OK, f"pid {pid} is alive, port {port}")
        _check_pid_process(pid, report)
    except ProcessLookupError:
        report.add(
            "PID file",
            Status.WARN,
            f"pid {pid} is dead — stale pidfile",
            f"Delete: rm {pidfile}",
        )
    except PermissionError:
        report.add("PID file", Status.OK, f"pid {pid} exists (owned by another user)")


def _check_server_logs(run_dir: Path, report: Report) -> None:
    """Show the last few lines of this workspace's server logs if they exist."""
    for log_name in ("daimon-agent.err.log", "daimon-agent.out.log"):
        log_path = run_dir / log_name
        if not log_path.exists():
            continue
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")
            # Get last 5 non-empty lines
            lines = [l for l in tail.splitlines() if l.strip()]
            last = lines[-5:]
            if last:
                report.add(
                    f"Log: {log_name}",
                    Status.SKIP,
                    f"last {len(last)} lines:\n    "
                    + "\n    ".join(last[-3:]),  # show last 3
                )
        except OSError:
            pass


def _check_port(port: int, report: Report) -> None:
    """Check if the configured port is in use and by what."""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-F", "pcn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            out = result.stdout.strip()
            report.add(
                f"Port :{port}",
                Status.OK,
                f"in use — {out}",
            )
        else:
            report.add(
                f"Port :{port}",
                Status.SKIP,
                "not in use — no process listening",
            )
    except FileNotFoundError:
        report.add(f"Port :{port}", Status.SKIP, "lsof not available")
    except subprocess.TimeoutExpired:
        report.add(f"Port :{port}", Status.SKIP, "lsof timed out")


def _check_pid_process(pid: int, report: Report) -> None:
    """Check what the PID actually is (command name, etc.)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid,comm,state="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            report.add(
                f"Process {pid}",
                Status.OK,
                result.stdout.strip(),
            )
        else:
            report.add(
                f"Process {pid}",
                Status.WARN,
                "not found — stale pidfile",
                "Run: daimon --stop",
            )
    except FileNotFoundError:
        report.add(f"Process {pid}", Status.SKIP, "ps not available")
    except subprocess.TimeoutExpired:
        report.add(f"Process {pid}", Status.SKIP, "ps timed out")


def _check_sibling_workspaces(current_run_dir: Path, report: Report) -> None:
    """Informational only — v1 keeps --stop/--doctor scoped to the current
    workspace; this just surfaces that other servers exist elsewhere."""
    root = current_run_dir.parent
    if not root.is_dir():
        return
    others = 0
    for d in root.iterdir():
        if not d.is_dir() or d == current_run_dir:
            continue
        pidfile = d / "daimon-agent.pid"
        pid = client._read_pid(pidfile) if pidfile.exists() else None
        if pid is not None and client._pid_alive(pid):
            others += 1
    if others:
        report.add(
            "Other workspaces",
            Status.SKIP,
            f"{others} other daimon workspace server(s) currently running "
            "(daimon --stop/--doctor only ever touch the current directory's server)",
        )


def _check_python(report: Report) -> None:
    """Check Python and key packages."""
    report.add("Python", Status.OK, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # ipykernel
    try:
        import jupyter_client  # noqa: F401
        report.add("ipykernel / jupyter_client", Status.OK, "available")
    except ImportError:
        report.add(
            "ipykernel / jupyter_client",
            Status.WARN,
            "not installed — kernel_execute will fail",
            "Run: pip install jupyter_client ipykernel",
        )

    # ruff
    try:
        subprocess.run(["ruff", "--version"], capture_output=True, timeout=5)
        report.add("ruff", Status.OK, "available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        report.add("ruff", Status.SKIP, "not installed — check_code will skip linting")

    # mypy
    try:
        subprocess.run(["mypy", "--version"], capture_output=True, timeout=5)
        report.add("mypy", Status.OK, "available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        report.add("mypy", Status.SKIP, "not installed — check_code will skip typechecking")

    # pytest
    try:
        subprocess.run(["pytest", "--version"], capture_output=True, timeout=5)
        report.add("pytest", Status.OK, "available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        report.add("pytest", Status.SKIP, "not installed — run_tests will fail")


def _check_dotenv(report: Report) -> None:
    """Check for .env / .daimon config files."""
    for name in (".env", ".daimon"):
        path = Path.cwd() / name
        if path.exists():
            report.add(f"Config: {name}", Status.OK, str(path))
        else:
            # Check agents/ too
            agents_path = Path(__file__).resolve().parents[3] / name
            if agents_path.exists():
                report.add(f"Config: {name}", Status.OK, str(agents_path))
            else:
                report.add(
                    f"Config: {name}",
                    Status.WARN,
                    "not found in CWD or agents/",
                    "Create a .env file with DEEPSEEK_API_KEY at minimum",
                )


def _check_cwd(report: Report) -> None:
    """Report the current working directory."""
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    shortened = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    report.add("CWD", Status.OK, shortened)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_doctor() -> Report:
    """Run all checks and return a structured report."""
    # A direct `python -m daimon_agent.cli.doctor` (or a test) may not have
    # gone through _amain's workspace default-setting — this must resolve
    # the same workspace `daimon` itself would, or every per-workspace check
    # below would be looking at the wrong run dir.
    if "DAIMON_WORKSPACE_DIR" not in os.environ:
        os.environ["DAIMON_WORKSPACE_DIR"] = os.getcwd()

    report = Report(
        cwd=os.getcwd(),
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    _check_cwd(report)
    _check_env(report)

    print("  Checking settings…", file=sys.stderr, end="\r")
    settings = _check_settings(report)

    run_dir: Path | None = None
    port: int | None = None
    if settings:
        run_dir = client.workspace_run_dir(settings.resolved_workspace_dir.resolve())
        pidfile = run_dir / "daimon-agent.pid"
        # The actual bound port is only known from the pidfile now — the
        # server may have been spawned on a dynamically-allocated port, not
        # settings.port (which is just this process's PORT env/default).
        port = client._read_port(pidfile) if pidfile.exists() else settings.port

    print("  Checking server…", file=sys.stderr, end="\r")
    if port is not None:
        await _check_server(port, report)

    print("  Checking PID file…", file=sys.stderr, end="\r")
    if run_dir is not None:
        _check_pidfile(run_dir, report)

    print("  Checking server logs…", file=sys.stderr, end="\r")
    if run_dir is not None:
        _check_server_logs(run_dir, report)

    print("  Checking port…", file=sys.stderr, end="\r")
    if port is not None:
        _check_port(port, report)

    print("  Checking other workspaces…", file=sys.stderr, end="\r")
    if run_dir is not None:
        _check_sibling_workspaces(run_dir, report)

    print("  Checking Python…", file=sys.stderr, end="\r")
    _check_python(report)

    print("  Checking config files…", file=sys.stderr, end="\r")
    _check_dotenv(report)

    print(" " * 30, file=sys.stderr, end="\r")  # clear progress line
    return report


def format_report(report: Report) -> str:
    """Format a report as a human-readable string."""
    lines: list[str] = []
    lines.append("")
    lines.append("── daimon doctor ──")
    lines.append(f"  CWD:       {report.cwd}")
    lines.append(f"  Time:      {report.timestamp}")
    lines.append("")

    # Group checks by prefix for readability
    ok_count = sum(1 for c in report.checks if c.status == Status.OK)
    warn_count = sum(1 for c in report.checks if c.status == Status.WARN)
    fail_count = sum(1 for c in report.checks if c.status == Status.FAIL)
    skip_count = sum(1 for c in report.checks if c.status == Status.SKIP)

    lines.append(f"  Results: {ok_count} ok, {warn_count} warn, "
                 f"{fail_count} fail, {skip_count} skip")
    lines.append("")

    for check in report.checks:
        marker = check.status
        lines.append(f"  {marker} {check.name}")
        if check.detail:
            lines.append(f"       {check.detail}")
        if check.recommendation:
            lines.append(f"       → {check.recommendation}")

    lines.append("")

    if fail_count > 0:
        lines.append("  ══ FAILURES ══")
        for check in report.checks:
            if check.status == Status.FAIL:
                lines.append(f"  {check.name}: {check.detail}")
                if check.recommendation:
                    lines.append(f"    → {check.recommendation}")
        lines.append("")

    # Recommended actions
    actions: list[str] = []
    for check in report.checks:
        if check.status == Status.FAIL:
            actions.append(check.recommendation) if check.recommendation else None
    if warn_count > 0:
        for check in report.checks:
            if check.status == Status.WARN and check.recommendation:
                if "restart" in check.recommendation.lower() or "--stop" in check.recommendation:
                    actions.append(check.recommendation)
    if actions:
        # Deduplicate
        seen: set[str] = set()
        unique: list[str] = []
        for a in actions:
            if a not in seen:
                seen.add(a)
                unique.append(a)
        lines.append("  ══ RECOMMENDED ACTIONS ══")
        for i, a in enumerate(unique, 1):
            lines.append(f"  {i}. {a}")
        lines.append("")

    lines.append("──")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the doctor and print the report."""
    report = asyncio.run(run_doctor())
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
