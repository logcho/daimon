import { ChatAnthropic } from "@langchain/anthropic";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import { daimonTools } from "./tools.js";

const SYSTEM_PROMPT =
  "You are Daimon, an on-device agent completing a task inside an isolated " +
  "background browser workspace on the user's own computer while they work " +
  "on something else. Use the available tools to complete the task, then " +
  "report a concise final result.";

export function buildAgent() {
  const llm = new ChatAnthropic({
    model: process.env.DAIMON_MODEL ?? "claude-sonnet-5",
    apiKey: process.env.ANTHROPIC_API_KEY,
  });

  return createReactAgent({ llm, tools: daimonTools, prompt: SYSTEM_PROMPT });
}
