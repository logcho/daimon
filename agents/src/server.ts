import { createServer } from "node:http";
import { runTurn } from "./run.js";
import { prepareWorkspace } from "./agent.js";
import type { TaskEvent } from "./events.js";
import { indexNote } from "./memory.js";
import { listNotes, readNote } from "./vault.js";

const PORT = Number(process.env.PORT ?? 4711);

// One-time startup scan: index whatever notes are already sitting in the
// vault (most importantly, notes the *user* wrote directly into a real
// Obsidian vault, never through the agent) so they're searchable planning
// context from this container's very first task, not just agent-authored
// ones written mid-session. Re-indexing something already indexed is
// harmless (that's exactly what `indexNote`'s upsert is for), so this is
// deliberately a plain full scan rather than change-detection.
async function indexExistingVaultNotes(): Promise<void> {
  const filenames = await listNotes();
  for (const filename of filenames) {
    try {
      const content = await readNote(filename);
      indexNote(filename, content);
    } catch (err) {
      console.error(`[daimon-agent] failed to index existing vault note "${filename}": ${err}`);
    }
  }
  console.error(`[daimon-agent] indexed ${filenames.length} existing vault note(s) on startup`);
}

await indexExistingVaultNotes();
// Makes `<vault>/skills` discoverable by the harness as project skills (via a
// `.claude/skills` symlink onto it) — see agent.ts. Best-effort, never throws.
await prepareWorkspace();

const server = createServer((req, res) => {
  // Every request lands in `docker logs <container>` (this only goes to
  // stdout/stderr — there's no other durable record of what a still-running
  // workspace container has seen), so it's the first thing worth checking
  // during a live stall, before the daemon's own stall timeout even fires.
  console.error(`[daimon-agent] ${req.method} ${req.url}`);

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
    req.on("end", () => {
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

      console.error(`[daimon-agent] received task: ${instruction}`);

      res.writeHead(200, {
        "content-type": "application/x-ndjson",
        "transfer-encoding": "chunked",
      });

      const emit = (event: TaskEvent) => {
        res.write(JSON.stringify(event) + "\n");
      };

      // `runTurn` already catches everything it can attribute to the turn
      // itself and emits/records a proper `error` event (see run.ts) — this
      // `.catch` is only a last-ditch backstop against something escaping
      // that (a bug in the catch path itself, an emit-time write failure if
      // the client already disconnected, ...). Without it, a rejection here
      // would be an unhandled promise rejection, which Node treats as fatal
      // and crashes the whole agent process instead of just this one turn's
      // response — silently turning "turn errored" into "container looks
      // stalled forever" from the daemon's point of view.
      runTurn(instruction, emit)
        .catch((err) => {
          console.error(`[daimon-agent] unhandled error running task: ${err instanceof Error ? err.stack ?? err.message : err}`);
          try {
            emit({ type: "error", message: err instanceof Error ? err.message : String(err) });
          } catch {
            // response may already be closed; nothing more we can do
          }
        })
        .finally(() => {
          res.end();
        });
    });
    return;
  }

  res.writeHead(404, { "content-type": "text/plain" }).end("not found");
});

server.listen(PORT, () => {
  console.log(`daimon-agent listening on :${PORT}`);
});
