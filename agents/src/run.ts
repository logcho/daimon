import { randomUUID } from "node:crypto";
import { AIMessage, HumanMessage, ToolMessage } from "@langchain/core/messages";
import { buildAgent } from "./graph.js";
import { runDemoTask } from "./demo.js";
import type { TaskEvent } from "./events.js";

export async function runTask(instruction: string, emit: (event: TaskEvent) => void): Promise<void> {
  if (!process.env.ANTHROPIC_API_KEY) {
    await runDemoTask(instruction, emit);
    return;
  }

  const thinkingId = randomUUID();
  emit({ type: "step", id: thinkingId, label: "Thinking", status: "running" });

  try {
    const agent = buildAgent();
    const stream = await agent.stream(
      { messages: [new HumanMessage(instruction)] },
      { streamMode: "updates" },
    );

    let finalResult = "";
    for await (const chunk of stream as AsyncIterable<Record<string, { messages?: unknown[] }>>) {
      const agentUpdate = chunk.agent;
      if (agentUpdate?.messages) {
        for (const message of agentUpdate.messages as AIMessage[]) {
          for (const call of message.tool_calls ?? []) {
            emit({ type: "step", id: call.id ?? randomUUID(), label: `Running ${call.name}`, status: "running" });
          }
          if (typeof message.content === "string" && message.content) {
            finalResult = message.content;
          }
        }
      }

      const toolsUpdate = chunk.tools;
      if (toolsUpdate?.messages) {
        for (const message of toolsUpdate.messages as ToolMessage[]) {
          emit({
            type: "step",
            id: message.tool_call_id ?? randomUUID(),
            label: `Completed ${message.name ?? "tool"}`,
            status: "done",
          });
        }
      }
    }

    emit({ type: "step", id: thinkingId, label: "Thinking", status: "done" });
    emit({ type: "done", result: finalResult || "Task complete." });
  } catch (err) {
    emit({ type: "error", message: err instanceof Error ? err.message : String(err) });
  }
}
