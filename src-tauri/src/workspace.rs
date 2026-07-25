use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use tokio::process::Command;
use tokio::sync::{Mutex, OnceCell};

const IMAGE_TAG: &str = "daimon-workspace:latest";

static IMAGE_READY: OnceCell<Result<(), String>> = OnceCell::const_new();

/// A session's container stays up for the session's whole lifetime (Phase 8:
/// follow-up messages continue the same browser/page state rather than
/// starting fresh) — this is the cache that makes a session's second and
/// later messages skip container creation entirely and reuse the
/// already-running container's known port. Entries are removed by
/// `end_session_workspace`.
static SESSION_PORTS: LazyLock<Mutex<HashMap<String, u16>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

pub(crate) fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent directory")
        .to_path_buf()
}

/// Every active session gets its own container (`daimon-workspace-<session-id>`)
/// on its own host-assigned port, so concurrent sessions can't collide on the
/// same Playwright browser page. Containers are pure compute — the only
/// shared, durable state is the bind-mounted `memory/` directory (SQLite WAL
/// mode tolerates concurrent readers plus one writer fine at our write
/// frequency) — so a session's container is created once and reused for
/// every message in that session until it's explicitly ended (or the app
/// quits), rather than recreated per message.
fn container_name(session_id: &str) -> String {
    format!("daimon-workspace-{session_id}")
}

/// Returns the host port for `session_id`'s workspace container, creating it
/// if this is the session's first message. If the container already exists
/// (a follow-up message in an ongoing session), this is a plain cache lookup
/// with no Docker calls at all — the container is already up and its browser
/// state is left exactly as the previous turn left it. Otherwise it builds
/// the shared image on first use (once per process, via `IMAGE_READY`),
/// starts a fresh container, waits for it to become healthy, and caches the
/// resulting port before returning.
pub async fn ensure_session_workspace(session_id: &str) -> Result<u16, String> {
    {
        let cache = SESSION_PORTS.lock().await;
        if let Some(port) = cache.get(session_id) {
            return Ok(*port);
        }
    }

    IMAGE_READY
        .get_or_init(|| async { build_image(&project_root()).await })
        .await
        .clone()?;

    run_container(session_id).await?;
    let port = host_port(session_id).await?;
    wait_for_health(port).await?;

    SESSION_PORTS.lock().await.insert(session_id.to_string(), port);
    Ok(port)
}

/// Explicit, session-triggered teardown — the *only* thing that removes a
/// session's container now (Phase 8 reversed the old "tear down after every
/// task" policy): either the user ends the session, or the app quits and
/// sweeps every still-cached session (see `all_session_ids` and `lib.rs`'s
/// exit handler). Idempotent: removing an id that was never cached, or
/// tearing down a container that's already gone, is a harmless no-op.
pub async fn end_session_workspace(session_id: &str) {
    SESSION_PORTS.lock().await.remove(session_id);
    let _ = run_docker(&["rm", "-f", &container_name(session_id)]).await;
}

/// Every session id whose container is currently believed to be up — used
/// only for the best-effort app-exit sweep, so a quit doesn't leave orphaned
/// containers running indefinitely now that nothing tears them down
/// automatically per message.
pub async fn all_session_ids() -> Vec<String> {
    SESSION_PORTS.lock().await.keys().cloned().collect()
}

async fn run_docker(args: &[&str]) -> Result<std::process::Output, String> {
    Command::new("docker")
        .args(args)
        .current_dir(project_root())
        .output()
        .await
        .map_err(|e| format!("failed to run `docker {}`: {e}", args.join(" ")))
}

