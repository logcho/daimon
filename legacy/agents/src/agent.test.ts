import assert from "node:assert/strict";
import test from "node:test";
import { buildCanUseTool } from "./agent.js";

// The vault confinement gate is a security boundary (ARCHITECTURE.md §5), and
// it has already been silently dead once: listing the file tools in
// `allowedTools` auto-approved them *before* the callback was consulted, so
// the check never ran. Nothing about that failure was visible from the
// outside — the agent behaved correctly anyway, because the model happened to
// decline on its own. These tests exercise the gate directly rather than
// through a model, so a regression fails here instead of silently widening
// what the agent can reach on the user's disk.

const VAULT = "/tmp/daimon-test-vault";
const gate = buildCanUseTool(VAULT);
const noop = { signal: new AbortController().signal };

// `CanUseTool` may return null, meaning "the consumer already sent the
// control_response out of band". This gate never does that — it always
// decides — so a null here is a bug in the gate, not a case to handle.
async function decide(tool: string, input: Record<string, unknown>) {
  const result = await gate(tool, input, noop as Parameters<typeof gate>[2]);
  assert.ok(result, `${tool} returned null instead of an allow/deny decision`);
  return result;
}

test("allows a write inside the vault", async () => {
  const result = await decide("Write", { file_path: `${VAULT}/notes.md` });
  assert.equal(result.behavior, "allow");
});

test("allows a vault-relative path", async () => {
  const result = await decide("Read", { file_path: "notes/todo.md" });
  assert.equal(result.behavior, "allow");
});

test("denies an absolute path outside the vault", async () => {
  const result = await decide("Write", { file_path: "/Users/someone/.ssh/id_rsa" });
  assert.equal(result.behavior, "deny");
});

test("denies traversal that escapes via ..", async () => {
  const result = await decide("Write", { file_path: "notes/../../../etc/passwd" });
  assert.equal(result.behavior, "deny");
});

test("denies a sibling directory sharing the vault's name prefix", async () => {
  // `/tmp/daimon-test-vault-evil` starts with the vault path as a string but
  // is a different directory — a startsWith check without the separator would
  // wrongly allow it.
  const result = await decide("Write", { file_path: `${VAULT}-evil/x.md` });
  assert.equal(result.behavior, "deny");
});

test("checks every path-bearing field, not just file_path", async () => {
  assert.equal((await decide("Grep", { path: "/etc" })).behavior, "deny");
  assert.equal((await decide("NotebookEdit", { notebook_path: "/etc/x.ipynb" })).behavior, "deny");
});

test("allows the vault root itself", async () => {
  const result = await decide("Glob", { path: VAULT });
  assert.equal(result.behavior, "allow");
});

test("lets Daimon's own MCP tools through without a path check", async () => {
  // A browser ref or an app name is not a filesystem path; these tools police
  // themselves and must not be tripped by a field that happens to be named
  // `path` (write_spreadsheet resolves its own destination).
  const result = await decide("mcp__daimon__open_application", { name: "Spotify" });
  assert.equal(result.behavior, "allow");
});
