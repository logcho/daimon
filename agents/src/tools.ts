import { z } from "zod";
import { tool } from "@langchain/core/tools";
import * as browser from "./browser.js";

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
];