async fn build_image(project_root: &Path) -> Result<(), String> {
    let output = Command::new("docker")
        .args(["build", "-f", "sandbox/Dockerfile", "-t", IMAGE_TAG, "."])
        .current_dir(project_root)
        .output()
        .await
        .map_err(|e| format!("failed to run docker build: {e}"))?;

    if !output.status.success() {
        return Err(format!(
            "docker build failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(())
}

async fn run_container(session_id: &str) -> Result<(), String> {
    let name = container_name(session_id);

    // A stale container from a previous run that crashed before teardown
    // (or was left over from an app restart mid-task) would otherwise
    // collide on this name — clear it before creating the real one.
    let _ = run_docker(&["rm", "-f", &name]).await;

    let memory_dir = project_root().join("memory");
    std::fs::create_dir_all(&memory_dir).map_err(|e| format!("failed to create memory directory: {e}"))?;
    let memory_mount = format!("{}:/workspace/memory", memory_dir.display());

    // Same shape as `memory_mount` above: the vault (a real Obsidian vault if
    // the user configured one, otherwise Daimon's own default `vault/`
    // folder — see `vault::vault_path`) is bind-mounted so the agent's
    // note tools (`agents/src/vault.ts`) read/write the exact same host
    // directory the daemon browses directly for the UI.
    let vault_dir = crate::vault::vault_path();
    let vault_mount = format!("{}:/workspace/vault", vault_dir.display());

    // Third bind mount, same shape as `memory_mount`/`vault_mount` above: the
    // container has no reverse channel to call back into the daemon (see
    // `automation.rs`'s module doc), so the agent's `create_automation` tool
    // writes a pending-request file into this directory instead, for the
    // daemon's scheduler loop to pick up, validate, and promote.
    let automations_dir = crate::automation::automations_dir();
    let automations_mount = format!("{}:/workspace/automations", automations_dir.display());

    // Fourth bind mount, same shape again: Playwright's built-in video
    // recording (see `browser.ts`) writes finished .webm files inside the
    // container at `/workspace/recordings` — bind-mounting it out here is
    // what lets that video actually survive `docker rm -f` and be watchable
    // on the host afterward, rather than being written to a filesystem layer
    // that disappears the moment the container is removed.
    let recordings_dir = project_root().join("recordings");
    std::fs::create_dir_all(&recordings_dir)
        .map_err(|e| format!("failed to create recordings directory: {e}"))?;
    let recordings_mount = format!("{}:/workspace/recordings", recordings_dir.display());

    // Host port left unspecified (`::4711`) so Docker assigns a free one —
    // this is what lets N task containers coexist without a colliding fixed
    // binding.
    let mut args = vec![
        "run", "-d", "--name", &name, "-p", "127.0.0.1::4711", "-v", &memory_mount, "-v", &vault_mount, "-v",
        &automations_mount, "-v", &recordings_mount,
    ];

    let api_key_env;
    if let Ok(key) = std::env::var("ANTHROPIC_API_KEY") {
        api_key_env = format!("ANTHROPIC_API_KEY={key}");
        args.push("-e");
        args.push(&api_key_env);
    }
    args.push(IMAGE_TAG);

    let output = run_docker(&args).await?;
    if !output.status.success() {
        return Err(format!("docker run failed: {}", String::from_utf8_lossy(&output.stderr)));
    }
    Ok(())
}

/// Reads back the host port Docker assigned to this container's `4711/tcp`.
/// The mapping is set synchronously by `docker run -d`, but a handful of
/// retries absorbs any inspect-right-after-create raciness rather than
/// assuming it's always instant.
async fn host_port(session_id: &str) -> Result<u16, String> {
    let name = container_name(session_id);
    let format = "{{(index (index .NetworkSettings.Ports \"4711/tcp\") 0).HostPort}}";

    for attempt in 0..10 {
        let output = run_docker(&["inspect", "-f", format, &name]).await?;
        if output.status.success() {
            let port = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if let Ok(port) = port.parse::<u16>() {
                return Ok(port);
            }
        }
        if attempt < 9 {
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
    }

    Err(format!("could not determine host port for container {name}"))
}

pub fn has_api_key() -> bool {
    std::env::var("ANTHROPIC_API_KEY")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

/// Persists the key to `.env` and updates it in this process's live
/// environment so the next *new* session picks it up without an app
/// restart. Note this doesn't reach any session whose container is already
/// running — since Phase 8, a session's container lives for the session's
/// whole lifetime, so an already-open session keeps whatever key it was
/// started with until it's ended and a fresh one is started.
pub async fn set_api_key(key: &str) -> Result<(), String> {
    let key = key.trim();
    if key.is_empty() {
        return Err("API key cannot be empty".into());
    }
    set_env_var("ANTHROPIC_API_KEY", key)
}

/// Upserts `KEY=value` into the project-root `.env` file and mirrors it into
/// this process's live environment, so whichever caller needs it next (a
/// freshly started task container, an OAuth helper reading a client ID)
/// picks it up without an app restart. Shared by every `.env`-backed setting
/// — `ANTHROPIC_API_KEY`, `GOOGLE_OAUTH_CLIENT_ID`, and any future one —
/// rather than forking the same read-modify-write logic per key.
pub(crate) fn set_env_var(key: &str, value: &str) -> Result<(), String> {
    let env_path = project_root().join(".env");
    let mut lines: Vec<String> = if env_path.exists() {
        std::fs::read_to_string(&env_path)
            .map_err(|e| format!("failed to read .env: {e}"))?
            .lines()
            .map(|l| l.to_string())
            .collect()
    } else {
        Vec::new()
    };

    let prefix = format!("{key}=");
    let mut found = false;
    for line in lines.iter_mut() {
        if line.starts_with(&prefix) {
            *line = format!("{key}={value}");
            found = true;
            break;
        }
    }
    if !found {
        lines.push(format!("{key}={value}"));
    }

    std::fs::write(&env_path, lines.join("\n") + "\n").map_err(|e| format!("failed to write .env: {e}"))?;

    // SAFETY: this only races with another thread concurrently *reading*
    // this env var (run_container does, for ANTHROPIC_API_KEY, when starting
    // a task) — a rare, user-initiated, single-value update, not a pattern
    // that's realistically hit concurrently in this app's usage.
    unsafe {
        std::env::set_var(key, value);
    }

    Ok(())
}

async fn wait_for_health(port: u16) -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = format!("http://127.0.0.1:{port}/health");

    for _ in 0..60 {
        if let Ok(resp) = client.get(&url).send().await {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }

    Err("background workspace did not become healthy in time".into())
}
