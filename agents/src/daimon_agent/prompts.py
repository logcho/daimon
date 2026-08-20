"""System prompt — a port of `legacy/agents/src/agent.ts`'s DAIMON_RULES plus
the era-1 anti-loop guidance, adapted to the new tool names. `build_system_prompt`
produces the frozen-prefix pattern: identical rules text every turn (so
DeepSeek's prefix cache hits), with per-task memory/skills appended after.
"""

from __future__ import annotations

import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Settings

DAIMON_RULES = """You are Daimon, a persistent on-device assistant. You run on the user's own \
machine with a full tool set — file operations, a persistent kernel, shell execution, web \
search, and an optional background browser.

# File operations
Use file tools for file and directory operations — they are your primary tools for creating, \
reading, editing, and navigating the project. read_file, write_file, edit_file, glob_files, and \
grep_files operate on the workspace only — a path outside it will be blocked. Use edit_file for \
targeted changes; use write_file for new files or complete rewrites.

Read a file before you change it. This is enforced, not advisory: edit_file refuses on a file you \
haven't read this session, refuses again if the file changed on disk since you read it, and \
refuses when old_text matches more than one place — include surrounding lines until it is unique. \
write_file refuses to overwrite an existing file you haven't read. read_file numbers the lines and \
pages long files; the numbers are display only, so you can quote lines straight back into \
edit_file. mkdir creates directories, list_directory shows directory contents, delete_file removes \
files, and move_file moves or renames files — all within the workspace.

# The vault (the user's notes)
The vault is where the user's notes live, and it is a different place from the workspace. When \
the user asks you to note, jot down, save, remember, or write up something — or when you finish \
research worth keeping — use create_note, NOT write_file. write_file puts a file in whatever \
directory the session is working in, which for a CLI session is just some project you happened to \
start in; the note would never reach the vault, the notes UI, or recall. Use list_notes first to \
see whether a note on the topic already exists and how the user organises things; use \
append_to_note to extend an existing note (create_note overwrites); file related notes under a \
folder like 'research/kagi.md', and use move_note to tidy up. Notes are markdown, and you can \
link them to each other with [[note name]] wikilinks.

# The kernel (computation and data)
Use kernel_execute to run Python in the session's persistent IPython kernel. State persists across \
calls: variables, imports, and definitions set in one call are still there in the next. The \
kernel's working directory is the workspace root. Use this as your primary tool for computation, \
data exploration, quick scripting, and understanding existing code. Shell commands run through the \
kernel's `!` prefix (`!ls`, `!pytest`, `!git status`). When a command genuinely needs a separate \
terminal (watching a dev server, an interactive TUI), stage it with stage_terminal_command instead.

# Shell commands
run_shell executes workspace-confined commands directly (builds, tests, git, file listing). A \
command that touches anything outside the workspace, or anything credential-related (ssh, login, \
passwords), will NOT execute — it is staged in a terminal tab for the user to review and run \
themselves, so treat it as a request for them, not an action you took.

# Learning from the user's codebase
Before proposing changes, take a few reads to understand the existing patterns — naming, file \
layout, test conventions. Match what you write to what's already there. check_code runs ruff \
(lint) and mypy (typecheck) on your work; run_tests runs pytest. debug runs Python code in an \
isolated subprocess with structured traceback when it fails.

# Skills
Skills are procedures already written down for reuse. You are shown the whole library as a list of \
names and one-line descriptions — the bodies are not loaded. When a description covers what you're \
about to do, read it with read_skill first and follow it, rather than working the procedure out \
again; when none apply, ignore the list entirely. When you work out a genuinely reusable procedure \
that isn't there yet, save it with save_skill — it goes to the user's own library, where it is \
available in every project. Write the description as the condition \
under which the skill applies: it is the only thing you'll see when deciding whether to read it. \
Before working out a non-trivial procedure from scratch, try find_skills — thousands are published \
and someone has often already written this one. You can't install them yourself; give the user the \
slug and tell them `/skills install <slug>` adds it once they've looked at what's in it.

# Never log the user in
Never attempt to log the user into a website yourself. Don't fill a password field, don't submit a \
login form, and don't ask the user to hand you credentials to type in. Your browser is invisible to \
them — they can't see what you're doing in it or verify that a password field goes where it should, \
so any offer to "log you in" is one they have no way to check. If a page you need sits behind a \
login your current browser profile doesn't already have, stop and tell the user, in your final \
result, to sign in once in a real, visible Chrome window (Daimon's "Login to browser" flow) — that \
login then carries into all your future sessions without you ever handling the password.

# Planning and delegation
For work with three or more real steps, write the plan down with update_todos before you start, \
and keep it current — the user watches it as a live checklist, so it is how they know what you're \
doing and how much is left. Exactly one item in_progress at a time, and mark an item done the \
moment it is done rather than batching updates at the end. Skip it entirely for single-step work.

Delegate with `task` when a piece of work would otherwise flood your context — mapping an \
unfamiliar codebase, chasing down where something is defined, investigating several independent \
questions. A sub-agent shares none of your context and returns only its conclusion, so write its \
prompt as if to someone who just walked in, and say what you want reported back. Several `task` \
calls in one message run at the same time. Do the work yourself when you already know where to \
look — delegation costs a round trip.

# Asking the user
ask_user and present_plan pause the turn until the user replies, so use them where the answer \
actually changes what you build: a genuine ambiguity where two readings lead to different work, \
or a decision that is theirs to make. Don't ask for permission to continue, don't ask what you can \
find out by reading the project, and don't ask to confirm a routine judgment call — make it, say \
what you assumed, and keep going. When you do ask, offer 2-4 concrete options with the one you'd \
recommend first. If the user has already answered a question, or repeated an instruction after you \
raised a concern, that is their decision — proceed with it rather than asking again.

# Research discipline
web_search, web_fetch, open_url, and read_page share a hard, enforced limit on how many times \
they can be used in a single turn — treat each one as worth using deliberately, not for \
open-ended exploring. Prefer web_search over guessing a URL when you don't already know the \
specific page you need. web_fetch reads a URL directly and works without the background browser \
— prefer it over open_url + read_page for simply reading a page when you already have the URL. \
Don't re-read a page you've already read unless something has actually changed since (e.g. after \
a click or fill_field action). If several steps have passed without clear progress toward the goal, \
stop and report what you've found or tried so far rather than continuing to retry the same approach.

# Browser (optional — may be unavailable)
open_url, read_page, click, fill_field, new_tab, switch_tab, close_tab, and extract_text run in \
an invisible background browser that may be temporarily unavailable. web_fetch does not need the \
browser — use it for simple page reading. When the browser is available, read_page returns an \
accessibility snapshot with stable refs (e.g. "e5"). click and fill_field address elements by \
ref, not CSS selector — call read_page first to see the current page's interactive elements. \
Refs go stale after any navigation, click, or fill_field (elements get renumbered).

# Safety
You're running on the user's real machine — not a sandbox. Don't delete or overwrite anything \
without being asked to, don't run destructive shell commands, and don't install packages globally. \
System-level changes and credential-related operations go through stage_terminal_command so the \
user sees and controls them.

# Reporting back
Your final result lands in a small chat bubble in a floating panel, not a document. Write it like a \
short message to a colleague: lead with the outcome, keep it to a few sentences, and skip the \
narration of your own tool use unless the user actually asked how you did something. Don't restate \
the instruction back to them. Markdown renders, so a short list is fine — but a heading or a table \
in a chat bubble this size is not."""


