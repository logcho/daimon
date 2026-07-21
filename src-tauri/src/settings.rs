use crate::workspace;

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
