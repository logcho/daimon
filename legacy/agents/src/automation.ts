import { randomUUID } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { CronExpressionParser } from "cron-parser";

// The in-container mount point the daemon's `run_container` bind-mounts the
// host `automations/` directory onto (see `src-tauri/src/workspace.rs`'s
// `automations_mount` and `src-tauri/src/automation.rs`'s `automations_dir`)
// — same shape as `DAIMON_VAULT_DIR` in `vault.ts`. The container has no
// reverse channel to call back into the daemon (see `automation.rs`'s module
// doc), so this directory — not a network call — is how a pending request
// gets from here to the daemon's scheduler loop.
export const AUTOMATIONS_DIR = process.env.DAIMON_AUTOMATIONS_DIR ?? "/workspace/automations";

/**
 * Validates `schedule` and writes a pending-request file the daemon's
 * scheduler loop will pick up, validate again, and promote into the real
 * automations store.
 *
 * The `cron-parser` check here is purely so the agent gets fast,
 * in-conversation feedback on an obviously malformed schedule — it is *not*
 * the authoritative validator. The Rust daemon re-validates (with its own
 * `cron` crate) when it actually promotes the request; this container's
 * claim that its schedule string parses is never trusted as-is.
 */
export async function requestAutomation(name: string, instruction: string, schedule: string): Promise<void> {
  try {
    CronExpressionParser.parse(schedule);
  } catch (err) {
    throw new Error(
      `"${schedule}" is not a valid cron expression: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  await fs.mkdir(AUTOMATIONS_DIR, { recursive: true });
  const file = path.join(AUTOMATIONS_DIR, `pending-${randomUUID()}.json`);
  await fs.writeFile(file, JSON.stringify({ name, instruction, schedule }), "utf-8");
}

/**
 * Same shape as `requestAutomation`, but for a single, non-repeating
 * reminder — `remindAt` is an RFC3339 instant to fire at exactly once, not
 * a cron expression (a cron field is modulo-recurring by construction,
 * there's no "just this once" — see `Automation::once_at`'s doc comment in
 * `src-tauri/src/automation.rs`). Validated the same "fast in-conversation
 * feedback, not the authoritative check" way as `requestAutomation` — the
 * daemon re-validates when it promotes the pending request.
 */
export async function requestReminder(name: string, instruction: string, remindAt: string): Promise<void> {
  const parsed = new Date(remindAt);
  if (Number.isNaN(parsed.getTime())) {
    throw new Error(`"${remindAt}" is not a valid date/time`);
  }

  await fs.mkdir(AUTOMATIONS_DIR, { recursive: true });
  const file = path.join(AUTOMATIONS_DIR, `pending-${randomUUID()}.json`);
  await fs.writeFile(file, JSON.stringify({ name, instruction, onceAt: parsed.toISOString() }), "utf-8");
}
