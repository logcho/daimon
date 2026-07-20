use std::path::{Path, PathBuf};
use tokio::process::Command;
use tokio::sync::OnceCell;

const IMAGE_TAG: &str = "daimon-workspace:latest";
const CONTAINER_NAME: &str = "daimon-workspace-default";
pub const PORT: u16 = 4711;

static IMAGE_READY: OnceCell<Result<(), String>> = OnceCell::const_new();

fn project_root() -> PathBuf {
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
            let port_mapping = format!("127.0.0.1:{PORT}:{PORT}");
            let mut args = vec!["run", "-d", "--name", CONTAINER_NAME, "-p", &port_mapping];

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
