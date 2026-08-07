//! Cron-scheduled automations: recurring instructions that fire on their own
//! without the user opening the app or typing anything (`ARCHITECTURE.md`
//! §3B/C, `PROMPT.md`'s Phase 10 section).
//!
//! **The real design wrinkle:** the agent runs as a native OS process that
//! only ever gets called *into* over HTTP by this daemon (`POST /task`, see
//! `session.rs`) — there's no reverse channel for it to call back out, and
//! opening one (a daemon-side listener the agent process could hit) would
//! be a real trust-direction reversal. So, same shape as `memory/` and
//! `vault/` (both passed to the agent process as plain host-path env
//! vars — see `workspace.rs`'s `spawn_node_agent`): a third directory,
//! `automations_dir()`, passed as `DAIMON_AUTOMATIONS_DIR`. The agent's
//! `create_automation` tool (`agents/src/automation.ts`) writes a small
//! pending-request JSON file into that directory; `poll_and_fire`'s scan
//! step (below) picks it up, re-validates it, and promotes it into the real
//! store — the agent process never reaches back into the daemon directly.
//!
//! Storage is a plain `automations.json` under `workspace::data_dir()`
//! (gitignored in dev, same treatment as `/memory/`/`/vault/`) — a small,
//! infrequently-written list, not worth a database. The in-memory
//! `AUTOMATIONS` cache below is this process's single source of truth while
//! running; every mutation (new automation, enable/disable, delete, or a
//! recorded run outcome) is immediately persisted back to disk.
//!
//! Schedules are evaluated in UTC (via `chrono::Utc::now()`) — no
//! timezone picker, a deliberately deferred stretch goal per the Phase 10
//! brief.

use std::path::PathBuf;
use std::str::FromStr;
use std::sync::LazyLock;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tauri::Emitter;
use tokio::sync::Mutex;

use crate::session;
use crate::workspace;

#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct Automation {
    id: String,
    name: String,
    instruction: String,
    /// Standard 5-field cron expression (minute hour day month weekday),
    /// e.g. `"0 8 * * *"` for daily at 8am. Stored exactly as given —
    /// `cron_schedule` below is what turns it into something we can
    /// actually evaluate. Empty/unused when `once_at` is set — the two are
    /// mutually exclusive (see `once_at`'s own doc comment).
    schedule: String,
    /// If set, this is a one-shot reminder rather than a recurring
    /// automation: an RFC3339 instant to fire at exactly once, not a cron
    /// expression. Added for the "remind me about X on this date" case,
    /// which a plain cron schedule can't represent (every cron field is
    /// modulo-recurring by construction — there's no "just this once").
    /// `is_due` branches on whether this is `Some`; after a one-shot fires,
    /// `record_run_outcome` sets `enabled = false` rather than deleting it,
    /// so it stays visible in the automations list as "fired" history
    /// instead of silently disappearing.
    #[serde(default)]
    once_at: Option<String>,
    enabled: bool,
    created_at: String,
    last_run_at: Option<String>,
    /// `"done"` | `"error"` — mirrors `session::EphemeralOutcome::status`.
    last_run_status: Option<String>,
    last_run_result: Option<String>,
}

/// This process's live automations store, loaded once from
/// `automations.json` on first access and written back to disk after every
/// mutation — same "in-memory cache backed by a file" shape as
/// `workspace.rs`'s `SESSION_PORTS`, just persisted rather than purely
/// runtime state.
static AUTOMATIONS: LazyLock<Mutex<Vec<Automation>>> = LazyLock::new(|| Mutex::new(load_automations()));

pub(crate) fn automations_path() -> PathBuf {
    workspace::data_dir().join("automations.json")
}

