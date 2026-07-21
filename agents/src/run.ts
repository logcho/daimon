import { randomUUID } from "node:crypto";
import { AIMessage, HumanMessage, ToolMessage } from "@langchain/core/messages";
import { buildAgent } from "./graph.js";
import { runDemoTask } from "./demo.js";
import { formatMemoryContext, recordTask, searchSkills, searchTasks } from "./memory.js";
import type { TaskEvent } from "./events.js";

export async function runTask(instruction: string, emit: (event: TaskEvent) => void): Promise<void> {
  if (!process.env.ANTHROPIC_API_KEY) {
    await runDemoTask(instruction, emit);
    return;
  }

  const thinkingId = randomUUID();
  emit({ type: "step", id: thinkingId, label: "Thinking", status: "running" });

  try {
    const memoryContext = formatMemoryContext(searchTasks(instruction, 3), searchSkills(instruction, 3));
    if (memoryContext) console.error(`[daimon-agent] memory context:\n${memoryContext}`);
    const agent = buildAgent(memoryContext);
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
            emit({
              type: "step",
              id: call.id ?? randomUUID(),
              label: call.name,
              status: "running",
              tool: call.name,
            });
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
            label: message.name ?? "tool",
            status: "done",
            tool: message.name,
          });
        }
      }
    }

    emit({ type: "step", id: thinkingId, label: "Thinking", status: "done" });
    const result = finalResult || "Task complete.";
    emit({ type: "done", result });
    recordTask(instruction, result, "done");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    emit({ type: "error", message });
    recordTask(instruction, message, "error");
  }
}
