"""Coding-agent tools — kernel as primary tool, file ops, search/docs, memory.

Compared to the general agent: no browser tools (no PinchTab dependency), no
research fan-out, run_shell executes without the stage-don't-execute guard
(the coding agent's system prompt handles safety via kernel discipline).
"""

from __future__ import annotations

import asyncio
import inspect
import os
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
    ]
