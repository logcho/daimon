"""The tool registry. Plain names (no `mcp__daimon__` prefix — legacy's
final form), flat string-only args (the DeepSeek tool-calling mitigation:
no nested object schemas). Descriptions ported from tools.ts where the tool
survived; fresh ones in the same voice for the new file/shell/repl/skills
shapes.

Four tools here have no working body on purpose. `research` and `task` are
intercepted by the graph, which turns them into concurrent sub-agent runs;
`ask_user` and `present_plan` are resolved by a LangGraph interrupt before the
tools node executes anything. They are declared here so their schemas reach the
model — the graph is where they actually happen."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..browser import build_browser
from ..emitter import emit
from ..events import ui_action_event
from ..memory import MemoryStore
from ..workspace import Confinement
from . import ask as _ask
from . import files as _files
from . import host as _host
from . import search as _search
from . import skills_tools as _skills
from . import todo as _todo
from . import web as _web
from .repl import get_repl
from . import shell as _shell

BROWSER_UNAVAILABLE = (
    "The background browser is not available — PinchTab isn't configured for "
    "this session (run scripts/setup-pinchtab.sh)."
)

# ---------------------------------------------------------------------------
# Flat string-only argument schemas
# ---------------------------------------------------------------------------

class NoArgs(BaseModel):
    pass


class UrlArgs(BaseModel):
    url: str = Field(description="The absolute URL to open")


class RefArgs(BaseModel):
    ref: str = Field(description="Element ref from read_page's listing, e.g. 'e5'")


class RefTextArgs(BaseModel):
    ref: str = Field(description="Element ref from read_page's listing, e.g. 'e3'")
    text: str = Field(description="The text to type into the field")


class TabArgs(BaseModel):
    tab_id: str = Field(description="Tab id from new_tab")


class QueryArgs(BaseModel):
    query: str = Field(description="The search query")


class FilePathArgs(BaseModel):
    file_path: str = Field(description="Path relative to the workspace root, or an absolute path inside it")


class WriteArgs(BaseModel):
    file_path: str = Field(description="Path relative to the workspace root, or an absolute path inside it")
    content: str = Field(description="The full content to write")


class EditArgs(BaseModel):
    file_path: str = Field(description="Path relative to the workspace root, or an absolute path inside it")
    old_text: str = Field(description="Exact existing text to replace (read_file first to get it exactly)")
    new_text: str = Field(description="Replacement text")


class PatternArgs(BaseModel):
    pattern: str = Field(description="Glob pattern relative to the workspace root, e.g. '*.md' or 'notes/*.txt'")


class GrepArgs(BaseModel):
    query: str = Field(description="Regular expression to search for")
    file_path: str = Field(default="", description="Optional subdirectory/file to search; empty searches the whole workspace")


class ShellArgs(BaseModel):
    command: str = Field(description="Shell command to run in the workspace — commands that could escape are staged instead")


class CodeArgs(BaseModel):
    code: str = Field(description="Python code to run in the session's kernel")


class SkillNameArgs(BaseModel):
    name: str = Field(description="Skill name, e.g. 'submit-job-application'")
    file: str = Field(
        default="",
        description="Optional file bundled with the skill (e.g. 'reference.md'), "
        "relative to the skill's own directory. Empty reads its SKILL.md.",
    )


class SaveSkillArgs(BaseModel):
    name: str = Field(description="Short identifier, e.g. 'submit-job-application'")
    description: str = Field(
        description="One line saying when this skill applies — it is all you see when "
        "deciding whether to read the skill later, so make it specific"
    )
    content: str = Field(description="The full SKILL.md body — the reusable procedure itself")
    scope: str = Field(
        default="vault",
        description="'vault' (default) to keep it across all projects, or 'project' "
        "to store it with this repo in .daimon/skills so it can be committed",
    )


class CommandArgs(BaseModel):
    command: str = Field(description="Shell command to stage in the new terminal — typed, not executed")


class ResearchArgs(BaseModel):
    queries: str = Field(
        description="One research question per line. Each line is delegated to a separate "
        "research subagent, all of which run concurrently with a research-only tool set."
    )


class TaskArgs(BaseModel):
    agent_type: str = Field(
        description="Which kind of sub-agent: 'explore' (read-only code search), "
        "'research' (web), or 'general' (full tool set)."
    )
    prompt: str = Field(
        description="The complete task for the sub-agent. It shares no context with you, "
        "so state the goal, the constraints, and exactly what to report back."
    )


class FindSkillsArgs(BaseModel):
    query: str = Field(
        description="What the skill should do, in a few words — e.g. 'fill pdf forms'"
    )


class TodoArgs(BaseModel):
    todos: str = Field(
        description="The whole list, one item per line as 'status|task', where status is "
        "pending, in_progress, or done. Replaces the previous list."
    )


class AskUserArgs(BaseModel):
    question: str = Field(description="The question, phrased so it can be answered directly")
    options: str = Field(
        default="",
        description="One option per line as 'label|short description'. 2-4 options, best first.",
    )
    header: str = Field(
        default="", description="Two or three words naming the choice, e.g. 'Auth method'"
    )
    multi_select: str = Field(
        default="", description="'true' when several options can be chosen together"
    )


class PresentPlanArgs(BaseModel):
    plan: str = Field(description="The plan in markdown — steps, files touched, assumptions")
    question: str = Field(
        default="", description="What you're asking them to decide (defaults to approval)"
    )
    options: str = Field(
        default="",
        description="One option per line as 'label|short description'. Empty = approve/revise.",
    )


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
    """Wrap `fn` as a StructuredTool. Async functions get `coroutine` set so
    `ainvoke` actually awaits them; without it, LangChain calls the sync
    `func` path, which returns an unawaited coroutine object for async defs."""
    if inspect.iscoroutinefunction(fn):
        return StructuredTool.from_function(
            name=name, description=description, args_schema=args_model,
            func=fn, coroutine=fn,
        )
    return StructuredTool.from_function(
        name=name, description=description, args_schema=args_model, func=fn,
    )


def build_tools(settings: Any, *, memory: MemoryStore | None = None, session_id: str = "default") -> list[BaseTool]:
    conf = Confinement(settings.resolved_workspace_dir)
    browser = build_browser(settings)
    provider = _search.build_search_provider(settings)
    fetcher = _web.build_web_fetcher(settings)
    repl = get_repl(session_id, settings.resolved_workspace_dir)

    # ---- background browser (PinchTab) -----------------------------------

    async def open_url(url: str) -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        title = await browser.open_url(url)
        return f"Opened {url}. Page title: {title}"

    async def read_page() -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        page = await browser.read_page()
        if not page.elements:
            return page.text or "(empty page)"
        lines = "\n".join(f'{el.ref}: {el.role} "{el.label}"' for el in page.elements)
        return f"{page.text}\n\nInteractive elements:\n{lines}"

    async def click(ref: str) -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        await browser.click(ref)
        return f'Clicked element "{ref}".'

    async def fill_field(ref: str, text: str) -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        await browser.fill(ref, text)
        return f'Filled element "{ref}" with the given text.'

    async def new_tab() -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        tab_id = await browser.new_tab()
        return f"Opened a new blank tab (id {tab_id})."

    async def switch_tab(tab_id: str) -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        await browser.switch_tab(tab_id)
        return f"Switched to tab {tab_id}."

    async def close_tab(tab_id: str) -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        await browser.close_tab(tab_id)
        return f"Closed tab {tab_id}."

    async def extract_text() -> str:
        if browser is None:
            return BROWSER_UNAVAILABLE
        text = await browser.extract_text()
        return text or "(no readable content on this page)"

    # ---- search ----------------------------------------------------------

    async def web_search(query: str) -> str:
        results = await provider.search(query)
        if not results:
            return f'No search results found for "{query}". Try a different, more specific query.'
        return "\n".join(f"{i + 1}. {r.title} — {r.url} — {r.snippet}" for i, r in enumerate(results))

    async def web_fetch(url: str) -> str:
        return await fetcher.fetch(url)

    # ---- memory ----------------------------------------------------------

    def recall(query: str) -> str:
        if memory is None:
            return "Memory is not available in this session."
        context = memory.recall(query)
        return context or f'Nothing in memory matched "{query}".'

    # ---- workspace files -------------------------------------------------

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

    # ---- shell + kernel --------------------------------------------------

    async def run_shell(command: str) -> str:
        """Run a shell command. Workspace-confined commands execute directly.
        Commands that could reach outside (credentials, remote hosts, absolute
        paths elsewhere) are blocked — use stage_terminal_command instead so
        the user can review and run them."""
        return await _shell.run_shell(conf, command)

    async def kernel_execute(code: str) -> str:
        """Run Python code in the session's persistent IPython kernel."""
        return await repl.execute(code)

    # ---- skills ----------------------------------------------------------

    def list_skills() -> str:
        return _skills.list_skills(settings)

    def read_skill(name: str, file: str = "") -> str:
        return _skills.read_skill(settings, name, file)

    def save_skill(name: str, description: str, content: str, scope: str = "vault") -> str:
        return _skills.save_skill(settings, memory, name, description, content, scope)

    # ---- host ------------------------------------------------------------

    def stage_terminal_command(command: str) -> str:
        return _host.stage_terminal_command(command)

    # ---- directory ops, code quality, debugging, tests -------------------

    def mkdir(path: str) -> str:
        """Create a directory (and any parents) in the workspace."""
        target = (conf.root / path).resolve()
        if not str(target).startswith(str(conf.root.resolve())):
            return f'Error: path "{path}" is outside the workspace.'
        target.mkdir(parents=True, exist_ok=True)
        rel = target.relative_to(conf.root)
        return f'Created directory "{rel}".'

    def list_directory(path: str) -> str:
        """List contents of a directory in the workspace."""
        target = conf.root / path if path else conf.root
        target = target.resolve()
        if not str(target).startswith(str(conf.root.resolve())):
            return f'Error: path "{path}" is outside the workspace.'
        if not target.exists():
            return f'Error: "{path or "."}" does not exist.'
        if not target.is_dir():
            return f'Error: "{path or "."}" is not a directory.'
        lines: list[str] = []
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return f'Error: permission denied reading "{path or "."}".'
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
            return f'Error: path "{file_path}" is outside the workspace.'
        if not target.exists():
            return f'Error: "{file_path}" does not exist.'
        if target.is_dir():
            return f'Error: "{file_path}" is a directory — use run_shell with `rm -r` instead.'
        target.unlink()
        return f'Deleted "{file_path}".'

    def move_file(source: str, destination: str) -> str:
        """Move or rename a workspace file."""
        src = (conf.root / source).resolve()
        dst = (conf.root / destination).resolve()
        if not str(src).startswith(str(conf.root.resolve())):
            return f'Error: source "{source}" is outside the workspace.'
        if not str(dst).startswith(str(conf.root.resolve())):
            return f'Error: destination "{destination}" is outside the workspace.'
        if not src.exists():
            return f'Error: source "{source}" does not exist.'
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f'Moved "{source}" → "{destination}".'

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
        for key in _shell._SECRET_ENV:
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

    async def find_skills(query: str) -> str:
        """Search the public registry for a skill that already does this."""
        from ..skills.registry import RegistryError, SkillRegistry

        registry = SkillRegistry(settings.registry_cache_dir)
        try:
            hits = await registry.search(query, limit=8)
        except RegistryError as exc:
            return f"Skill search unavailable: {exc}"
        if not hits:
            return (
                f'No published skill matches "{query}". Write the procedure '
                f"yourself, and save it with save_skill if it's worth reusing."
            )
        lines = [
            f'{len(hits)} published skill(s) matching "{query}" '
            f"(★ = human-curated). You cannot install these yourself — tell the "
            f"user the slug and that `/skills install <slug>` adds it:",
        ]
        for hit in hits:
            mark = "★" if hit.featured else "-"
            lines.append(
                f"{mark} {hit.slug} ({hit.repo}, {hit.stars:,}★): {hit.description[:160]}"
            )
        return "\n".join(lines)

    def update_todos(todos: str) -> str:
        """Replace this session's visible task list and broadcast it."""
        return _todo.set_todos(session_id, todos)

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

    return [
        _tool(
            "open_url",
            "Navigate the background browser to a URL and return the page title.",
            UrlArgs,
            open_url,
        ),
        _tool(
            "read_page",
            "Read the visible text of the current page in the background browser, plus a listing of "
            "its interactive elements as (ref, role, label). Use the ref shown here — not a CSS "
            "selector — with click/fill_field.",
            NoArgs,
            read_page,
        ),
        _tool(
            "click",
            "Click an element (button, link, checkbox, ...) in the background browser. `ref` is an "
            "element reference from read_page's listing (e.g. 'e5'), NOT a CSS selector. Refs are "
            "renumbered on every snapshot, so they go stale after any navigation, click, or fill — "
            "call read_page again before reusing one rather than assuming it still points at the "
            "same element.",
            RefArgs,
            click,
        ),
        _tool(
            "fill_field",
            "Type text into a form field in the background browser. `ref` is an element reference "
            "from read_page's listing (e.g. 'e3'), NOT a CSS selector. Refs go stale after any "
            "navigation, click, or fill — call read_page again before reusing one.",
            RefTextArgs,
            fill_field,
        ),
        _tool(
            "new_tab",
            "Open a fresh blank tab in the background browser and make it the current page.",
            NoArgs,
            new_tab,
        ),
        _tool(
            "switch_tab",
            "Switch the background browser to an existing tab by id. Tab ids come from new_tab.",
            TabArgs,
            switch_tab,
        ),
        _tool(
            "close_tab",
            "Close a tab in the background browser by id.",
            TabArgs,
            close_tab,
        ),
        _tool(
            "extract_text",
            "Extract the main article text of the current page in the background browser "
            "(Readability-style — drops navigation chrome). Use for long-form pages where "
            "read_page's raw text is too noisy.",
            NoArgs,
            extract_text,
        ),
        _tool(
            "web_search",
            "Search the web and get back a numbered list of results (title, URL, snippet). Use this "
            "when you don't already know the specific URL you need, instead of guessing one, then "
            "open_url or web_fetch the most relevant result.",
            QueryArgs,
            web_search,
        ),
        _tool(
            "web_fetch",
            "Fetch a URL and return its main content as clean Markdown. Works without the "
            "background browser — no PinchTab needed. Use this to read a specific page when you "
            "already have the URL (e.g. from a web_search result). For interactive browsing "
            "(clicking, filling forms), use open_url + read_page + click + fill_field instead.",
            UrlArgs,
            web_fetch,
        ),
        _tool(
            "recall",
            "Search Daimon's memory of past tasks, saved skills, and the user's notes for "
            "anything relevant to a topic. Use this when a request references something that may "
            "have come up before ('the job spreadsheet I made', 'like last time') or when prior "
            "context would change how you approach the task.",
            QueryArgs,
            recall,
        ),
        _tool(
            "read_file",
            "Read a file from the workspace. Paths are relative to the workspace root; "
            "anything resolving outside it is blocked.",
            FilePathArgs,
            read_file,
        ),
        _tool(
            "write_file",
            "Write (or overwrite) a file in the workspace. Creates parent directories as needed.",
            WriteArgs,
            write_file,
        ),
        _tool(
            "edit_file",
            "Replace one occurrence of exact text in a workspace file. Use read_file first to get "
            "the exact content — the match must be byte-identical.",
            EditArgs,
            edit_file,
        ),
        _tool(
            "glob_files",
            "List files in the workspace matching a glob pattern ('*.md' matches at any depth; "
            "'notes/*.txt' matches by directory).",
            PatternArgs,
            glob_files,
        ),
        _tool(
            "grep_files",
            "Search workspace files for a regular expression and return matching lines with "
            "line numbers.",
            GrepArgs,
            grep_files,
        ),
        _tool(
            "run_shell",
            "Run a shell command in the workspace. Commands that could reach outside it (absolute "
            "paths elsewhere, '..', home dir, credential or network verbs) are staged in a terminal "
            "tab for the user instead and never executed. When in doubt, stage.",
            ShellArgs,
            run_shell,
        ),
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
            "list_skills",
            "List the reusable skills currently in the library.",
            NoArgs,
            list_skills,
        ),
        _tool(
            "read_skill",
            "Read a skill's SKILL.md, or one of the files bundled with it. A skill is a "
            "directory: many ship reference documents and scripts that their SKILL.md tells "
            "you to open. Reading a skill lists what else is in it and where it lives on "
            "disk; pass `file` to read one of those.",
            SkillNameArgs,
            read_skill,
        ),
        _tool(
            "save_skill",
            "Save a reusable, generalized procedure as a skill so a similar future request can be "
            "handled faster. Only for genuinely reusable procedures, not one-off tasks.",
            SaveSkillArgs,
            save_skill,
        ),
        _tool(
            "find_skills",
            "Search the public registry of published skills for one that already does what "
            "you're about to write. Worth a call before working out any non-trivial reusable "
            "procedure — someone has often already written it. Returns names, descriptions "
            "and source repos. You cannot install them: report the slug to the user, who "
            "installs it with `/skills install <slug>` after reviewing what it contains.",
            FindSkillsArgs,
            find_skills,
        ),
        _tool(
            "stage_terminal_command",
            "Open a new terminal tab in Daimon's own UI and type a shell command into it WITHOUT "
            "running it — the user presses Enter themselves. Use this when the user asks you to "
            "open or use their terminal, or to navigate somewhere and launch something there. "
            "Combine multiple steps into one && -joined command line rather than calling this more "
            "than once for the same request. This never executes anything on its own.",
            CommandArgs,
            stage_terminal_command,
        ),
        _tool(
            "research",
            "Research a set of questions by fanning each out to its own research subagent "
            "(flash model, research-only tools, its own step budget). Put one research question "
            "per line — each line is delegated separately and they all run at the same time, so "
            "splitting independent questions apart costs nothing extra. The combined findings "
            "return as tool results. Use this for multi-part or open-ended investigation "
            "instead of a long single-query search.",
            ResearchArgs,
            lambda queries: "research is handled by the graph's subagent fan-out",
        ),
        _tool(
            "task",
            "Delegate a self-contained piece of work to a sub-agent and get back only its "
            "conclusion — the sub-agent's own tool output never enters your context, which is "
            "what makes this worth doing for anything that would take many reads. "
            "agent_type picks its tools: 'explore' reads and searches code, 'research' browses "
            "the web, 'general' gets the full set. The sub-agent starts fresh with no memory of "
            "this conversation, so the prompt must be complete on its own and say what to report. "
            "Issue several task calls in one message to run them concurrently. Sub-agents cannot "
            "spawn further sub-agents or ask the user questions.",
            TaskArgs,
            lambda agent_type, prompt: "task is handled by the graph's subagent fan-out",
        ),
        _tool(
            "update_todos",
            _todo.UPDATE_TODOS_DESCRIPTION,
            TodoArgs,
            update_todos,
        ),
        # ---- conferring with the user ---------------------------------------
        # Bodies are unreachable: the graph resolves both via interrupt() before
        # the tools node executes anything. They exist so the schema reaches the
        # model, and are only offered when the client says it can answer.
        _tool(
            "ask_user",
            _ask.ASK_USER_DESCRIPTION,
            AskUserArgs,
            lambda question, options="", header="", multi_select="": (
                "ask_user is resolved by the graph's interrupt"
            ),
        ),
        _tool(
            "present_plan",
            _ask.PRESENT_PLAN_DESCRIPTION,
            PresentPlanArgs,
            lambda plan, question="", options="": (
                "present_plan is resolved by the graph's interrupt"
            ),
        ),
        # ---- directory ops, code quality, debugging, tests -------------------
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
