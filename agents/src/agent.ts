import path from "node:path";
import fs from "node:fs/promises";
import { query, type CanUseTool, type Options, type Query, type SDKMessage, type SDKUserMessage } from "@anthropic-ai/claude-agent-sdk";
import { daimonToolServer } from "./tools.js";

// Replaces the old `graph.ts`, which despite its name contained no graph —
// just `createReactAgent` plus an 88-line prompt string. The agent loop,
// history, compaction, tool-call discipline, and subagents now come from the
// Claude Code harness; what's left here is Daimon's own configuration.

// What survives of the old system prompt. The `claude_code` preset already
// covers tool discipline, anti-looping, and output formatting, so everything
// here is either a hard product invariant or a disambiguation the harness has
// no way to infer. Deliberately NOT restated here (each now lives once, in
// the relevant tool's own description in tools.ts): the ref-vs-CSS-selector
// rule, and the full create_reminder / create_automation contrast.
const DAIMON_RULES = `You are Daimon, an ambient on-device assistant. You run in the background on the \
user's own machine while they get on with something else, and you report back when you're done.

# Never log the user in
Never attempt to log the user into a website yourself. Don't fill a password field, don't submit a \
login form, and don't ask the user to hand you credentials to type in. Your browser is invisible to \
them — they can't see what you're doing in it or verify that a password field goes where it should, \
so any offer to "log you in" is one they have no way to check. If a page you need sits behind a \
login your current browser profile doesn't already have, stop and tell the user, in your final \
result, to open Daimon's Settings and use "Login to browser" to sign in once in a real, visible \
Chrome window — that login then carries into all your future sessions without you ever handling the \
password. For Gmail and Spotify specifically, tell them to connect the account in Settings instead, \
which uses real OAuth and no password at all.

# Opening apps vs. browsing
A bare "open/launch/start <app>" request means open_application, full stop — don't reinterpret it as \
a web task just because the app also has a website. Reach for the browser tools only when the \
request names an actual task beyond opening something ("go to YouTube and play X" names a \
destination and an action). "play <song>" is neither — that's play_music_by_name.

# Scheduling
A request naming one concrete date or time is create_reminder. A request naming a cadence is \
create_automation. When in doubt, prefer create_reminder — it's recoverable, a runaway recurring job \
is not.

# Your working directory
Your working directory is the user's notes vault. Read, Write, Edit, Glob, and Grep all operate \
there and nowhere else on the user's disk. When you learn a genuinely reusable procedure — a \
multi-step task likely to recur in a similar form, not a one-off — write it as skills/<name>/\
SKILL.md so it's available next time and the user can read and edit it in Obsidian.

# Reporting back
Your final result lands in a small chat bubble in a floating panel, not a document. Write it like a \
short message to a colleague: lead with the outcome, keep it to a few sentences, and skip the \
narration of your own tool use unless the user actually asked how you did something. Don't restate \
the instruction back to them.`;

function vaultDir(): string {
  const dir = process.env.DAIMON_VAULT_DIR;
  if (!dir) throw new Error("DAIMON_VAULT_DIR is not set — the daemon always sets it (see workspace.rs)");
  return dir;
}

// Claude Code discovers project skills under `<cwd>/.claude/skills`, but a
// dot-directory is invisible in Obsidian — and the whole point of putting
// skills in the vault is that the user can read and edit them. So the real
// directory is the plainly-visible `<vault>/skills`, and this symlinks the
// path the harness looks in onto it. Both sides then see the same files.
//
// Best-effort: a vault on a filesystem without symlink support, or a
// pre-existing real `.claude/skills` directory, just means skills aren't
// auto-discovered — the agent can still Read them, and nothing else breaks.
async function linkSkillsIntoClaudeDir(vault: string): Promise<void> {
  const visible = path.join(vault, "skills");
  const claudeDir = path.join(vault, ".claude");
  const linkPath = path.join(claudeDir, "skills");
  try {
    await fs.mkdir(visible, { recursive: true });
    await fs.mkdir(claudeDir, { recursive: true });
    const existing = await fs.lstat(linkPath).catch(() => null);
    if (existing?.isSymbolicLink()) return;
    if (existing) return; // a real directory someone else owns — leave it alone
    await fs.symlink(path.relative(claudeDir, visible), linkPath, "dir");
  } catch (err) {
    console.error(`[daimon-agent] could not link vault skills into .claude/skills: ${err}`);
  }
}

