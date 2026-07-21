use std::path::{Path, PathBuf};
use tokio::process::Command;
use tokio::sync::OnceCell;

const IMAGE_TAG: &str = "daimon-workspace:latest";
const CONTAINER_NAME: &str = "daimon-workspace-default";
pub const PORT: u16 = 4711;

static IMAGE_READY: OnceCell<Result<(), String>> = OnceCell::const_new();

pub(crate) fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent directory")
        .to_path_buf()
}

/// Ensures the background workspace (a Docker container running a headless
/// browser + shell) exists and is reachable, building the image on first use
/// and reusing/restarting the same named container on every call after that.
pub async fn ensure_workspace() -> Result<u16, String> {
    IMAGE_READY
        .get_or_init(|| async { build_image(&project_root()).await })
        .await
        .clone()?;

    ensure_container().await?;
    wait_for_health().await?;
    Ok(PORT)
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

/// `Some(true)` running, `Some(false)` stopped, `None` container doesn't exist.
async fn container_state() -> Result<Option<bool>, String> {
    let output = run_docker(&["inspect", "-f", "{{.State.Running}}", CONTAINER_NAME]).await?;
    if !output.status.success() {
        return Ok(None);
    }
    let running = String::from_utf8_lossy(&output.stdout).trim() == "true";
    Ok(Some(running))
}

async fn ensure_container() -> Result<(), String> {
    match container_state().await? {
        Some(true) => Ok(()),
        Some(false) => {
            let output = run_docker(&["start", CONTAINER_NAME]).await?;
            if !output.status.success() {
                return Err(format!(
                    "docker start failed: {}",
                    String::from_utf8_lossy(&output.stderr)
                ));
            }
            Ok(())
        }
        None => {
            let memory_dir = project_root().join("memory");
            std::fs::create_dir_all(&memory_dir)
                .map_err(|e| format!("failed to create memory directory: {e}"))?;
            let memory_mount = format!("{}:/workspace/memory", memory_dir.display());

            let port_mapping = format!("127.0.0.1:{PORT}:{PORT}");
            let mut args = vec![
                "run",
                "-d",
                "--name",
                CONTAINER_NAME,
                "-p",
                &port_mapping,
                "-v",
                &memory_mount,
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
                return Err(format!(
                    "docker run failed: {}",
                    String::from_utf8_lossy(&output.stderr)
                ));
            }
            Ok(())
        }
    }
}

pub fn has_api_key() -> bool {
    std::env::var("ANTHROPIC_API_KEY")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

/// Persists the key to `.env`, updates it in this process's live environment
/// so the very next task picks it up without an app restart, and drops any
/// existing workspace container so it gets recreated with the new key —
/// the key is only ever injected into a container at creation time.
pub async fn set_api_key(key: &str) -> Result<(), String> {
    let key = key.trim();
    if key.is_empty() {
        return Err("API key cannot be empty".into());
    }

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

    let mut found = false;
    for line in lines.iter_mut() {
        if line.starts_with("ANTHROPIC_API_KEY=") {
            *line = format!("ANTHROPIC_API_KEY={key}");
            found = true;
            break;
        }
    }
    if !found {
        lines.push(format!("ANTHROPIC_API_KEY={key}"));
    }

    std::fs::write(&env_path, lines.join("\n") + "\n")
        .map_err(|e| format!("failed to write .env: {e}"))?;

    // SAFETY: this only races with another thread concurrently *reading*
    // ANTHROPIC_API_KEY (ensure_container does, when starting a task) — a
    // rare, user-initiated, single-value update, not a pattern that's
    // realistically hit concurrently in this app's usage.
    unsafe {
        std::env::set_var("ANTHROPIC_API_KEY", key);
    }

    let _ = run_docker(&["rm", "-f", CONTAINER_NAME]).await;

    Ok(())
}

async fn wait_for_health() -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = format!("http://127.0.0.1:{PORT}/health");

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
