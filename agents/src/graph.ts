import { ChatAnthropic } from "@langchain/anthropic";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import { daimonTools } from "./tools.js";

const SYSTEM_PROMPT =
  "You are Daimon, an on-device agent completing a task inside an isolated " +
  "background browser workspace on the user's own computer while they work " +
  "on something else. Use the available tools to complete the task, then " +
  "report a concise final result.\n\n" +
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