// The non-disruption gate. `ARCHITECTURE.md` §5 requires that the agent never
// touches the user's real machine except through an explicit, reviewable
// channel — which is why open_terminal_with_command stages a command instead
// of running it. The harness's built-in file tools would quietly break that
// by reaching anywhere on disk, so they're confined to the vault here.
//
// Bash is not in `allowedTools` at all (see buildOptions), so this is defence
// in depth rather than the only line — but it's the one that survives someone
// later adding a tool to that list without thinking about paths.
export function buildCanUseTool(vault: string): CanUseTool {
  const vaultRoot = path.resolve(vault);
  // Every built-in file tool names its target with one of these.
  const PATH_FIELDS = ["file_path", "path", "notebook_path"];

  return async (toolName, input) => {
    // Daimon's own MCP tools police themselves — a browser ref or an app
    // name isn't a filesystem path and has nothing to check here.
    if (toolName.startsWith("mcp__daimon__")) return { behavior: "allow", updatedInput: input };

    for (const field of PATH_FIELDS) {
      const value = input[field];
      if (typeof value !== "string") continue;
      // path.resolve collapses `..` before the check, so a traversal like
      // `notes/../../.ssh/id_rsa` is caught rather than matched literally.
      const target = path.resolve(vaultRoot, value);
      if (target !== vaultRoot && !target.startsWith(vaultRoot + path.sep)) {
        return {
          behavior: "deny",
          message:
            `${toolName} was blocked: "${value}" resolves outside the user's vault. You can only ` +
            `read and write inside the vault (your working directory). Don't retry this path — ` +
            `use one inside the vault, or tell the user what you needed and why.`,
        };
      }
    }
    return { behavior: "allow", updatedInput: input };
  };
}

function buildOptions(vault: string): Options {
  const authMode = process.env.DAIMON_AUTH_MODE ?? "api_key";

  if (authMode === "api_key" && !process.env.ANTHROPIC_API_KEY) {
    throw new Error("DAIMON_AUTH_MODE=api_key but ANTHROPIC_API_KEY is not set");
  }

  // The single most expensive mistake available in this file. In subscription
  // mode the harness must authenticate through the user's Claude Code login;
  // an ANTHROPIC_API_KEY present in the environment *shadows* that OAuth
  // profile, so a stray key would silently bill the user's API account for
  // every turn while the UI cheerfully reports "using your subscription".
  // The daemon already declines to set it (workspace.rs), but it can also
  // arrive from the daemon's own inherited environment, so strip it here too.
  const env = { ...process.env } as Record<string, string>;
  if (authMode === "subscription") delete env.ANTHROPIC_API_KEY;

  return {
    systemPrompt: { type: "preset", preset: "claude_code", append: DAIMON_RULES },
    cwd: vault,
    env,
    mcpServers: { daimon: daimonToolServer },
    // `allowedTools` is deliberately NOT set. A bare entry there auto-approves
    // that tool *before* `canUseTool` is consulted — the SDK emits a
    // CLAUDE_SDK_CAN_USE_TOOL_SHADOWED warning saying so — which silently
    // turned the vault confinement gate below into dead code the first time
    // this was written. Leaving the list empty makes every non-denied tool
    // fall through to the callback, which is the only place the path check
    // actually runs.
    //
    // `disallowedTools` is a hard deny that never reaches the callback. Bash
    // would let the agent run arbitrary commands on the user's machine, which
    // is precisely what open_terminal_with_command's stage-don't-execute
    // design exists to avoid; revisit it behind the SDK's `sandbox` option
    // rather than by listing it here. WebFetch/WebSearch are denied because
    // Daimon routes all web access through the stateful logged-in PinchTab
    // browser instead, and Task because subagents would each need their own
    // copy of this gate.
    disallowedTools: ["Bash", "WebFetch", "WebSearch", "Task"],
    canUseTool: buildCanUseTool(vault),
    permissionMode: "default",
    // Load project settings (so `<vault>/.claude/skills` is discovered) but
    // NOT 'user' — `~/.claude/settings.json` is the user's own Claude Code
    // config, with their coding MCP servers, hooks, and permissions in it.
    // Daimon inheriting that would be surprising and occasionally dangerous.
    settingSources: ["project"],
    skills: "all",
    model: process.env.DAIMON_MODEL,
    includePartialMessages: false,
  };
}