/// The pending-request directory — created if missing, since nothing else
/// (unlike `memory/`/`vault/`, which get created by their own modules on
/// first real use) otherwise guarantees this directory exists before
/// `workspace::spawn_node_agent` passes it to the agent process as
/// `DAIMON_AUTOMATIONS_DIR`.
pub(crate) fn automations_dir() -> PathBuf {
    let dir = workspace::data_dir().join("automations");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn load_automations() -> Vec<Automation> {
    let path = automations_path();
    let Ok(content) = std::fs::read_to_string(&path) else {
        return Vec::new();
    };
    serde_json::from_str(&content).unwrap_or_else(|e| {
        log::warn!("automations.json is malformed ({e}), starting from an empty list");
        Vec::new()
    })
}

fn save_automations(automations: &[Automation]) {
    let path = automations_path();
    match serde_json::to_string_pretty(automations) {
        Ok(json) => {
            if let Err(e) = std::fs::write(&path, json) {
                log::error!("failed to write {}: {e}", path.display());
            }
        }
        Err(e) => log::error!("failed to serialize automations: {e}"),
    }
}

fn now_rfc3339() -> String {
    Utc::now().to_rfc3339()
}

/// Standard 5-field cron (minute hour day-of-month month day-of-week) needs
/// a leading seconds field to satisfy the `cron` crate's 6/7-field grammar —
/// prepending a literal `0 ` maps the two one-to-one (seconds always `:00`),
/// which is exactly the granularity Phase 10 needs (nothing here schedules
/// sub-minute).
fn parse_cron(schedule: &str) -> Result<cron::Schedule, String> {
    let expr = format!("0 {}", schedule.trim());
    cron::Schedule::from_str(&expr).map_err(|e| format!("invalid cron schedule \"{schedule}\": {e}"))
}

/// Whether `automation`'s next occurrence after its last run (or its
/// creation, if it's never run) has already passed. `enabled` is checked by
/// the caller, not here.
fn is_due(automation: &Automation, now: DateTime<Utc>) -> bool {
    if let Some(once_at) = &automation.once_at {
        // One-shot: due once, at exactly `once_at`, and never again — a
        // one-shot that has already run is disabled by `record_run_outcome`
        // (see that function), but this guard is kept here too so `is_due`
        // stays correct on its own even before that write lands.
        if automation.last_run_at.is_some() {
            return false;
        }
        return DateTime::parse_from_rfc3339(once_at).is_ok_and(|dt| dt.with_timezone(&Utc) <= now);
    }

    let Ok(schedule) = parse_cron(&automation.schedule) else {
        // Shouldn't happen for anything that made it into the store (both
        // `create_automation` and the pending-request promotion below
        // validate first) — but a schedule that somehow went stale between
        // versions should never fire rather than panicking the scheduler
        // loop.
        return false;
    };

    let after = automation
        .last_run_at
        .as_deref()
        .or(Some(automation.created_at.as_str()))
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .map(|dt| dt.with_timezone(&Utc));

    let Some(after) = after else {
        return false;
    };

    matches!(schedule.after(&after).next(), Some(next) if next <= now)
}

async fn add_automation(automation: Automation) {
    let mut automations = AUTOMATIONS.lock().await;
    automations.push(automation);
    save_automations(&automations);
}

#[tauri::command]
pub async fn list_automations() -> Result<Vec<Automation>, String> {
    Ok(AUTOMATIONS.lock().await.clone())
}

#[tauri::command]
pub async fn create_automation(name: String, instruction: String, schedule: String) -> Result<Automation, String> {
    parse_cron(&schedule)?;

    let automation = Automation {
        id: uuid::Uuid::new_v4().to_string(),
        name,
        instruction,
        schedule,
        once_at: None,
        enabled: true,
        created_at: now_rfc3339(),
        last_run_at: None,
        last_run_status: None,
        last_run_result: None,
    };
    add_automation(automation.clone()).await;
    Ok(automation)
}

/// Same shape as `create_automation` but for a single, non-repeating
/// reminder — see `Automation::once_at`'s doc comment for why this needs a
/// distinct field/command rather than trying to shoehorn "just once" into a
/// cron expression. `remind_at` must be an RFC3339 instant; validated here
/// the same way `create_automation` validates its cron string.
#[tauri::command]
pub async fn create_reminder(name: String, instruction: String, remind_at: String) -> Result<Automation, String> {
    DateTime::parse_from_rfc3339(&remind_at).map_err(|e| format!("invalid reminder time \"{remind_at}\": {e}"))?;

    let automation = Automation {
        id: uuid::Uuid::new_v4().to_string(),
        name,
        instruction,
        schedule: String::new(),
        once_at: Some(remind_at),
        enabled: true,
        created_at: now_rfc3339(),
        last_run_at: None,
        last_run_status: None,
        last_run_result: None,
    };
    add_automation(automation.clone()).await;
    Ok(automation)
}

#[tauri::command]
pub async fn set_automation_enabled(id: String, enabled: bool) -> Result<(), String> {
    let mut automations = AUTOMATIONS.lock().await;
    let automation = automations
        .iter_mut()
        .find(|a| a.id == id)
        .ok_or_else(|| format!("no automation with id {id}"))?;
    automation.enabled = enabled;
    save_automations(&automations);
    Ok(())
}

#[tauri::command]
pub async fn delete_automation(id: String) -> Result<(), String> {
    let mut automations = AUTOMATIONS.lock().await;
    let before = automations.len();
    automations.retain(|a| a.id != id);
    if automations.len() == before {
        return Err(format!("no automation with id {id}"));
    }
    save_automations(&automations);
    Ok(())
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PendingAutomationRequest {
    name: String,
    instruction: String,
    /// Exactly one of `schedule`/`once_at` (`onceAt` on the wire, matching
    /// `agents/src/automation.ts`'s JSON) is set — mirrors the same
    /// mutual-exclusivity as `Automation` itself (see that struct's
    /// `once_at` doc comment). Written by either `agents/src/automation.ts`'s
    /// `requestAutomation` (recurring) or `requestReminder` (one-shot).
    #[serde(default)]
    schedule: Option<String>,
    #[serde(default)]
    once_at: Option<String>,
}

/// Scans `automations_dir()` for `pending-*.json` files left by the agent's
/// `create_automation`/`create_reminder` tools (`agents/src/automation.ts`).
/// Each one is re-validated here — the container's claim that its cron
/// string or reminder time parses isn't trusted as-is, this is the
/// authoritative check — and either promoted into the real store (fresh id,
/// `enabled: true`, run fields unset) or discarded with a logged warning.
/// Either way the pending file is removed: a malformed request is not
/// retried forever.
async fn promote_pending_requests() {
    let dir = automations_dir();
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return;
    };

    for entry in entries.flatten() {
        let path = entry.path();
        let is_pending_json = entry.file_name().to_string_lossy().starts_with("pending-")
            && path.extension().and_then(|e| e.to_str()) == Some("json");
        if !is_pending_json {
            continue;
        }

        match promote_one(&path).await {
            Ok(automation) => log::info!(
                "automation: promoted pending request into automation {} ({:?})",
                automation.id,
                automation.name
            ),
            Err(e) => log::warn!(
                "automation: discarding malformed pending request {}: {e}",
                path.display()
            ),
        }

        let _ = std::fs::remove_file(&path);
    }
}

async fn promote_one(path: &std::path::Path) -> Result<Automation, String> {
    let content = std::fs::read_to_string(path).map_err(|e| format!("failed to read: {e}"))?;
    let request: PendingAutomationRequest =
        serde_json::from_str(&content).map_err(|e| format!("failed to parse: {e}"))?;

    let (schedule, once_at) = match (request.schedule, request.once_at) {
        (Some(schedule), None) => {
            parse_cron(&schedule)?;
            (schedule, None)
        }
        (None, Some(once_at)) => {
            DateTime::parse_from_rfc3339(&once_at)
                .map_err(|e| format!("invalid reminder time \"{once_at}\": {e}"))?;
            (String::new(), Some(once_at))
        }
        _ => return Err("pending request must set exactly one of schedule/onceAt".to_string()),
    };

    let automation = Automation {
        id: uuid::Uuid::new_v4().to_string(),
        name: request.name,
        instruction: request.instruction,
        schedule,
        once_at,
        enabled: true,
        created_at: now_rfc3339(),
        last_run_at: None,
        last_run_status: None,
        last_run_result: None,
    };
    add_automation(automation.clone()).await;
    Ok(automation)
}

async fn record_run_outcome(id: &str, outcome: session::EphemeralOutcome) {
    let mut automations = AUTOMATIONS.lock().await;
    if let Some(automation) = automations.iter_mut().find(|a| a.id == id) {
        automation.last_run_at = Some(now_rfc3339());
        automation.last_run_status = Some(outcome.status);
        automation.last_run_result = outcome.result;
        // A one-shot reminder only ever fires once by definition — disable
        // it rather than delete it, so it stays visible in the automations
        // list as "fired" history (with its result) instead of silently
        // vanishing. `is_due` would also refuse to re-fire it (guarded on
        // `last_run_at`), but disabling makes that explicit in the UI too.
        if automation.once_at.is_some() {
            automation.enabled = false;
        }
    }
    save_automations(&automations);
}

/// One scheduler tick: promote whatever pending requests are waiting, then
/// fire every enabled automation that's due. Called on a fixed poll
/// interval from `lib.rs`'s `run()` — see that call site for the interval
/// and rationale — but written to be trivially callable a single time (as
/// the tests below do), rather than only meaningful as part of a loop.
pub(crate) async fn poll_and_fire<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    promote_pending_requests().await;

    let now = Utc::now();
    let due: Vec<Automation> = {
        let automations = AUTOMATIONS.lock().await;
        automations.iter().filter(|a| a.enabled && is_due(a, now)).cloned().collect()
    };

    for automation in due {
        log::info!("automation {} ({:?}): due, firing", automation.id, automation.name);
        // Confirmed empirically as a real, live bug, not a hypothetical: an
        // ephemeral run has zero memory of the original conversation that
        // scheduled it, so an instruction like "Remind the user: X" — sound
        // advice for how to *phrase* a reminder — got taken completely
        // literally and the model called create_reminder *again*, recursively
        // rescheduling something that had just fired instead of just stating
        // X. This prefix runs only for the actual firing, not for
        // create_automation/create_reminder's own preview text anywhere else,
        // and makes explicit what the instruction text alone couldn't:
        // this IS the scheduled execution, not a request to schedule one.
        let effective_instruction = format!(
            "(This instruction is firing automatically on its own schedule — it already IS the \
             scheduled reminder/automation running, not a request to set one up. Just do what it \
             says, or state its message directly as your result. Do not call create_reminder or \
             create_automation in response to this unless the instruction below explicitly asks \
             you to schedule something else, separate from itself.)\n\n{}",
            automation.instruction
        );
        let outcome = session::run_ephemeral(app, effective_instruction).await;
        log::info!(
            "automation {} ({:?}): run finished, status={}",
            automation.id,
            automation.name,
            outcome.status
        );
        notify_run_outcome(app, &automation, &outcome);
        if automation.once_at.is_some() {
            emit_reminder_fired(app, &automation, &outcome);
        }
        record_run_outcome(&automation.id, outcome).await;
    }
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ReminderFiredPayload {
    id: String,
    name: String,
    status: String,
    result: Option<String>,
}

/// A dedicated event, separate from the generic `session-status` channel a
/// fired reminder's ephemeral run also streams over (`session::run_ephemeral`
/// → `run_and_stream`) — landing a reminder as just another entry in the
/// ordinary session list is exactly what the user reported as too easy to
/// miss. The frontend's `onReminderFired` listener (see `src/lib/api.ts`)
/// reacts to this by force-expanding the panel and showing a dedicated,
/// dismiss-until-acknowledged alert, regardless of whichever tab/session is
/// currently showing. Only emitted for actual one-shot reminders
/// (`once_at.is_some()`) — a recurring `create_automation` firing doesn't
/// get this treatment, matching the user's specific complaint about
/// reminders, not automations generally.
fn emit_reminder_fired<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    automation: &Automation,
    outcome: &session::EphemeralOutcome,
) {
    let payload = ReminderFiredPayload {
        id: automation.id.clone(),
        name: automation.name.clone(),
        status: outcome.status.clone(),
        result: outcome.result.clone(),
    };
    let _ = app.emit("reminder-fired", payload);
}