PLAN_MODE_RULES = """# Plan mode is on
Before you create, edit, move, or delete anything, or run any command that changes state, work out \
what you intend to do and put it to the user with present_plan. Read, search, and ask whatever you \
need first — investigating is not changing anything. Once they approve, carry the plan out; if they \
ask for changes, revise and present it again. This applies until the user turns plan mode off.

This is enforced, not advisory: every tool that would change something refuses to run until a plan \
of yours has been approved, and tells you so. If you find yourself reading that message, present a \
plan rather than trying a different tool."""


#: How long a git probe is reused. Re-shelling out on every hop of a turn buys
#: nothing — the branch does not change mid-turn, and the dirty flag changing
#: has no bearing on what the agent should do next.
_GIT_TTL_S = 30.0
_git_cache: dict[str, tuple[float, str]] = {}


def _git(root: Path, *args: str) -> str:
    """One git command, or "" for anything that isn't a clean success. Not
    being in a repo is the common case, not an error."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _git_line(root: Path) -> str:
    key = str(root)
    cached = _git_cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _GIT_TTL_S:
        return cached[1]

    # `--show-current` first: it answers in a repo that has no commits yet,
    # where rev-parse fails outright because there is no HEAD to resolve. It
    # returns empty on a detached HEAD, which is what the fallback covers.
    branch = _git(root, "branch", "--show-current") or _git(
        root, "rev-parse", "--abbrev-ref", "HEAD"
    )
    line = ""
    if branch:
        dirty = bool(_git(root, "status", "--porcelain"))
        line = f"Git branch: {branch}" + (" (uncommitted changes)" if dirty else " (clean)")
    _git_cache[key] = (now, line)
    return line


def environment_block(settings: Settings) -> str:
    """Where the agent is. Four lines it would otherwise spend tool calls
    rediscovering at the start of every session — and often doesn't, which is
    how you get a note written into whatever directory the CLI was launched
    from."""
    root = Path(settings.resolved_workspace_dir)
    lines = [
        "# Environment",
        f"Workspace (file tools, shell, and the kernel all work here): {root}",
        f"Vault (where notes go): {Path(settings.vault_dir)}",
        f"Platform: {platform.system()} {platform.release()}",
    ]
    git = _git_line(root)
    if git:
        lines.append(git)
    return "\n".join(lines)


#: Project instruction files, most specific first. DAIMON.md is ours; the other
#: two are read because a repo that already has one has already written down
#: what an agent working here needs to know, and asking the user to duplicate it
#: under a third name would be a poor trade.
PROJECT_CONTEXT_FILES = ("DAIMON.md", "AGENTS.md", "CLAUDE.md")

#: Cap on the combined project context. A file past this is being used as
#: documentation rather than as instructions, and it is displacing the
#: conversation to no purpose.
MAX_PROJECT_CONTEXT_CHARS = 16_000

_context_cache: dict[str, tuple[float, str]] = {}


def _read_context_file(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        stamp = path.stat().st_mtime_ns
    except OSError:
        return ""
    cached = _context_cache.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    _context_cache[str(path)] = (stamp, text)
    return text


def project_context(settings: Settings) -> str:
    """Standing instructions for this machine and this project.

    Two sources, global first so the project can override it in the same way a
    project config beats a global one: `~/.daimon/DAIMON.md` follows the user
    everywhere, and the first of DAIMON.md / AGENTS.md / CLAUDE.md found in the
    workspace root travels with the repo.

    This is the piece the harness was missing entirely. Conventions the user
    has already written down — how to run the tests, which directory the source
    lives in, what not to touch — were rediscovered by reading, every session,
    or more often simply not discovered at all.
    """
    parts: list[tuple[str, str]] = []

    global_file = Path(settings.resolved_global_context)
    home = _read_context_file(global_file)
    if home:
        parts.append((f"{global_file.name} (global)", home))

    root = Path(settings.resolved_workspace_dir)
    for name in PROJECT_CONTEXT_FILES:
        text = _read_context_file(root / name)
        if text:
            parts.append((name, text))
            break  # most specific wins; two of them would just contradict

    if not parts:
        return ""

    sections = [
        "# Standing instructions",
        "",
        "Written down by the user for this machine and this project. They take "
        "precedence over your own defaults, and over any general guidance above "
        "that they contradict.",
    ]
    budget = MAX_PROJECT_CONTEXT_CHARS
    for source, text in parts:
        if budget <= 0:
            break
        body = text[:budget]
        if len(body) < len(text):
            body += f"\n\n… (truncated at {MAX_PROJECT_CONTEXT_CHARS:,} characters)"
        budget -= len(body)
        sections.append(f"\n## From {source}\n\n{body}")
    return "\n".join(sections)


def date_line() -> str:
    """Local date in one unambiguous line, so relative dates ("Friday", "in
    two weeks") resolve against the user's own machine — the era-1 `new
    Date().toString()` equivalent, kept in the same local frame.

    Day resolution, not seconds: this line sits at the head of the per-task
    tail, and a timestamp that changes every second would break the prefix
    cache on every single turn for the sake of precision nothing here uses.
    """
    now = datetime.now().astimezone()
    return f"Current date on the user's machine: {now.strftime('%a %b %d %Y %Z')}"


def build_system_prompt(
    settings: Settings,
    skills_block: str = "",
    date_line_text: str | None = None,
    mode: str = "normal",
) -> str:
    """The frozen-prefix system prompt: constant rules first (prefix cache),
    then the date, then the per-task skills tail.

    Mode rules go with the constant prefix rather than the tail — plan mode
    holds for a whole session, so keeping it adjacent to DAIMON_RULES means a
    session in plan mode has its own stable prefix instead of a cache miss on
    every turn.

    There is deliberately no memory block here. Stored notes and past tasks
    reach the model through the `recall` tool, which the agent calls when it
    decides it needs them — injecting them into every prompt would pay for
    them on every turn whether or not they're relevant.

    The environment and standing-instruction blocks go in the *tail*, with the
    date and the skills index, and never in the prefix. They change when the
    user switches branch or edits DAIMON.md, and a prefix that changes is a
    prefix that never caches.
    """
    prefix = f"{DAIMON_RULES}\n\n{PLAN_MODE_RULES}" if mode == "plan" else DAIMON_RULES
    parts = [prefix, date_line_text or date_line(), environment_block(settings)]
    context = project_context(settings)
    if context:
        parts.append(context)
    if skills_block:
        parts.append(skills_block)
    return "\n\n".join(parts)
