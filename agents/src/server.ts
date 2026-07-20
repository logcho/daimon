import { createServer } from "node:http";
import { runTask } from "./run.js";
import type { TaskEvent } from "./events.js";

const PORT = Number(process.env.PORT ?? 4711);

const server = createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  if (req.method === "POST" && req.url === "/task") {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", async () => {
      let instruction: unknown;
      try {
        instruction = JSON.parse(body).instruction;
      } catch {
        res.writeHead(400, { "content-type": "text/plain" }).end("invalid json body");
        return;
      }
      if (typeof instruction !== "string" || !instruction.trim()) {
        res.writeHead(400, { "content-type": "text/plain" }).end("instruction is required");
        return;
      }

      res.writeHead(200, {
        "content-type": "application/x-ndjson",
        "transfer-encoding": "chunked",
      });

      const emit = (event: TaskEvent) => {
        res.write(JSON.stringify(event) + "\n");
      };

      await runTask(instruction, emit);
      res.end();
    });
    return;
  }

  res.writeHead(404, { "content-type": "text/plain" }).end("not found");
});

server.listen(PORT, () => {
  console.log(`daimon-agent listening on :${PORT}`);
});
