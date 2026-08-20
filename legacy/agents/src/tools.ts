import { z } from "zod";
import { createSdkMcpServer, tool } from "@anthropic-ai/claude-agent-sdk";
import * as automation from "./automation.js";
import * as browser from "./browser.js";
import * as documents from "./documents.js";
import { playMusicByName } from "./spotify.js";
import { formatMemoryContext, searchNotes, searchSkills, searchTasks } from "./memory.js";
import { emitTaskEvent } from "./emitter.js";

// Every tool returns this shape (MCP's CallToolResult). The old LangGraph
// tools returned a bare string; MCP wraps it.
function text(body: string) {
  return { content: [{ type: "text" as const, text: body }] };
}

// Daimon's own capabilities, exposed to the Claude Code harness as an
// in-process MCP server. Everything here is something the harness has no
// built-in equivalent for: a stateful logged-in browser, the user's real
// desktop, the scheduler, Spotify, and .xlsx generation.
//
// Note what is deliberately NOT here any more:
//   - write_note/read_note/list_notes — the harness's built-in Write/Read/
//     Glob do this, with `cwd` pointed at the vault (see agent.ts).
//   - save_skill — the agent writes a real SKILL.md into vault/skills/
//     with Write instead of stuffing a name+description into SQLite.
//   - ~89 lines of anti-loop machinery (exact-argument repeat hashing, an
//     unchanged-page signature, a hard 10-call research budget, and Jaccard
//     near-duplicate query detection). All four existed because
//     createReactAgent had no loop detection of its own and each previous
//     mitigation had failed; the harness handles this. If looping resurfaces,
//     add back ONE mechanism and say which — not the whole stack again.
export const daimonToolServer = createSdkMcpServer({
  name: "daimon",
  version: "1.0.0",
  tools: [
    // ---- background browser (PinchTab) --------------------------------
    // Not replaceable by the harness's built-in WebFetch/WebSearch: Daimon
    // needs one stateful, logged-in, recordable browser session that
    // persists across tool calls and across turns, not independent
    // one-shot fetches.
    tool(
      "open_url",
      "Navigate the background browser to a URL and return the page title.",
      { url: z.string().describe("The absolute URL to open") },
      async ({ url }) => text(`Opened ${url}. Page title: ${await browser.openUrl(url)}`),
    ),
    tool(
      "read_page",
      "Read the visible text of the current page in the background browser, plus a listing of " +
        "its interactive elements as (ref, role, label). Use the ref shown here — not a CSS " +
        "selector — with click/fill_field.",
      {},
      async () => {
        const { text: body, elements } = await browser.readPage();
        if (elements.length === 0) return text(body);
        const lines = elements.map((el) => `${el.ref}: ${el.role} "${el.label}"`).join("\n");
        return text(`${body}\n\nInteractive elements:\n${lines}`);
      },
    ),
    tool(
      "click",
      "Click an element (button, link, checkbox, ...) in the background browser. `ref` is an " +
        "element reference from read_page's listing (e.g. 'e5'), NOT a CSS selector. Refs are " +
        "renumbered on every snapshot, so they go stale after any navigation, click, or fill — " +
        "call read_page again before reusing one rather than assuming it still points at the " +
        "same element.",
      { ref: z.string().describe("Element ref from read_page's listing, e.g. 'e5'") },
      async ({ ref }) => {
        await browser.click(ref);
        return text(`Clicked element "${ref}".`);
      },
    ),
    tool(
      "fill_field",
      "Type text into a form field in the background browser. `ref` is an element reference " +
        "from read_page's listing (e.g. 'e3'), NOT a CSS selector. Refs go stale after any " +
        "navigation, click, or fill — call read_page again before reusing one.",
      {
        ref: z.string().describe("Element ref from read_page's listing, e.g. 'e3'"),
        text: z.string().describe("The text to type into the field"),
      },
      async ({ ref, text: value }) => {
        await browser.fill(ref, value);
        return text(`Filled element "${ref}" with the given text.`);
      },
    ),
    tool(
      "web_search",
      "Search the web and get back a numbered list of results (title, URL, snippet). Use this " +
        "when you don't already know the specific URL you need, instead of guessing one, then " +
        "open_url the most relevant result.",
      { query: z.string().describe("The search query") },
      async ({ query }) => {
        const results = await browser.search(query);
        if (results.length === 0) {
          return text(`No search results found for "${query}". Try a different, more specific query.`);
        }
        return text(results.map((r, i) => `${i + 1}. ${r.title} — ${r.url} — ${r.snippet}`).join("\n"));
      },
    ),
    tool(
      "finish_recording",
      "Stop and save a video recording of everything the background browser has done so far " +
        "this turn, so the user can watch it. Call this when the user explicitly asks to see, " +
        "record, or review what you did in the browser. Call it once, at the end of the browsing " +
        "you want captured — calling it starts a fresh, empty recording for anything after.",
      {},
      async () => {
        const path = await browser.finishRecording();
        if (!path) {
          return text(
            "No browser recording is in progress — no browser actions have been taken yet this turn.",
          );
        }
        return text(
          `Saved a video recording of the browser session to ${path}. Mention this to the user ` +
            `so they know they can watch it.`,
        );
      },
    ),

    // ---- memory -------------------------------------------------------
    // New: under LangGraph, memory was injected once before the loop began
    // and the agent had no way to query it mid-task. Now it can.
    tool(
      "recall",
      "Search Daimon's memory of past tasks, saved skills, and the user's vault notes for " +
        "anything relevant to a topic. Use this when a request references something that may " +
        "have come up before ('the job spreadsheet I made', 'like last time') or when prior " +
        "context would change how you approach the task.",
      { query: z.string().describe("What to look for, e.g. 'job applications spreadsheet'") },
      async ({ query }) => {
        const context = formatMemoryContext(
          searchTasks(query, 3),
          searchSkills(query, 3),
          searchNotes(query, 3),
        );
        return text(context || `Nothing in memory matched "${query}".`);
      },
    ),

    // ---- scheduling ---------------------------------------------------
    // These write a pending-*.json file into DAIMON_AUTOMATIONS_DIR, which
    // the Rust scheduler polls and re-validates — the agent process has no
    // direct callback channel to the daemon. See automation.ts.
    tool(
      "create_reminder",
      "Set a ONE-TIME reminder for a specific future date/time — 'remind me about X on Friday', " +
        "'ping me on the 15th', 'in two hours'. Delivered as a native notification at that " +
        "moment, so it reaches the user even if Daimon isn't open. This is the right tool " +
        "whenever the request names a single concrete occasion. It does NOT write to a real " +
        "calendar app. `instruction` is what Daimon actually does when it fires and becomes the " +
        "notification body — for a plain reminder with nothing to look up, phrase it as " +
        "'Remind the user: <message>' so the result is exactly that message rather than a " +
        "generic acknowledgement.",
      {
        name: z.string().describe("Short human-readable name, e.g. 'Follow up on Tesla job posting'"),
        instruction: z.string().describe("What Daimon does when this fires — becomes the notification body"),
        remindAt: z
          .string()
          .describe("Exact date/time to fire, ISO 8601, e.g. '2026-08-15T09:00:00'"),
      },
      async ({ name, instruction, remindAt }) => {
        await automation.requestReminder(name, instruction, remindAt);
        return text(
          `Reminder "${name}" set for ${new Date(remindAt).toLocaleString()}. ` +
            `Daimon will notify you then — it won't repeat.`,
        );
      },
    ),
    tool(
      "create_automation",
      "Schedule a RECURRING instruction that runs unattended forever on a cron cadence — 'every " +
        "morning', 'daily', 'every Monday'. `schedule` is a standard 5-field cron expression " +
        "(minute hour day month weekday), e.g. '0 8 * * *' for 8am daily. This has no way to " +
        "fire just once — if the request names one specific date/time, use create_reminder.",
      {
        name: z.string().describe("Short human-readable name, e.g. 'Daily Tech Brief'"),
        instruction: z.string().describe("The instruction to run each time this fires"),
        schedule: z.string().describe("5-field cron expression, e.g. '0 8 * * *' for 8am daily"),
      },
      async ({ name, instruction, schedule }) => {
        await automation.requestAutomation(name, instruction, schedule);
        return text(`Automation "${name}" scheduled (${schedule}). It'll run automatically going forward.`);
      },
    ),

    // ---- the user's real machine --------------------------------------
    // All of these are pure event emissions; the Rust daemon performs the
    // actual action (see HostActionEvent in events.ts). This process has no
    // access to the user's desktop.
    //
    // "Stage, don't execute" is the invariant for open_terminal_with_command
    // specifically — the agent never presses Enter on the user's behalf,
    // matching how voice-dictated text lands in an input without submitting.
    tool(
      "open_terminal_with_command",
      "Open a new terminal tab in Daimon's own UI, switch to it, and type a shell command into " +
        "it WITHOUT running it — the user presses Enter themselves. Use this when the user asks " +
        "you to open or use their terminal, or to navigate somewhere and launch something there. " +
        "Combine multiple steps into one && -joined command line rather than calling this more " +
        "than once for the same request. This never executes anything on its own.",
      {
        command: z.string().describe("Shell command to stage in the new terminal — typed, not executed"),
      },
      async ({ command }) => {
        emitTaskEvent({ type: "ui_action", action: "open_terminal_with_command", command });
        return text(
          `Opened a new terminal tab in Daimon and typed "${command}" into it — staged only, ` +
            `not executed. The user needs to press Enter themselves to actually run it.`,
        );
      },
    ),
    // Unlike the terminal above, these run immediately with no staging step:
    // launching or quitting a named app is low-risk and trivially reversible,
    // so a review step would only slow down a voice command.
    tool(
      "open_application",
      "Launch a native application on the user's Mac by name ('Spotify', 'Calculator', 'Notes') " +
        "— the real installed desktop app, not a website. A bare 'open/launch/start <app>' " +
        "request always means this tool: it's the literal match, and it leaves the user able to " +
        "keep using that real app themselves afterward with their own logins, extensions, and " +
        "audio routing — none of which the background browser has. Only reach for the browser " +
        "tools instead when the request names an actual task beyond opening something ('go to " +
        "YouTube and play X' names a destination and an action). Opens immediately.",
      { name: z.string().describe("App name exactly as it appears in Finder/Launchpad, e.g. 'Spotify'") },
      async ({ name }) => {
        emitTaskEvent({ type: "host_action", action: "open_application", name });
        return text(`Opened ${name}.`);
      },
    ),
    tool(
      "close_application",
      "Quit a native application on the user's Mac by name — a graceful quit, same as choosing " +
        "Quit from its menu, not a force-kill. Runs immediately.",
      { name: z.string().describe("App name exactly as it appears in Finder/Launchpad") },
      async ({ name }) => {
        emitTaskEvent({ type: "host_action", action: "close_application", name });
        return text(`Closed ${name}.`);
      },
    ),
    // A fixed command list rather than a free-form AppleScript string — see
    // HostActionEvent's doc comment in events.ts. Spotify's and Music's real
    // AppleScript dictionaries only cover transport control, not search;
    // that's what play_music_by_name below is for.
    tool(
      "music_control",
      "Control playback in Spotify or Apple Music — play/resume, pause, next, previous. This " +
        "only controls what is already loaded or queued; it CANNOT search for a song by name. " +
        "For 'play <song>' requests use play_music_by_name instead. Runs immediately.",
      {
        app: z.enum(["Spotify", "Music"]).describe("Which app to control"),
        command: z.enum(["play", "pause", "next", "previous"]).describe("Transport command to send"),
      },
      async ({ app, command }) => {
        emitTaskEvent({ type: "host_action", action: "music_control", app, command });
        return text(`Sent "${command}" to ${app}.`);
      },
    ),
    // Real search via Spotify's Web API — not AppleScript (can't search) and
    // not the web player (wouldn't play through the user's real output
    // device). Requires an account connected in Settings; see spotify.ts.
    tool(
      "play_music_by_name",
      "Search for a specific song, artist, or track and start it playing on the user's real " +
        "desktop Spotify app — for 'play <song>' / 'play something by <artist>' requests. " +
        "Requires a Spotify account connected in Settings and the desktop app already open " +
        "(open_application first if it isn't). Use music_control for plain play/pause/skip.",
      { query: z.string().describe("What to search for, e.g. 'Blinding Lights The Weeknd'") },
      async ({ query }) => text(await playMusicByName(query)),
    ),

    // ---- documents ----------------------------------------------------
    // Writes a real .xlsx rather than driving a live Excel window — Excel's
    // macOS scripting support is too partial to be a reliable automation
    // target. See documents.ts.
    tool(
      "write_spreadsheet",
      "Create a real .xlsx spreadsheet with the given data and open it in the user's default " +
        "spreadsheet app. Use this for 'make/fill out a spreadsheet' requests instead of trying " +
        "to control a live Excel window, which isn't reliably scriptable. `rows` is the full " +
        "grid top to bottom — conventionally make the first row a header row.",
      {
        filename: z.string().describe("File name, e.g. 'job-applications.xlsx' (.xlsx added if missing)"),
        sheetName: z.string().optional().describe("Sheet tab name, defaults to 'Sheet1'"),
        rows: z.array(z.array(z.string())).describe("Grid of cell values, row by row"),
      },
      async ({ filename, sheetName, rows }) => {
        const destPath = await documents.writeSpreadsheet(filename, sheetName ?? "Sheet1", rows);
        emitTaskEvent({ type: "host_action", action: "open_file", path: destPath });
        return text(`Saved the spreadsheet to ${destPath} and opened it.`);
      },
    ),
  ],
});

// Fully-qualified names for the tools above, as the harness addresses them
// (`mcp__<server>__<tool>`). Listed explicitly rather than with a wildcard so
// that adding a tool above is a deliberate two-line change and never silently
// widens what the agent is allowed to do without a prompt.
export const DAIMON_TOOL_NAMES = [
  "mcp__daimon__open_url",
  "mcp__daimon__read_page",
  "mcp__daimon__click",
  "mcp__daimon__fill_field",
  "mcp__daimon__web_search",
  "mcp__daimon__finish_recording",
  "mcp__daimon__recall",
  "mcp__daimon__create_reminder",
  "mcp__daimon__create_automation",
  "mcp__daimon__open_terminal_with_command",
  "mcp__daimon__open_application",
  "mcp__daimon__close_application",
  "mcp__daimon__music_control",
  "mcp__daimon__play_music_by_name",
  "mcp__daimon__write_spreadsheet",
];
