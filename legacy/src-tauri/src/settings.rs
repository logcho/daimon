use crate::workspace;

/// How the agent process authenticates to Anthropic.
///
/// `Subscription` runs the Claude Code harness under the user's own
/// `claude login`, so turns bill against their Claude subscription and no API
/// key is involved at all. `ApiKey` uses the key stored in `.env`.
///
/// Stored in `.env` as `DAIMON_AUTH_MODE` alongside the key itself, and
/// forwarded to each session's agent process (see `workspace::spawn_node_agent`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthMode {
    Subscription,
    ApiKey,
}

impl AuthMode {
    pub fn as_str(self) -> &'static str {
        match self {
            AuthMode::Subscription => "subscription",
            AuthMode::ApiKey => "api_key",
        }
    }
}

/// Defaults to `ApiKey`, which is both the pre-existing behaviour and the
/// safe direction to fail: an unset or unrecognized value can only ever mean
/// "keep using the key you already have", never "silently start spending the
/// user's subscription".
pub fn agent_auth_mode() -> AuthMode {
    match std::env::var("DAIMON_AUTH_MODE").as_deref() {
        Ok("subscription") => AuthMode::Subscription,
        _ => AuthMode::ApiKey,
    }
}

#[tauri::command]
pub async fn get_agent_auth_mode() -> Result<String, String> {
    Ok(agent_auth_mode().as_str().to_string())
}

#[tauri::command]
pub async fn set_agent_auth_mode(mode: String) -> Result<(), String> {
    let mode = match mode.as_str() {
        "subscription" => AuthMode::Subscription,
        "api_key" => AuthMode::ApiKey,
        other => return Err(format!("unknown auth mode: {other}")),
    };
    // Subscription mode is only meaningful if the harness can actually find a
    // Claude Code login to use. Checked here rather than only in the UI so the
    // failure is a clear error at the moment of choosing, instead of every
    // subsequent turn dying inside the agent process.
    if mode == AuthMode::Subscription && !crate::terminal::claude_cli_available().await {
        return Err(
            "Claude Code isn't installed. Install it with `npm install -g @anthropic-ai/claude-code` \
             and run `claude login`, then try again."
                .into(),
        );
    }
    workspace::set_env_var("DAIMON_AUTH_MODE", mode.as_str())
}

#[tauri::command]
pub async fn get_api_key_status() -> Result<bool, String> {
    Ok(workspace::has_api_key())
}

#[tauri::command]
pub async fn set_api_key(key: String) -> Result<(), String> {
    workspace::set_api_key(&key).await
}

#[cfg(test)]
mod tests {
    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, INVOKE_KEY};
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

    /// Proves the ACL is actually wired for these commands (start_task hit a
    /// real "not allowed" bug here earlier this project — that's exactly
    /// what this test would have caught before it shipped).
    #[test]
    fn settings_commands_clear_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let status = get_ipc_response(&webview, invoke_request("get_api_key_status", serde_json::json!({})))
            .expect("get_api_key_status should be allowed by the capability");
        let _: bool = status.deserialize().expect("expected a bool");

        let mode = get_ipc_response(&webview, invoke_request("get_agent_auth_mode", serde_json::json!({})))
            .expect("get_agent_auth_mode should be allowed by the capability");
        let mode: String = mode.deserialize().expect("expected a string");
        assert!(
            mode == "subscription" || mode == "api_key",
            "unexpected auth mode: {mode}"
        );

        let bad_mode = get_ipc_response(
            &webview,
            invoke_request("set_agent_auth_mode", serde_json::json!({ "mode": "nonsense" })),
        );
        // Same shape as the set_api_key assertion below: an Err from the
        // command's own validation proves it's reachable, where an ACL
        // rejection would not.
        let message = bad_mode.expect_err("an unknown auth mode should be rejected");
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "set_agent_auth_mode should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );

        let set_result = get_ipc_response(
            &webview,
            invoke_request("set_api_key", serde_json::json!({ "key": "" })),
        );
        // Empty key is expected to be rejected by set_api_key's own
        // validation — the point here is that it's *reachable* at all
        // (an Err from application logic, not an ACL "not allowed" Err).
        assert!(set_result.is_err(), "empty key should be rejected");
        let message = set_result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "set_api_key should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
    }
}
