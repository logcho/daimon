import { z } from "zod";
import { tool } from "@langchain/core/tools";
import * as automation from "./automation.js";
import * as browser from "./browser.js";
import * as vault from "./vault.js";
import { indexNote, saveSkill } from "./memory.js";

export const daimonTools = [
  tool(
    async ({ url }: { url: string }) => {
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
    async ({ selector, text }: { selector: string; text: string }) => {
      await browser.fill(selector, text);
      return `Filled "${selector}" with the given text.`;
    },
    {
      name: "fill_field",
      description: "Fill a form field matched by a CSS selector with the given text.",
      schema: z.object({
        selector: z.string().describe("CSS selector for the input/textarea"),
        text: z.string().describe("The text to type into the field"),
      }),
    },
  ),
  tool(
    async ({ selector }: { selector: string }) => {
      await browser.click(selector);
      return `Clicked "${selector}".`;
    },
    {
      name: "click",
      description: "Click an element (button, link, checkbox, ...) matched by a CSS selector.",
      schema: z.object({ selector: z.string().describe("CSS selector for the element to click") }),
    },
  ),
  tool(
    async () => browser.getPageText(),
    {
      name: "read_page",
      description: "Read the visible text content of the current page, to decide what to do next.",
      schema: z.object({}),
    },
  ),
  tool(
    async ({ query }: { query: string }) => {
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
];
