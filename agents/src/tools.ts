import { z } from "zod";
import { tool } from "@langchain/core/tools";
import * as automation from "./automation.js";
import * as browser from "./browser.js";
import * as documents from "./documents.js";
import { playMusicByName } from "./spotify.js";
import * as vault from "./vault.js";
import { indexNote, saveSkill } from "./memory.js";
import type { TaskEvent } from "./events.js";

// A factory, not a static array, because open_terminal_with_command (below)
// needs a way to push a `ui_action` event to the frontend as a side effect,
// not just return a string to the LLM like every other tool here — and the
// per-turn `emit` callback it needs only exists once `runTurn` (run.ts) is
// already underway, not at module load time. Called fresh each turn in
// graph.ts's `buildAgent` so it's always closed over that turn's own `emit`.
export function buildDaimonTools(emit: (event: TaskEvent) => void) {
  // Per-turn state (this factory is called fresh at the start of every turn
  // — see graph.ts's `buildAgent` — so nothing here ever leaks across turns
  // or sessions). Detects a browsing tool being called with byte-for-byte
  // identical arguments more than once in the same turn — a cheap, reliable
  // signal that the model is stuck looping rather than making progress,
  // since any genuinely new attempt (a different query/URL/selector)
  // always produces a different key. Rather than letting the underlying
  // action actually run again (a real network request, for a repeat that
  // can't produce new information), the repeat itself becomes the tool's
  // return value: a direct correction fed back into the conversation as a
  // normal ToolMessage, using the exact same channel the model already
  // reads its results from.
  const repeatedCallCounts = new Map<string, number>();
  function checkRepeat(toolName: string, args: unknown): string | null {
    const key = `${toolName}:${JSON.stringify(args)}`;
    const count = (repeatedCallCounts.get(key) ?? 0) + 1;
    repeatedCallCounts.set(key, count);
    if (count <= 1) return null;
    return (
      `You've already called ${toolName} with these exact same arguments ${count} time(s) this ` +
      `turn and gotten the same result — repeating it again won't produce new information. Try a ` +
      `genuinely different approach (a different query/URL/selector), or if you've already tried ` +
      `several different approaches without progress, stop here and report what you've found so far.`
    );
  }

  // `read_page` takes no arguments at all, so the generic "same arguments"
  // check above can't tell "reading the exact same static page again"
  // (wasteful) apart from "reading again after a click/fill changed
  // something" (legitimate) — every call would look identical either way.
  // Track the last-seen (URL, text) pair instead and compare actual
  // content, which correctly distinguishes the two cases.
  let lastReadSignature: string | null = null;

  // A real, reported loop (web_search -> read_page -> open_url, repeating)
  // survived the exact-argument guard above, because a model that's stuck
  // rarely repeats byte-identical arguments — it rephrases the query
  // slightly each time, which produces a "new" key every time and slips
  // straight past that check. This is a documented failure mode for
  // ReAct-style tool loops generally, not something specific to this
  // codebase: exact (tool, args) hashing alone only catches the crudest
  // form of repetition; the standard mitigation on top of it is (1) a hard,
  // enforced budget on the whole category of "research" actions
  // (web_search + open_url + read_page combined) that actually *refuses*
  // to keep executing past a cap rather than just nudging the model, and
  // (2) catching *near*-duplicate queries via simple text-similarity, not
  // just exact string equality.
  //
  // The budget is enforced here, not just suggested in the system prompt —
  // once past `RESEARCH_TOOL_BUDGET`, these three tools stop doing real
  // work entirely and only ever return the same hard-stop instruction, so
  // the model has no way to keep spending steps on research even if it
  // tries to.
  const RESEARCH_TOOL_BUDGET = 10;
  let researchToolCallCount = 0;
  function checkResearchBudget(toolName: string): string | null {
    researchToolCallCount += 1;
    if (researchToolCallCount <= RESEARCH_TOOL_BUDGET) return null;
    return (
      `You've used ${RESEARCH_TOOL_BUDGET} search/browse actions this turn without reaching an ` +
      `answer — that's the limit for this turn. Do not call web_search, open_url, or read_page ` +
      `again. Answer now using whatever you've already found, or tell the user plainly that you ` +
      `weren't able to find a reliable answer.`
    );
  }

  // Catches "the model just reworded the same search" — a plain lowercase
  // word-set Jaccard similarity against every query already tried this
  // turn, so "best pizza nyc" and "top pizza restaurants New York City"
  // are recognized as the same underlying attempt even though they're
  // different strings.
  const triedSearchQueries: Set<string>[] = [];
  function wordSet(text: string): Set<string> {
    return new Set(
      text
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .split(/\s+/)
        .filter(Boolean),
    );
  }
  function jaccardSimilarity(a: Set<string>, b: Set<string>): number {
    let intersection = 0;
    for (const word of a) if (b.has(word)) intersection++;
    const union = a.size + b.size - intersection;
    return union === 0 ? 1 : intersection / union;
  }

  return [
    tool(
      async ({ url }: { url: string }) => {
        const budgetHit = checkResearchBudget("open_url");
        if (budgetHit) return budgetHit;
        const warning = checkRepeat("open_url", { url });
        if (warning) return warning;
        const title = await browser.openUrl(url);
        return `Opened ${url}. Page title: ${title}`;
      },
      {
        name: "open_url",
        description: "Navigate the background browser to a URL and return the page title.",
        schema: z.object({ url: z.string().describe("The absolute URL to open") }),
      },
    ),
    tool(
      async ({ ref, text }: { ref: string; text: string }) => {
        // Scoped by `lastReadSignature` (the page a ref was read from), not
        // just (ref, text) — PinchTab's refs (e0, e1, ...) are renumbered
        // per snapshot, so clicking/filling ref "e5" on two different pages
        // is a legitimately different action even though the ref string
        // repeats, and must not be flagged as a no-op repeat.
        const warning = checkRepeat("fill_field", { ref, text, page: lastReadSignature });
        if (warning) return warning;
        await browser.fill(ref, text);
        return `Filled element "${ref}" with the given text.`;
      },
      {
        name: "fill_field",
        description:
          "Fill a form field with the given text. `ref` is an element reference from read_page's " +
          "interactive elements listing (e.g. 'e3'), not a CSS selector — call read_page first if " +
          "you don't already have a current one. Refs go stale after any navigation, click, or " +
          "fill, so call read_page again before reusing one.",
        schema: z.object({
          ref: z.string().describe("Element ref from read_page's interactive elements listing, e.g. 'e3'"),
          text: z.string().describe("The text to type into the field"),
        }),
      },
    ),
    tool(
      async ({ ref }: { ref: string }) => {
        // See the comment on fill_field above for why `lastReadSignature` is
        // part of this key.
        const warning = checkRepeat("click", { ref, page: lastReadSignature });
        if (warning) return warning;
        await browser.click(ref);
        return `Clicked element "${ref}".`;
      },
      {
        name: "click",
        description:
          "Click an element (button, link, checkbox, ...). `ref` is an element reference from " +
          "read_page's interactive elements listing (e.g. 'e5'), not a CSS selector — call " +
          "read_page first if you don't already have a current one. Refs go stale after any " +
          "navigation, click, or fill, so call read_page again before reusing one.",
        schema: z.object({
          ref: z.string().describe("Element ref from read_page's interactive elements listing, e.g. 'e5'"),
        }),
      },
    ),
    tool(
      async () => {
        const budgetHit = checkResearchBudget("read_page");
        if (budgetHit) return budgetHit;
        const { url, text, elements } = await browser.readPage();
        const signature = `${url}::${text}`;
        if (signature === lastReadSignature) {
          return (
            "The page hasn't changed since you last read it — same content, no new information " +
            "here. If you're stuck, try a different action: search with different terms, open a " +
            "different URL, or click/fill something to actually change the page first. If you've " +
            "tried several different approaches without progress, stop and report what you've " +
            "found so far."
          );
        }
        lastReadSignature = signature;
        if (elements.length === 0) return text;
        const elementLines = elements.map((el) => `${el.ref}: ${el.role} "${el.label}"`).join("\n");
        return `${text}\n\nInteractive elements:\n${elementLines}`;
      },
      {
        name: "read_page",
        description:
          "Read the visible text content of the current page, along with a list of its " +
          "interactive elements (ref, role, label) to decide what to do next. Use the ref shown " +
          "here — not a CSS selector — with click/fill_field.",
        schema: z.object({}),
      },
    ),
    tool(
      async ({ query }: { query: string }) => {
        const budgetHit = checkResearchBudget("web_search");
        if (budgetHit) return budgetHit;
        const warning = checkRepeat("web_search", { query });
        if (warning) return warning;

        const queryWords = wordSet(query);
        const isNearDuplicate = triedSearchQueries.some((prev) => jaccardSimilarity(queryWords, prev) >= 0.6);
        triedSearchQueries.push(queryWords);
        if (isNearDuplicate) {
          return (
            `"${query}" is too similar to a query you already tried this turn — rewording it ` +
            `slightly won't surface meaningfully different results. Either open one of the results ` +
            `you already have, try a genuinely different angle on the question, or stop and report ` +
            `what you've found so far.`
          );
        }

        const results = await browser.search(query);
        if (results.length === 0) {
          return `No search results found for "${query}". Try a different, more specific query.`;
        }
        return results
          .map((r, i) => `${i + 1}. ${r.title} — ${r.url} — ${r.snippet}`)
          .join("\n");
      },
      {
        name: "web_search",
        description:
          "Search the web for a query and get back a numbered list of results (title, URL, " +
          "snippet). Use this whenever you don't already know the specific URL you need, instead " +
          "of guessing one. Then use open_url on whichever result looks most relevant.",
        schema: z.object({ query: z.string().describe("The search query") }),
      },
    ),
    tool(
      async () => {
        const path = await browser.finishRecording();
        if (!path) {
          return "No browser recording is in progress — open_url/click/fill_field haven't been used yet this turn.";
        }
        return `Saved a video recording of the browser session to ${path}. Mention this to the user so they know they can watch it.`;
      },
      {
        name: "finish_recording",
        description:
          "Stop and save a video recording of everything the background browser has done so far " +
          "this turn (every open_url/click/fill_field action), so the user can watch it. Call this " +
          "whenever the user explicitly asks to see, record, or review what you did in the browser " +
          "— e.g. 'show me what you're doing' or 'record this.' Call it once, at the end, after " +
          "you've finished the browsing you want captured — calling it starts a fresh, empty " +
          "recording for anything that happens afterward.",
        schema: z.object({}),
      },
    ),
    tool(
      async ({ name, description }: { name: string; description: string }) => {
        saveSkill(name, description);
        return `Saved skill "${name}" for future reuse.`;
      },
      {
        name: "save_skill",
        description:
          "Save a reusable, generalized description of a task pattern you just completed, so a " +
          "similar future request can be handled faster. Only for genuinely reusable procedures, " +
          "not one-off tasks.",
        schema: z.object({
          name: z.string().describe("Short identifier, e.g. 'submit-job-application'"),
          description: z.string().describe("A generalized, parameterized description of the procedure"),
        }),
      },
    ),
    tool(
      async ({ filename, content }: { filename: string; content: string }) => {
        const savedName = await vault.writeNote(filename, content);
        // Indexed immediately so it's searchable within this same session,
        // rather than waiting for the next container restart's startup scan
        // (see server.ts).
        indexNote(savedName, content);
        return `Saved note "${savedName}" to the vault.`;
      },
      {
        name: "write_note",
        description:
          "Write (or overwrite) a markdown note in the user's notes vault. Use this to persist " +
          "durable findings, plans, or reference material beyond a single task.",
        schema: z.object({
          filename: z.string().describe("Note file name, e.g. 'job-search-notes.md' (`.md` added if missing)"),
          content: z.string().describe("The full markdown content of the note"),
        }),
      },
    ),
    tool(
      async ({ filename }: { filename: string }) => vault.readNote(filename),
      {
        name: "read_note",
        description: "Read the content of an existing markdown note from the user's notes vault.",
        schema: z.object({ filename: z.string().describe("Note file name to read") }),
      },
    ),
    tool(
      async () => {
        const notes = await vault.listNotes();
        return notes.length > 0 ? notes.join("\n") : "(vault is empty)";
      },
      {
        name: "list_notes",
        description: "List the markdown note file names currently in the user's notes vault.",
        schema: z.object({}),
      },
    ),
    tool(
      async ({ name, instruction, schedule }: { name: string; instruction: string; schedule: string }) => {
        await automation.requestAutomation(name, instruction, schedule);
        return `Automation "${name}" scheduled (${schedule}). It'll run automatically going forward.`;
      },
      {
        name: "create_automation",
        description:
          "Schedule a recurring instruction to run automatically without user interaction — e.g. " +
          "a daily brief. `schedule` is a standard 5-field cron expression (minute hour day month " +
          "weekday), e.g. '0 8 * * *' for daily at 8am. Use this whenever the user asks for " +
          "something recurring, scheduled, daily, or 'every morning/week/etc.'",
        schema: z.object({
          name: z.string().describe("Short human-readable name, e.g. 'Daily Tech Brief'"),
          instruction: z.string().describe("The instruction to run each time this fires"),
          schedule: z.string().describe("Standard 5-field cron expression, e.g. '0 8 * * *' for daily at 8am"),
        }),
      },
    ),
    tool(
      async ({ name, instruction, remindAt }: { name: string; instruction: string; remindAt: string }) => {
        await automation.requestReminder(name, instruction, remindAt);
        const when = new Date(remindAt).toLocaleString();
        return `Reminder "${name}" set for ${when}. Daimon will notify you then — it won't repeat.`;
      },
      {
        name: "create_reminder",
        description:
          "Set a one-time reminder for a specific future date/time — e.g. 'remind me about the " +
          "job posting on Friday' or 'put a reminder on my calendar for the 15th.' This does NOT " +
          "write to a real calendar app; it's Daimon's own reminder, delivered as a native " +
          "notification at that moment (it works even if the user isn't looking at Daimon right " +
          "then). Use this instead of create_automation whenever the request is about a single " +
          "specific date/time rather than something recurring — create_automation can only " +
          "repeat, it has no way to fire 'just once.' `instruction` is what Daimon actually does " +
          "when it fires (e.g. re-check something and summarize) — for a plain reminder with " +
          "nothing to look up, make it something like 'Remind the user: <message>' so the result " +
          "is exactly that message, not a generic acknowledgement.",
        schema: z.object({
          name: z.string().describe("Short human-readable name, e.g. 'Follow up on Tesla job posting'"),
          instruction: z.string().describe("What Daimon does when this fires — becomes the notification body"),
          remindAt: z
            .string()
            .describe("The exact date/time to fire, as an ISO 8601 datetime, e.g. '2026-08-15T09:00:00'"),
        }),
      },
    ),
    // Deliberately "stage, don't execute" — the agent never presses Enter on
    // the user's behalf, matching the same principle already used for
    // voice-dictated text landing in an input without auto-submitting. The
    // real work here (opening a terminal tab, focusing it, typing the text)
    // happens entirely on the frontend in reaction to the `ui_action` event;
    // this tool's own job is just to fire that event and hand the LLM back a
    // plain-language confirmation of what happened and what's still pending.
    tool(
      async ({ command }: { command: string }) => {
        emit({ type: "ui_action", action: "open_terminal_with_command", command });
        return `Opened a new terminal tab in Daimon and typed "${command}" into it — staged only, ` +
          `not executed. The user needs to press Enter themselves to actually run it.`;
      },
      {
        name: "open_terminal_with_command",
        description:
          "Open a brand-new terminal tab in Daimon's own UI, switch to it, and type a shell " +
          "command into it WITHOUT running it — the user has to press Enter themselves to " +
          "actually execute it. Use this whenever the user asks you to open/use their terminal, " +
          "or navigate to a directory and launch something there (e.g. 'go into my terminal, cd " +
          "to my Desktop, and open Claude Code'). Combine multiple steps into one shell command " +
          "line with && rather than calling this tool more than once for the same request (e.g. " +
          "'cd ~/Desktop && claude'). This never actually runs anything on the user's machine on " +
          "its own — it only stages text for the user to review and run themselves.",
        schema: z.object({
          command: z.string().describe("The shell command to type into the newly opened terminal — staged, not executed"),
        }),
      },
    ),
    // Unlike open_terminal_with_command above, this executes immediately —
    // no staging, no confirmation step. Launching a named application is
    // low-risk and trivially reversible (the user just quits it again), so
    // there's no reason to slow down a voice command with a review step the
    // way a shell command (which could do anything) warrants. The actual
    // `open -a <name>` call happens in the Rust daemon, not here — this
    // container has no direct access to the user's real desktop — see
    // HostActionEvent's doc comment in events.ts.
    tool(
      async ({ name }: { name: string }) => {
        emit({ type: "host_action", action: "open_application", name });
        return `Opened ${name}.`;
      },
      {
        name: "open_application",
        description:
          "Launch a native application on the user's Mac by name (e.g. 'Spotify', 'Calculator', " +
          "'Notes', 'Xcode') — the real installed desktop app, not a website or a web version of " +
          "it. Opens immediately with no confirmation needed. Use this whenever the user asks to " +
          "open, launch, start, or switch to an app by name. If they specifically want a website " +
          "instead (e.g. 'open Spotify in my browser'), use open_url, not this.",
        schema: z.object({
          name: z
            .string()
            .describe("The application's name exactly as it appears in Finder/Launchpad, e.g. 'Spotify'"),
        }),
      },
    ),
    // Same reasoning as open_application above (low-risk, reversible,
    // immediate) — quits gracefully (lets the app save state / prompt for
    // unsaved changes) rather than force-killing it.
    tool(
      async ({ name }: { name: string }) => {
        emit({ type: "host_action", action: "close_application", name });
        return `Closed ${name}.`;
      },
      {
        name: "close_application",
        description:
          "Quit a native application on the user's Mac by name (e.g. 'Spotify', 'Calculator') — " +
          "a graceful quit, same as choosing Quit from its menu, not a force-kill. Opens " +
          "immediately with no confirmation needed. Use this whenever the user asks to close, " +
          "quit, or exit an app by name.",
        schema: z.object({
          name: z
            .string()
            .describe("The application's name exactly as it appears in Finder/Launchpad, e.g. 'Spotify'"),
        }),
      },
    ),
    // Deliberately a short, fixed list of commands rather than a free-form
    // AppleScript string — see HostActionEvent's doc comment in events.ts.
    // Spotify/Music's real AppleScript dictionaries only support transport
    // control and playing a *known* track URI, not "search for X and play
    // it" — see play_music_by_name below for that (real Spotify Web API
    // search + Connect device-targeted playback, landing on the user's
    // actual desktop app). This tool's description says so explicitly so
    // the model doesn't reach for it expecting search.
    tool(
      async ({ app, command }: { app: "Spotify" | "Music"; command: "play" | "pause" | "next" | "previous" }) => {
        emit({ type: "host_action", action: "music_control", app, command });
        return `Sent "${command}" to ${app}.`;
      },
      {
        name: "music_control",
        description:
          "Control playback in Spotify or Apple Music — play/resume, pause, skip to the next " +
          "track, or go back to the previous one. This only controls whatever is already loaded " +
          "or queued in the app; it CANNOT search for a song, artist, or playlist by name — for " +
          "'play <song>'/'play some jazz' type requests, use play_music_by_name instead. Opens " +
          "immediately with no confirmation needed.",
        schema: z.object({
          app: z.enum(["Spotify", "Music"]).describe("Which app to control"),
          command: z.enum(["play", "pause", "next", "previous"]).describe("The transport command to send"),
        }),
      },
    ),
    // Real search — Spotify's Web API, not AppleScript (which can't search)
    // and not the web player (which wouldn't play through the user's real
    // desktop app/output device). Requires a connected Spotify account
    // (Settings) — see spotify.ts's own doc comment for the full mechanism.
    tool(
      async ({ query }: { query: string }) => {
        return await playMusicByName(query);
      },
      {
        name: "play_music_by_name",
        description:
          "Search for a specific song, artist, or track and start it playing on the user's real " +
          "desktop Spotify app — use this for 'play <song>', 'play something by <artist>' type " +
          "requests. Requires a Spotify account connected in Settings and the desktop app already " +
          "open (open_application first if it's not). Use music_control instead for plain " +
          "play/pause/skip on whatever's already playing.",
        schema: z.object({
          query: z.string().describe("What to search for, e.g. 'Blinding Lights The Weeknd'"),
        }),
      },
    ),
    // Writes a real .xlsx file directly rather than trying to drive a live
    // Excel window — Excel's own AppleScript/scripting support on macOS is
    // inconsistent/partial, a poor automation target compared to just
    // producing the actual file. See documents.ts's own doc comment.
    tool(
      async ({
        filename,
        sheetName,
        rows,
      }: {
        filename: string;
        sheetName?: string;
        rows: string[][];
      }) => {
        const destPath = await documents.writeSpreadsheet(filename, sheetName ?? "Sheet1", rows);
        emit({ type: "host_action", action: "open_file", path: destPath });
        return `Saved the spreadsheet to ${destPath} and opened it.`;
      },
      {
        name: "write_spreadsheet",
        description:
          "Create a real .xlsx spreadsheet file with the given data and open it in the user's " +
          "default spreadsheet app (Excel, Numbers, etc.) — use this for 'make/fill out a " +
          "spreadsheet' type requests instead of trying to control a live Excel window, which " +
          "isn't reliably scriptable. `rows` is the full grid, top to bottom — conventionally " +
          "make the first row a header row (e.g. column titles).",
        schema: z.object({
          filename: z.string().describe("File name, e.g. 'job-applications.xlsx' (.xlsx added if missing)"),
          sheetName: z.string().optional().describe("Sheet tab name, defaults to 'Sheet1'"),
          rows: z
            .array(z.array(z.string()))
            .describe("Grid of cell values, row by row — first row conventionally a header"),
        }),
      },
    ),
  ];
}