// Feeds the harness's streaming-input mode. `query()` takes an AsyncIterable
// that stays open for the life of the session; each turn pushes exactly one
// message into it and then reads events back off the shared output iterator
// until that turn's result arrives.
class MessageQueue implements AsyncIterable<SDKUserMessage> {
  private pending: SDKUserMessage[] = [];
  private waiting: ((result: IteratorResult<SDKUserMessage>) => void) | null = null;
  private closed = false;

  push(instruction: string): void {
    const message: SDKUserMessage = {
      type: "user",
      message: { role: "user", content: instruction },
      parent_tool_use_id: null,
    };
    if (this.waiting) {
      const resolve = this.waiting;
      this.waiting = null;
      resolve({ value: message, done: false });
      return;
    }
    this.pending.push(message);
  }

  close(): void {
    this.closed = true;
    this.waiting?.({ value: undefined as never, done: true });
    this.waiting = null;
  }

  async *[Symbol.asyncIterator](): AsyncIterator<SDKUserMessage> {
    for (;;) {
      const queued = this.pending.shift();
      if (queued) {
        yield queued;
        continue;
      }
      if (this.closed) return;
      const next = await new Promise<IteratorResult<SDKUserMessage>>((resolve) => {
        this.waiting = resolve;
      });
      if (next.done) return;
      yield next.value;
    }
  }
}

// One per agent process, and the daemon dedicates one process per session for
// its whole lifetime (workspace.rs reuses the same port across every message
// in a session) — so process-scoped state is session-scoped state, and the
// harness owns the conversation history that `run.ts` used to keep in a
// module-global array and hand-roll rollback for.
export class Session {
  private queue = new MessageQueue();
  private stream: Query | null = null;
  private iterator: AsyncIterator<SDKMessage> | null = null;
  // Turns are serialized by chaining onto this. Two overlapping turns would
  // interleave on the shared output iterator and cross their events into each
  // other's NDJSON response, and `emitter.ts`'s single active-emit slot
  // assumes exactly one turn is live.
  private tail: Promise<void> = Promise.resolve();

  private ensureStarted(): AsyncIterator<SDKMessage> {
    if (!this.iterator) {
      this.stream = query({ prompt: this.queue, options: buildOptions(vaultDir()) });
      this.iterator = this.stream[Symbol.asyncIterator]();
    }
    return this.iterator;
  }

  /** Serializes `fn` after every turn already queued on this session. */
  run<T>(fn: (iterator: AsyncIterator<SDKMessage>, send: (text: string) => void) => Promise<T>): Promise<T> {
    const result = this.tail.then(() => {
      const iterator = this.ensureStarted();
      return fn(iterator, (text) => this.queue.push(text));
    });
    // Chained off `result` but swallowing its rejection, so one failed turn
    // doesn't poison every turn queued behind it.
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  async close(): Promise<void> {
    this.queue.close();
    await this.stream?.interrupt().catch(() => undefined);
  }
}

export async function prepareWorkspace(): Promise<void> {
  await linkSkillsIntoClaudeDir(vaultDir());
}

export const session = new Session();
