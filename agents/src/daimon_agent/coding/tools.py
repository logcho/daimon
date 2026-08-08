"""Coding-agent tools — kernel as primary tool, file ops, search/docs, memory.

Compared to the general agent: no browser tools (no PinchTab dependency), no
research fan-out, run_shell executes without the stage-don't-execute guard
(the coding agent's system prompt handles safety via kernel discipline).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..memory import MemoryStore
from ..tools import files as _files
from ..tools import host as _host
from ..tools import search as _search
from ..tools.shell import command_stays_in_workspace
from ..tools import web as _web
from ..tools.repl import get_repl
from ..workspace import Confinement

# ---------------------------------------------------------------------------
# Flat string-only argument schemas (subset of the general agent's)
# ---------------------------------------------------------------------------


class NoArgs(BaseModel):
    pass


class FilePathArgs(BaseModel):
    file_path: str = Field(description="Path relative to the workspace root")


class WriteArgs(BaseModel):
    file_path: str = Field(description="Path relative to the workspace root")
    content: str = Field(description="The full content to write")


class EditArgs(BaseModel):
    file_path: str = Field(description="Path relative to the workspace root")
    old_text: str = Field(description="Exact existing text to replace")
    new_text: str = Field(description="Replacement text")


class PatternArgs(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '*.py' or 'tests/*.py'")


class GrepArgs(BaseModel):
    query: str = Field(description="Regular expression to search for")
    file_path: str = Field(default="", description="Optional subdirectory/file to search")


class CodeArgs(BaseModel):
    code: str = Field(description="Python code to run in the session's kernel")


class ShellArgs(BaseModel):
    command: str = Field(description="Shell command to run via the kernel's ! prefix")


class QueryArgs(BaseModel):
    query: str = Field(description="Search query for web or memory")


class UrlArgs(BaseModel):
    url: str = Field(description="The absolute URL to fetch")


class SkillNameArgs(BaseModel):
    name: str = Field(description="Skill name, e.g. 'deploy-workflow'")


class SaveSkillArgs(BaseModel):
    name: str = Field(description="Short identifier")
    description: str = Field(description="Generalized description of the procedure")
    content: str = Field(description="The full SKILL.md body")


class CommandArgs(BaseModel):
    command: str = Field(description="Shell command to stage — typed, not executed")


class MkdirArgs(BaseModel):
    path: str = Field(description="Directory path relative to the workspace root")


class ListDirArgs(BaseModel):
    path: str = Field(default="", description="Directory to list (empty = workspace root)")


class MoveArgs(BaseModel):
    source: str = Field(description="Source file path relative to the workspace root")
    destination: str = Field(description="Destination file path relative to the workspace root")


class CheckCodeArgs(BaseModel):
    file_path: str = Field(default="", description="File or directory to check (empty = whole workspace)")


class DebugArgs(BaseModel):
    code: str = Field(description="Python code to debug (runs in an isolated subprocess)")


class RunTestsArgs(BaseModel):
    path: str = Field(default="", description="Test file or directory to run (empty = all tests)")


def _tool(name: str, description: str, args_model: type[BaseModel], fn) -> StructuredTool:
    if inspect.iscoroutinefunction(fn):
        return StructuredTool.from_function(
            name=name, description=description, args_schema=args_model,
            func=fn, coroutine=fn,
        )
    return StructuredTool.from_function(
        name=name, description=description, args_schema=args_model, func=fn,
    )


def build_coding_tools(
    settings: Any,
    *,
    memory: MemoryStore | None = None,
    session_id: str = "default",
) -> list[BaseTool]:
    """Build the coding agent's tool set: kernel, file ops, shell, search, memory."""
    conf = Confinement(settings.resolved_workspace_dir)
    provider = _search.build_search_provider(settings)
    fetcher = _web.build_web_fetcher(settings)
    repl = get_repl(session_id, settings.resolved_workspace_dir)

    # ---- kernel (primary tool) --------------------------------------------

    async def kernel_execute(code: str) -> str:
        """Run Python code in the session's persistent IPython kernel."""
        return await repl.execute(code)

    # ---- shell (executes workspace-confined commands; blocks the rest) ----

    _SECRET_ENV = {"DEEPSEEK_API_KEY", "PINCHTAB_TOKEN", "TAVILY_API_KEY"}

    async def run_shell(command: str) -> str:
        """Run a shell command. Workspace-confined commands execute directly.
        Commands that could reach outside (credentials, remote hosts, absolute
        paths elsewhere) are blocked — use stage_terminal_command instead so
        the user can review and run them."""
        reason = command_stays_in_workspace(command, conf.root)
        if reason is not None:
            return (
                f'Safety: command was not executed — {reason}. '
                f'Use stage_terminal_command to stage it for the user instead.'
            )
        env = dict(os.environ)
        for key in _SECRET_ENV:
            env.pop(key, None)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(conf.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            output = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Command timed out after 120s and was killed."
        stdout = (output[0] or b"").decode("utf-8", errors="replace")
        if len(stdout) > 8000:
            stdout = stdout[:8000] + "\n…(output truncated)"
        if proc.returncode != 0:
            return f"Command exited with code {proc.returncode}:\n{stdout}"
        return stdout or "(no output)"

    # ---- file ops (same as general agent) ---------------------------------

    def read_file(file_path: str) -> str:
        return _files.read_file(conf, file_path)

    def write_file(file_path: str, content: str) -> str:
        return _files.write_file(conf, file_path, content)

    def edit_file(file_path: str, old_text: str, new_text: str) -> str:
        return _files.edit_file(conf, file_path, old_text, new_text)

    def glob_files(pattern: str) -> str:
        return _files.glob_files(conf, pattern)

    def grep_files(query: str, file_path: str) -> str:
        return _files.grep_files(conf, query, file_path)

    # ---- search / docs ----------------------------------------------------

    async def web_search(query: str) -> str:
        results = await provider.search(query)
        if not results:
            return f'No search results for "{query}".'
        return "\n".join(
            f"{i + 1}. {r.title} — {r.url} — {r.snippet}"
            for i, r in enumerate(results)
        )

    async def web_fetch(url: str) -> str:
        return await fetcher.fetch(url)

    # ---- memory ------------------------------------------------------------

    def recall(query: str) -> str:
        if memory is None:
            return "Memory is not available in this session."
        context = memory.recall(query)
        return context or f'Nothing in memory matched "{query}".'

    # ---- skills ------------------------------------------------------------

    from ..tools import skills_tools as _skills

    def list_skills() -> str:
        return _skills.list_skills(settings)

    def read_skill(name: str) -> str:
        return _skills.read_skill(settings, name)

    def save_skill(name: str, description: str, content: str) -> str:
        return _skills.save_skill(settings, memory, name, description, content)

    # ---- host --------------------------------------------------------------

    def stage_terminal_command(command: str) -> str:
        return _host.stage_terminal_command(command)

    # ---- new tools: directory ops, code quality, debugging, tests ----------

    def mkdir(path: str) -> str:
        """Create a directory (and any parents) in the workspace."""
        target = (conf.root / path).resolve()
        if not str(target).startswith(str(conf.root.resolve())):
            return f"Error: path \"{path}\" is outside the workspace."
        target.mkdir(parents=True, exist_ok=True)
        rel = target.relative_to(conf.root)
        return f"Created directory \"{rel}\"."

    def list_directory(path: str) -> str:
        """List contents of a directory in the workspace."""
        target = conf.root / path if path else conf.root
        target = target.resolve()
        if not str(target).startswith(str(conf.root.resolve())):
            return f"Error: path \"{path}\" is outside the workspace."
        if not target.exists():
            return f"Error: \"{path or '.'}\" does not exist."
        if not target.is_dir():
            return f"Error: \"{path or '.'}\" is not a directory."
        lines: list[str] = []
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return f"Error: permission denied reading \"{path or '.'}\"."
        for entry in entries:
            try:
                st = entry.stat()
            except OSError:
                lines.append(f"??? {entry.name}")
                continue
            if entry.is_dir():
                lines.append(f"drwxr-xr-x {entry.name}/")
            else:
                size = st.st_size
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size / 1024:.1f}K"
                else:
                    size_str = f"{size / (1024 * 1024):.1f}M"
                lines.append(f"-rw-r--r-- {size_str:>6} {entry.name}")
        if not lines:
            return "(empty directory)"
        return "\n".join(lines)

    def delete_file(file_path: str) -> str:
        """Delete a workspace file. Refuses to delete directories."""
        target = (conf.root / file_path).resolve()
        if not str(target).startswith(str(conf.root.resolve())):
            return f"Error: path \"{file_path}\" is outside the workspace."
        if not target.exists():
            return f"Error: \"{file_path}\" does not exist."
        if target.is_dir():
            return f"Error: \"{file_path}\" is a directory — use run_shell with `rm -r` instead."
        target.unlink()
        return f"Deleted \"{file_path}\"."

    def move_file(source: str, destination: str) -> str:
        """Move or rename a workspace file."""
        src = (conf.root / source).resolve()
        dst = (conf.root / destination).resolve()
        if not str(src).startswith(str(conf.root.resolve())):
            return f"Error: source \"{source}\" is outside the workspace."
        if not str(dst).startswith(str(conf.root.resolve())):
            return f"Error: destination \"{destination}\" is outside the workspace."
        if not src.exists():
            return f"Error: source \"{source}\" does not exist."
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f"Moved \"{source}\" → \"{destination}\"."

    async def check_code(file_path: str) -> str:
        """Run ruff (lint) and mypy (typecheck) on workspace code."""
        target = conf.root / file_path if file_path else conf.root
        cwd = str(conf.root)
        results: list[str] = []

        # ruff check — fast linting
        try:
            proc = await asyncio.create_subprocess_exec(
                "ruff", "check", str(target.relative_to(conf.root)) if file_path else ".",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            out = (stdout or b"").decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                results.append("✓ ruff: no issues found")
            else:
                results.append(f"✗ ruff (exit {proc.returncode}):\n{out}" if out else f"✗ ruff (exit {proc.returncode})")
        except FileNotFoundError:
            results.append("⚠ ruff not installed — run `pip install ruff`")
        except asyncio.TimeoutError:
            results.append("⚠ ruff timed out")

        # mypy — type checking
        try:
            proc = await asyncio.create_subprocess_exec(
                "mypy", str(target.relative_to(conf.root)) if file_path else ".",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            out = (stdout or b"").decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                results.append("✓ mypy: no type errors found")
            else:
                results.append(f"✗ mypy (exit {proc.returncode}):\n{out}" if out else f"✗ mypy (exit {proc.returncode})")
        except FileNotFoundError:
            results.append("⚠ mypy not installed — run `pip install mypy`")
        except asyncio.TimeoutError:
            results.append("⚠ mypy timed out")

        return "\n\n".join(results) if results else "No checks available."

    async def debug(code: str) -> str:
        """Run Python code in an isolated subprocess and capture structured
        debugging output on failure: exception type, message, and traceback
        with local variables at each frame."""
        script = (
            "import sys, traceback, pprint\n"
            "try:\n"
            + "\n".join(f"    {line}" for line in code.split("\n"))
            + "\n"
            "except Exception as exc:\n"
            "    print(f'EXCEPTION: {type(exc).__name__}: {exc}', file=sys.stderr)\n"
            "    tb = traceback.TracebackException.from_exception(exc, capture_locals=True)\n"
            "    print(''.join(tb.format()), file=sys.stderr)\n"
            "    sys.exit(1)\n"
        )
        env = dict(os.environ)
        for key in _SECRET_ENV:
            env.pop(key, None)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script,
            cwd=str(conf.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "Debug execution timed out after 30s and was killed."

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return f"Debug — execution failed:\n\n{err}{out}" if err else f"Debug — execution failed (exit {proc.returncode}):\n{out}"
        return out or "(no output — code ran successfully)"

    async def run_tests(path: str) -> str:
        """Run pytest in the workspace. Falls back if pytest isn't installed."""
        cwd = str(conf.root)
        args = ["pytest", "-x", "-q"]
        if path:
            args.append(path)
        else:
            args.append(".")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            out = (stdout or b"").decode("utf-8", errors="replace")
            if len(out) > 8000:
                out = out[:8000] + "\n…(output truncated)"
            if proc.returncode == 0:
                return f"✓ All tests passed.\n\n{out}" if out.strip() else "✓ All tests passed."
            return f"✗ Tests failed (exit {proc.returncode}):\n{out}"
        except FileNotFoundError:
            return "pytest not installed — run `pip install pytest`"
        except asyncio.TimeoutError:
            return "Tests timed out after 120s."

    # ---- tool list ---------------------------------------------------------

    return [
        _tool(
            "kernel_execute",
            "Run Python code in the session's persistent IPython kernel. State persists "
            "across calls: variables, imports, and definitions set in one call are still "
            "there in the next. Use this as your primary tool for computation, data work, "
            "testing, and exploring code. Shell commands run via `!command` prefix.",
            CodeArgs,
            kernel_execute,
        ),
        _tool(
            "run_shell",
            "Run a shell command in the workspace. Commands execute directly — use the "
            "kernel's `!` prefix for quick commands, and this for standalone shell work "
            "(git, builds, test runners). Commands that would reach outside the workspace "
            "are blocked and must be staged instead.",
            ShellArgs,
            run_shell,
        ),
        _tool(
            "read_file",
            "Read a file from the workspace. Always read before editing so the old_text "
            "matches exactly.",
            FilePathArgs,
            read_file,
        ),
        _tool(
            "write_file",
            "Write (or overwrite) a file in the workspace. Creates parent directories.",
            WriteArgs,
            write_file,
        ),
        _tool(
            "edit_file",
            "Replace one occurrence of exact text in a workspace file. Read the file "
            "first to get the exact content — the match is byte-identical.",
            EditArgs,
            edit_file,
        ),
        _tool(
            "glob_files",
            "List files matching a glob pattern (e.g. '*.py', 'tests/**/*.py').",
            PatternArgs,
            glob_files,
        ),
        _tool(
            "grep_files",
            "Search workspace files for a regex and return matching lines with line numbers.",
            GrepArgs,
            grep_files,
        ),
        _tool(
            "web_search",
            "Search the web for docs, API references, error messages, and examples. "
            "Use the top result's URL with web_fetch to read the full page.",
            QueryArgs,
            web_search,
        ),
        _tool(
            "web_fetch",
            "Fetch a URL and return its content as clean Markdown. Use for reading "
            "docs and references found via web_search.",
            UrlArgs,
            web_fetch,
        ),
        _tool(
            "recall",
            "Search Daimon's memory for past tasks, skills, and vault notes relevant "
            "to the current coding task.",
            QueryArgs,
            recall,
        ),
        _tool(
            "list_skills",
            "List available reusable skills.",
            NoArgs,
            list_skills,
        ),
        _tool(
            "read_skill",
            "Read a skill's full content.",
            SkillNameArgs,
            read_skill,
        ),
        _tool(
            "save_skill",
            "Save a reusable procedure as a skill for future coding tasks.",
            SaveSkillArgs,
            save_skill,
        ),
        _tool(
            "stage_terminal_command",
            "Open a terminal tab with a command typed but NOT executed — the user "
            "presses Enter themselves. Use for dev servers, interactive TUIs, installs, "
            "or commands the safety check blocks.",
            CommandArgs,
            stage_terminal_command,
        ),
        # ---- new: directory ops, code quality, debugging, tests -------------
        _tool(
            "mkdir",
            "Create a directory (and any parent directories) in the workspace.",
            MkdirArgs,
            mkdir,
        ),
        _tool(
            "list_directory",
            "List the contents of a workspace directory. Shows names, sizes, and "
            "type markers (dir/ suffix). Like `ls -la`.",
            ListDirArgs,
            list_directory,
        ),
        _tool(
            "delete_file",
            "Delete a file from the workspace. Refuses to delete directories — "
            "use run_shell with `rm -r` for that.",
            FilePathArgs,
            delete_file,
        ),
        _tool(
            "move_file",
            "Move or rename a workspace file. Creates parent directories of the "
            "destination if needed.",
            MoveArgs,
            move_file,
        ),
        _tool(
            "check_code",
            "Run ruff (lint) and mypy (typecheck) on workspace code. Pass a "
            "specific file or directory, or omit the argument to check everything.",
            CheckCodeArgs,
            check_code,
        ),
        _tool(
            "debug",
            "Run Python code in an isolated subprocess and return a structured "
            "debugging report. On failure: exception type, message, and traceback "
            "with local variables at each frame. Use to understand *why* code "
            "is failing before editing.",
            DebugArgs,
            debug,
        ),
        _tool(
            "run_tests",
            "Run pytest in the workspace. Pass a specific test file or directory, "
            "or omit the argument to run all tests. Falls back gracefully if pytest "
            "isn't installed.",
            RunTestsArgs,
            run_tests,
        ),
    ]
