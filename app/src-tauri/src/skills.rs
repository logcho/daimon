//! The skill library: reusable procedures as `<name>/SKILL.md` files, in two
//! places — the vault's `skills/` (which follows the user between projects)
//! and the current workspace's `.daimon/skills/` (which travels with the repo
//! and can be committed). A project skill shadows a vault skill of the same
//! name, mirroring `agents/src/daimon_agent/skills/injector.py`.
//!
//! Like `vault.rs`, browsing never touches the agent server — these are files
//! on the host, so the Tauri backend reads them directly for the UI without a
//! round-trip through the Python process. Read-only by design: skills are
//! written by the agent (`save_skill`) and edited in a real editor.

use std::path::{Path, PathBuf};

use crate::vault;

const SKILL_FILE: &str = "SKILL.md";

/// The user's global library.
fn vault_skills_dir() -> PathBuf {
    vault::vault_path().join("skills")
}

/// The current project's library. Mirrors `Settings.project_skills_dir`:
/// `DAIMON_WORKSPACE_DIR` if set, else the vault (the agent's own default when
/// no workspace is configured).
fn project_skills_dir() -> PathBuf {
    let workspace = std::env::var("DAIMON_WORKSPACE_DIR")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(vault::vault_path);
    workspace.join(".daimon").join("skills")
}

/// A skill directory name reduced to a single path component — no separators,
/// no `..` — before it is ever joined onto a library root. Same discipline as
/// `vault::sanitize_filename`; a name arriving from the webview is untrusted.
fn sanitize_name(name: &str) -> Result<String, String> {
    let candidate = Path::new(name)
        .file_name()
        .ok_or_else(|| "invalid skill name".to_string())?
        .to_string_lossy()
        .to_string();

    if candidate.is_empty() || candidate == "." || candidate == ".." {
        return Err("invalid skill name".to_string());
    }
    Ok(candidate)
}

/// Pulls `name` and `description` out of a `---`-delimited frontmatter block.
/// A file that doesn't parse still lists — it just shows no description, the
/// same degradation the Python injector applies.
fn parse_frontmatter(text: &str) -> (Option<String>, Option<String>) {
    let Some(rest) = text.strip_prefix("---") else {
        return (None, None);
    };
    let Some(end) = rest.find("\n---") else {
        return (None, None);
    };
    let mut name = None;
    let mut description = None;
    for line in rest[..end].lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        match key.trim() {
            "name" => name = Some(value.trim().to_string()),
            "description" => description = Some(value.trim().to_string()),
            _ => {}
        }
    }
    (name, description)
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SkillFile {
    name: String,
    description: String,
    /// "vault" or "project".
    source: String,
}

fn read_library(dir: &Path, source: &str, out: &mut Vec<SkillFile>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return; // a missing library is an empty one, not an error
    };
    for entry in entries.flatten() {
        let path = entry.path().join(SKILL_FILE);
        if !path.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap_or_default();
        let (meta_name, description) = parse_frontmatter(&text);
        let dir_name = entry.file_name().to_string_lossy().to_string();
        out.push(SkillFile {
            name: meta_name.filter(|n| !n.is_empty()).unwrap_or(dir_name),
            description: description.unwrap_or_default(),
            source: source.to_string(),
        });
    }
}

#[tauri::command]
pub async fn list_skills() -> Result<Vec<SkillFile>, String> {
    let mut skills = Vec::new();
    read_library(&vault_skills_dir(), "vault", &mut skills);

    // Project skills are collected second and shadow same-named vault ones,
    // matching the agent's precedence — the UI must show what the agent uses.
    let mut project = Vec::new();
    read_library(&project_skills_dir(), "project", &mut project);
    for skill in project {
        skills.retain(|s| s.name != skill.name);
        skills.push(skill);
    }

    skills.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(skills)
}

#[tauri::command]
pub async fn read_skill(name: String) -> Result<String, String> {
    let safe = sanitize_name(&name)?;
    // Project first, matching list_skills' precedence.
    for root in [project_skills_dir(), vault_skills_dir()] {
        let path = root.join(&safe).join(SKILL_FILE);
        if path.is_file() {
            return std::fs::read_to_string(&path)
                .map_err(|e| format!("failed to read \"{safe}\": {e}"));
        }
    }
    Err(format!("no skill named \"{safe}\""))
}

#[cfg(test)]
mod tests {
    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, mock_context, noop_assets, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::WebviewWindowBuilder;

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

    #[test]
    fn sanitize_name_strips_traversal() {
        assert_eq!(
            super::sanitize_name("../../etc/passwd").expect("should reduce to a bare name"),
            "passwd"
        );
        assert!(super::sanitize_name("..").is_err());
    }

    #[test]
    fn parse_frontmatter_reads_name_and_description() {
        let (name, description) =
            super::parse_frontmatter("---\nname: deploy\ndescription: Ship it\n---\n\nbody\n");
        assert_eq!(name.as_deref(), Some("deploy"));
        assert_eq!(description.as_deref(), Some("Ship it"));
    }

    #[test]
    fn parse_frontmatter_degrades_on_malformed_input() {
        let (name, description) = super::parse_frontmatter("no frontmatter at all");
        assert!(name.is_none() && description.is_none());
    }

    /// Same ACL-reachability pattern as vault.rs — a missing
    /// `"allow-<command>"` capability entry compiles fine and only fails at
    /// runtime, which has bitten this app twice before.
    #[test]
    fn skills_commands_clear_the_acl() {
        let app = mock_builder()
            .invoke_handler(tauri::generate_handler![
                super::list_skills,
                super::read_skill,
            ])
            .build(mock_context(noop_assets()))
            .expect("error while building test app");
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let list = get_ipc_response(&webview, invoke_request("list_skills", serde_json::json!({})))
            .expect("list_skills should be allowed by the capability");
        let _: Vec<super::SkillFile> = list.deserialize().expect("expected a Vec<SkillFile>");

        // A nonexistent skill errors from the lookup itself — the point is
        // that the command is *reachable* at all.
        let read = get_ipc_response(
            &webview,
            invoke_request("read_skill", serde_json::json!({ "name": "does-not-exist" })),
        );
        assert!(read.is_err(), "reading a nonexistent skill should error");
        let message = read.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "read_skill should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }
}
