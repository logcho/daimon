import { randomUUID } from "node:crypto";
import * as browser from "./browser.js";
import type { TaskEvent } from "./events.js";

export async function runDemoTask(instruction: string, emit: (event: TaskEvent) => void): Promise<void> {
  const steps = [
    { id: randomUUID(), label: "Opening https://example.com" },
    { id: randomUUID(), label: "Reading page content" },
    { id: randomUUID(), label: "Capturing screenshot" },
  ];

  try {
    emit({ type: "step", ...steps[0], status: "running" });
    const title = await browser.openUrl("https://example.com");
    emit({ type: "step", ...steps[0], status: "done" });

    emit({ type: "step", ...steps[1], status: "running" });
    const text = await browser.getPageText();
    emit({ type: "step", ...steps[1], status: "done" });

    emit({ type: "step", ...steps[2], status: "running" });
    await browser.screenshotBase64();
    emit({ type: "step", ...steps[2], status: "done" });

    emit({
      type: "done",
      result:
        `No ANTHROPIC_API_KEY was set, so I ran a scripted demo instead of "${instruction}": ` +
        `opened example.com ("${title}"), read ${text.length} characters of page text, and took ` +
        "a screenshot. The background workspace pipeline works end to end.",
    });
  } catch (err) {
    emit({ type: "error", message: err instanceof Error ? err.message : String(err) });
  }
}