/// A fired automation was previously completely silent unless the app
/// window happened to already be open and mounted at the exact moment it
/// ran — the `session-status` channel it also streams over has no reach
/// beyond a live webview. This is what makes "remind me about X on this
/// date" an actual reminder rather than a line in a JSON file nobody's
/// looking at: a real macOS notification, fired regardless of whether the
/// app window is open. Best-effort — a failure to show a notification (e.g.
/// the user has never granted notification permission) should never be
/// treated as the automation run itself having failed.
fn notify_run_outcome<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    automation: &Automation,
    outcome: &session::EphemeralOutcome,
) {
    use tauri_plugin_notification::NotificationExt;

    let title = if automation.once_at.is_some() {
        format!("Reminder: {}", automation.name)
    } else {
        automation.name.clone()
    };
    let body = match outcome.status.as_str() {
        "error" => outcome.result.clone().unwrap_or_else(|| "Something went wrong.".to_string()),
        _ => outcome
            .result
            .clone()
            .unwrap_or_else(|| "Done — no summary was returned.".to_string()),
    };

    if let Err(e) = app.notification().builder().title(title).body(body).show() {
        log::warn!("automation {} ({:?}): failed to show notification: {e}", automation.id, automation.name);
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex as StdMutex};
    use std::time::Duration;

    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::{Listener, WebviewWindowBuilder};

    use super::*;

    fn invoke_request(cmd: &str, body: serde_json::Value) -> InvokeRequest {
        InvokeRequest {
            cmd: cmd.into(),
            callback: CallbackFn(0),
            error: CallbackFn(1),
            url: "tauri://localhost".parse().unwrap(),
            body: body.into(),
            headers: Default::default(),
            invoke_key: INVOKE_KEY.to_string(),
        }
    }

    /// Test-only insertion that (unlike `create_automation`) lets a test
    /// force an automation straight into an already-due state, rather than
    /// waiting on a real wall-clock cron boundary.
    async fn insert_for_test(automation: Automation) {
        add_automation(automation).await;
    }

    async fn remove_for_test(id: &str) {
        let mut automations = AUTOMATIONS.lock().await;
        automations.retain(|a| a.id != id);
        save_automations(&automations);
    }

    #[test]
    fn parse_cron_accepts_standard_five_field_expressions_and_rejects_garbage() {
        assert!(parse_cron("0 8 * * *").is_ok(), "a standard daily-at-8am expression should parse");
        assert!(parse_cron("not a cron expression").is_err(), "garbage should be rejected, not panic");
    }

    #[test]
    fn is_due_true_when_next_occurrence_after_last_run_has_passed() {
        let automation = Automation {
            id: "test".into(),
            name: "test".into(),
            instruction: "noop".into(),
            schedule: "* * * * *".into(), // every minute
            once_at: None,
            enabled: true,
            created_at: "2020-01-01T00:00:00Z".into(),
            last_run_at: Some("2020-01-01T00:00:00Z".into()),
            last_run_status: None,
            last_run_result: None,
        };
        assert!(is_due(&automation, Utc::now()), "an every-minute schedule last run in 2020 should be due now");
    }

    #[test]
    fn is_due_false_for_a_daily_schedule_that_just_ran() {
        let automation = Automation {
            id: "test".into(),
            name: "test".into(),
            instruction: "noop".into(),
            schedule: "0 8 * * *".into(),
            once_at: None,
            enabled: true,
            created_at: "2020-01-01T00:00:00Z".into(),
            last_run_at: Some(Utc::now().to_rfc3339()),
            last_run_status: None,
            last_run_result: None,
        };
        assert!(
            !is_due(&automation, Utc::now()),
            "a daily schedule that just ran should not be due again immediately"
        );
    }

    /// Same ACL-reachability pattern as `vault.rs`'s `vault_commands_clear_the_acl` —
    /// a missing `"allow-<command>"` capability entry compiles fine and only
    /// fails at runtime.
    #[test]
    fn automation_commands_clear_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let list = get_ipc_response(&webview, invoke_request("list_automations", serde_json::json!({})))
            .expect("list_automations should be allowed by the capability");
        let _: Vec<Automation> = list.deserialize().expect("expected a Vec<Automation>");

        let create = get_ipc_response(
            &webview,
            invoke_request(
                "create_automation",
                serde_json::json!({ "name": "acl test", "instruction": "noop", "schedule": "0 8 * * *" }),
            ),
        )
        .expect("create_automation should be allowed by the capability");
        let created: Automation = create.deserialize().expect("expected an Automation");

        let set_enabled = get_ipc_response(
            &webview,
            invoke_request(
                "set_automation_enabled",
                serde_json::json!({ "id": created.id, "enabled": false }),
            ),
        );
        assert!(set_enabled.is_ok(), "set_automation_enabled should be ACL-allowed, got {set_enabled:?}");

        let delete = get_ipc_response(
            &webview,
            invoke_request("delete_automation", serde_json::json!({ "id": created.id })),
        );
        assert!(delete.is_ok(), "delete_automation should be ACL-allowed, got {delete:?}");

        // An invalid schedule is expected to be rejected by create_automation's
        // own validation — the point here is that it's *reachable* at all (an
        // Err from application logic, not an ACL "not allowed" Err).
        let bad_schedule = get_ipc_response(
            &webview,
            invoke_request(
                "create_automation",
                serde_json::json!({ "name": "bad", "instruction": "noop", "schedule": "not-a-cron" }),
            ),
        );
        assert!(bad_schedule.is_err(), "an invalid cron expression should be rejected");
        let message = bad_schedule.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "create_automation should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }

    /// The core Phase 10 behavior: a due, enabled automation actually fires
    /// through the real session machinery (a live native workspace) and, on
    /// completion, its PinchTab/Node processes are gone — unlike an
    /// interactive session (see `session.rs`'s own test asserting the
    /// opposite for `start_session`). Forces "due" via a far-in-the-past
    /// `last_run_at` rather than waiting on a real cron boundary, per the
    /// Phase 10 brief.
    #[tokio::test(flavor = "multi_thread")]
    async fn poll_and_fire_runs_a_due_automation_and_tears_down_its_workspace() {
        // `notify_run_outcome` calls `app.notification()`, which needs the
        // plugin's state `.manage()`d — in the real app this happens because
        // `run()` puts `tauri_plugin_notification::init()` on the builder
        // before it ever reaches `build_app` (see `run()`'s own comment on
        // why `log`/`global_shortcut` are registered there and not inside
        // `build_app` itself); a bare `mock_builder()` skips that, so this
        // test has to add it back itself or `app.notification()` panics with
        // "state() called before manage()".
        let app = crate::build_app(mock_builder().plugin(tauri_plugin_notification::init()));

        let events: Arc<StdMutex<Vec<serde_json::Value>>> = Arc::new(StdMutex::new(Vec::new()));
        let events_for_listener = events.clone();
        app.listen("session-status", move |event| {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                events_for_listener.lock().unwrap().push(payload);
            }
        });

        let automation = Automation {
            id: uuid::Uuid::new_v4().to_string(),
            name: "automation test".into(),
            instruction: "say hello".into(),
            schedule: "* * * * *".into(),
            once_at: None,
            enabled: true,
            created_at: "2020-01-01T00:00:00Z".into(),
            last_run_at: Some("2020-01-01T00:00:00Z".into()),
            last_run_status: None,
            last_run_result: None,
        };
        let id = automation.id.clone();
        insert_for_test(automation).await;

        // Bounded so a genuinely hung run doesn't wedge the test suite —
        // generous enough to clear a real done/error round trip through the
        // live workspace (session.rs's own tests budget 30s for the same).
        let poll = tauri::async_runtime::spawn({
            let app = app.handle().clone();
            async move { poll_and_fire(&app).await }
        });
        tokio::time::timeout(Duration::from_secs(45), poll)
            .await
            .expect("poll_and_fire should finish within 45s")
            .expect("poll_and_fire task should not panic");

        let captured = events.lock().unwrap().clone();
        let session_id = captured
            .iter()
            .find_map(|e| e["session_id"].as_str())
            .expect("expected at least one session-status event from the ephemeral run")
            .to_string();
        assert!(
            captured.iter().any(|e| e["session_id"] == session_id && e["event"]["type"] == "done"),
            "expected a done event for the ephemeral run's session, got {captured:?}"
        );

        assert!(
            crate::workspace::session_process_snapshot(&session_id).await.is_none(),
            "session {session_id}'s workspace processes should have been torn down once the ephemeral automation run completed"
        );

        let automations = list_automations().await.expect("list_automations should succeed");
        let updated = automations.iter().find(|a| a.id == id).expect("automation should still exist in the store");
        assert_eq!(updated.last_run_status.as_deref(), Some("done"), "expected the run to be recorded as done");
        assert!(updated.last_run_at.is_some(), "expected last_run_at to be populated after firing");

        remove_for_test(&id).await;
    }
}
