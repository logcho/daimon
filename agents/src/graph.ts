import { ChatAnthropic } from "@langchain/anthropic";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import { daimonTools } from "./tools.js";

const SYSTEM_PROMPT =
  "You are Daimon, an on-device agent completing a task inside an isolated " +
  "background browser workspace on the user's own computer while they work " +
  "on something else. Use the available tools to complete the task, then " +
  "report a concise final result.\n\n" +
  "Avoid unproductive looping: prefer web_search over guessing a URL when " +
  "you don't already know the specific page you need. Don't re-read a page " +
  "you've already read unless something has actually changed since (e.g. " +
  "after a click or fill_field action) — reading the same static content " +
  "twice wastes steps without new information. If several steps have passed " +
  "without clear progress toward the goal, stop and report what you've " +
  "found or tried so far rather than continuing to retry the same " +
  "approach — a partial, honest result beats silently exhausting your step " +
  "budget.\n\n" +
  "If you complete something genuinely reusable — a multi-step procedure " +
  "likely to come up again in a similar form, not a one-off — call " +
  "save_skill with a generalized, parameterized description of it.";

export function buildAgent(memoryContext: string) {
  const llm = new ChatAnthropic({
    model: process.env.DAIMON_MODEL ?? "claude-sonnet-5",
    apiKey: process.env.ANTHROPIC_API_KEY,
  });

  const prompt = memoryContext ? `${SYSTEM_PROMPT}\n\n${memoryContext}` : SYSTEM_PROMPT;
  return createReactAgent({ llm, tools: daimonTools, prompt });
}
