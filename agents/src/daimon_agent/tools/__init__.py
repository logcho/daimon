"""The 21-tool registry. Plain names (no `mcp__daimon__` prefix — legacy's
final form), flat string-only args (the DeepSeek tool-calling mitigation:
no nested object schemas). Descriptions ported from tools.ts where the tool
survived; fresh ones in the same voice for the new file/shell/repl/skills
shapes. `research` (Phase E's fan-out) is reserved."""

from __future__ import annotations

import inspect
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..browser import build_browser
from ..memory import MemoryStore
from ..workspace import Confinement
from . import files as _files
from . import host as _host
from . import search as _search
from . import shell as _shell
from . import skills_tools as _skills
from . import web as _web
from .repl import get_repl

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
    tab_id: str = Field(description="Tab id from new_tab or list_tabs")


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


class SaveSkillArgs(BaseModel):
    name: str = Field(description="Short identifier, e.g. 'submit-job-application'")
    description: str = Field(description="A generalized, parameterized description of the procedure")
    content: str = Field(description="The full SKILL.md body — the reusable procedure itself")


class CommandArgs(BaseModel):
    command: str = Field(description="Shell command to stage in the new terminal — typed, not executed")


class ResearchArgs(BaseModel):
    queries: str = Field(
        description="One research question per line. Each line is delegated to a separate "
        "research subagent running in parallel with a research-only tool set."
    )


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

    # ---- shell + repl ----------------------------------------------------

    async def run_shell(command: str) -> str:
        return await _shell.run_shell(conf, command)

    async def python_repl(code: str) -> str:
        return await repl.execute(code)

    # ---- skills ----------------------------------------------------------

    def list_skills() -> str:
        return _skills.list_skills(settings)

    def read_skill(name: str) -> str:
        return _skills.read_skill(settings, name)

    def save_skill(name: str, description: str, content: str) -> str:
        return _skills.save_skill(settings, memory, name, description, content)

    # ---- host ------------------------------------------------------------

    def stage_terminal_command(command: str) -> str:
        return _host.stage_terminal_command(command)

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
            "Switch the background browser to an existing tab by id. Get tab ids from new_tab or "
            "list_tabs.",
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
            "Search Daimon's memory of past tasks, saved skills, and the user's vault notes for "
            "anything relevant to a topic. Use this when a request references something that may "
            "have come up before ('the job spreadsheet I made', 'like last time') or when prior "
            "context would change how you approach the task.",
            QueryArgs,
            recall,
        ),
        _tool(
            "read_file",
            "Read a file from the workspace (the vault). Paths are relative to the workspace root; "
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
            "python_repl",
            "Run Python code in the session's Jupyter kernel. State persists across calls in this "
            "session ('x = 1' then 'x + 1' works). The kernel's working directory is the workspace "
            "root.",
            CodeArgs,
            python_repl,
        ),
        _tool(
            "list_skills",
            "List the reusable skills currently in the library.",
            NoArgs,
            list_skills,
        ),
        _tool(
            "read_skill",
            "Read a skill's full SKILL.md content.",
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
            "Research a set of questions by fanning each out to a parallel research subagent "
            "(flash model, research-only tools, its own step budget). Put one research question "
            "per line — each line is delegated separately, so split independent questions apart "
            "to parallelize. The combined findings return as tool results. Use this for "
            "multi-part or open-ended investigation instead of a long single-query search.",
            ResearchArgs,
            lambda queries: "research is handled by the graph's subagent fan-out",
        ),
    ]
