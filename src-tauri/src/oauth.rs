//! Gmail OAuth2 (PKCE, loopback redirect) connect flow.
//!
//! Scoped to Gmail only for this first pass — Outlook (and any generic
//! multi-provider abstraction) is a deliberately separate, later pass. See
//! `ARCHITECTURE.md` §3F/§5 and `PROMPT.md`'s Phase 6 section for the design
//! this follows: consent happens in the user's real system browser (never an
//! embedded webview, which would be able to observe the Google password
//! field), and only the refresh token + minimal account metadata are
//! persisted, in the OS keychain — never plaintext on disk.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::Duration;
use tauri_plugin_opener::OpenerExt;

const KEYRING_SERVICE: &str = "com.daimon.app";
const KEYRING_REFRESH_TOKEN_ACCOUNT: &str = "gmail_refresh_token";
const KEYRING_META_ACCOUNT: &str = "gmail_account_meta";
const GMAIL_SCOPE: &str = "https://www.googleapis.com/auth/gmail.readonly";
const AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const PROFILE_URL: &str = "https://gmail.googleapis.com/gmail/v1/users/me/profile";
const LOOPBACK_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ConnectedAccount {
    pub email: String,
    pub connected_at: String,
}

// ---------------------------------------------------------------------
// Client ID configuration — same `.env` upsert pattern as
// `workspace::has_api_key`/`set_api_key`, just a different key.
// ---------------------------------------------------------------------

pub fn has_google_client_id() -> bool {
    std::env::var("GOOGLE_OAUTH_CLIENT_ID")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

async fn store_google_client_id(client_id: &str) -> Result<(), String> {
    let client_id = client_id.trim();
    if client_id.is_empty() {
        return Err("Google OAuth client ID cannot be empty".into());
    }
    crate::workspace::set_env_var("GOOGLE_OAUTH_CLIENT_ID", client_id)
}

fn configured_client_id() -> Result<String, String> {
    std::env::var("GOOGLE_OAUTH_CLIENT_ID")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| "connect a Google OAuth client ID first".to_string())
}

// ---------------------------------------------------------------------
// PKCE
// ---------------------------------------------------------------------

/// 32 random bytes, base64url-encoded (no padding) — a 43-character
/// verifier, comfortably within the 43-128 char range PKCE requires.
fn generate_code_verifier() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}

fn code_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    URL_SAFE_NO_PAD.encode(digest)
}

// ---------------------------------------------------------------------
// Loopback redirect listener
// ---------------------------------------------------------------------

/// Result of the one-shot loopback HTTP listener: either the `code` Google
/// redirected back with, or an error message (either an `error` query param
/// from Google, or a locally-detected problem parsing the request).
enum RedirectOutcome {
    Code(String),
    Error(String),
}

/// Accepts exactly one connection on `listener`, parses the `code`/`error`
/// query param off the request line, and writes back a minimal human-readable
/// HTML response so the browser tab doesn't hang. Runs on a blocking thread
/// since `std::net::TcpListener` has no async API of its own and this is a
/// single accept+read+write, not worth pulling in tokio's "net" feature for.
fn accept_redirect(listener: TcpListener) -> Result<RedirectOutcome, String> {
    let (mut stream, _) = listener.accept().map_err(|e| format!("failed to accept loopback connection: {e}"))?;

    let mut reader = BufReader::new(stream.try_clone().map_err(|e| format!("failed to clone loopback stream: {e}"))?);
    let mut request_line = String::new();
    reader
        .read_line(&mut request_line)
        .map_err(|e| format!("failed to read loopback request: {e}"))?;

    // Request line looks like: "GET /?code=...&scope=... HTTP/1.1"
    let path = request_line.split_whitespace().nth(1).unwrap_or("");
    let query = path.splitn(2, '?').nth(1).unwrap_or("");

    let mut code = None;
    let mut error = None;
    for pair in query.split('&') {
        let mut parts = pair.splitn(2, '=');
        let key = parts.next().unwrap_or("");
        let value = parts.next().unwrap_or("");
        let value = urlencoding_decode(value);
        match key {
            "code" => code = Some(value),
            "error" => error = Some(value),
            _ => {}
        }
    }

    let outcome = if let Some(err) = error {
        RedirectOutcome::Error(err)
    } else if let Some(code) = code {
        RedirectOutcome::Code(code)
    } else {
        RedirectOutcome::Error("no authorization code found in redirect".to_string())
    };

    let body = match &outcome {
        RedirectOutcome::Code(_) => "<html><body><h3>Connected</h3><p>You can close this tab.</p></body></html>",
        RedirectOutcome::Error(_) => {
            "<html><body><h3>Connection failed</h3><p>You can close this tab and try again.</p></body></html>"
        }
    };
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();

    Ok(outcome)
}

/// Minimal `application/x-www-form-urlencoded` percent-decoding — enough for
/// the query params Google's redirect actually sends (auth codes and error
/// codes are both plain ASCII token/`.`/`-`/`_`/`/` characters in practice,
/// but decode `%XX` and `+` properly anyway rather than assuming that).
fn urlencoding_decode(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars();
    while let Some(c) = chars.next() {
        match c {
            '+' => out.push(' '),
            '%' => {
                let hi = chars.next();
                let lo = chars.next();
                if let (Some(hi), Some(lo)) = (hi, lo) {
                    if let Ok(byte) = u8::from_str_radix(&format!("{hi}{lo}"), 16) {
                        out.push(byte as char);
                        continue;
                    }
                }
                out.push('%');
            }
            other => out.push(other),
        }
    }
    out
}

// ---------------------------------------------------------------------
// Token exchange + profile lookup
// ---------------------------------------------------------------------

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: Option<String>,
}

#[derive(Deserialize)]
struct ProfileResponse {
    #[serde(rename = "emailAddress")]
    email_address: String,
}

async fn exchange_code_for_tokens(
    client_id: &str,
    code: &str,
    code_verifier: &str,
    redirect_uri: &str,
) -> Result<TokenResponse, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(TOKEN_URL)
        .form(&[
            ("client_id", client_id),
            ("code", code),
            ("code_verifier", code_verifier),
            ("grant_type", "authorization_code"),
            ("redirect_uri", redirect_uri),
        ])
        .send()
        .await
        .map_err(|e| format!("failed to reach Google's token endpoint: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("token exchange failed ({status}): {text}"));
    }

    resp.json::<TokenResponse>()
        .await
        .map_err(|e| format!("failed to parse token response: {e}"))
}

async fn fetch_gmail_email(access_token: &str) -> Result<String, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(PROFILE_URL)
        .bearer_auth(access_token)
        .send()
        .await
        .map_err(|e| format!("failed to reach Gmail profile endpoint: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("failed to fetch Gmail profile ({status}): {text}"));
    }

    let profile = resp
        .json::<ProfileResponse>()
        .await
        .map_err(|e| format!("failed to parse Gmail profile response: {e}"))?;
    Ok(profile.email_address)
}

fn now_rfc3339() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs();

    // Minimal, dependency-free civil-from-days conversion (Howard Hinnant's
    // algorithm) — avoids pulling in `chrono` for a single timestamp string.
    let days_since_epoch = (secs / 86_400) as i64;
    let secs_of_day = secs % 86_400;
    let (hour, minute, second) = (secs_of_day / 3600, (secs_of_day % 3600) / 60, secs_of_day % 60);

    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };

    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

// ---------------------------------------------------------------------
// Keychain storage
// ---------------------------------------------------------------------

fn store_refresh_token(refresh_token: &str) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN_ACCOUNT)
        .map_err(|e| format!("failed to open keychain entry: {e}"))?;
    entry
        .set_password(refresh_token)
        .map_err(|e| format!("failed to store refresh token in keychain: {e}"))
}

fn store_account_meta(account: &ConnectedAccount) -> Result<(), String> {
    let json = serde_json::to_string(account).map_err(|e| format!("failed to serialize account metadata: {e}"))?;
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_META_ACCOUNT)
        .map_err(|e| format!("failed to open keychain entry: {e}"))?;
    entry
        .set_password(&json)
        .map_err(|e| format!("failed to store account metadata in keychain: {e}"))
}

fn read_gmail_account() -> Result<Option<ConnectedAccount>, String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_META_ACCOUNT)
        .map_err(|e| format!("failed to open keychain entry: {e}"))?;
    match entry.get_password() {
        Ok(json) => {
            let account: ConnectedAccount =
                serde_json::from_str(&json).map_err(|e| format!("failed to parse stored account metadata: {e}"))?;
            Ok(Some(account))
        }
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("failed to read account metadata from keychain: {e}")),
    }
}

fn clear_gmail_account() -> Result<(), String> {
    for account in [KEYRING_REFRESH_TOKEN_ACCOUNT, KEYRING_META_ACCOUNT] {
        let entry =
            keyring::Entry::new(KEYRING_SERVICE, account).map_err(|e| format!("failed to open keychain entry: {e}"))?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => {}
            Err(e) => return Err(format!("failed to remove {account} from keychain: {e}")),
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------
// The connect flow itself
// ---------------------------------------------------------------------

async fn run_gmail_connect_flow<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<ConnectedAccount, String> {
    let client_id = configured_client_id()?;

    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| format!("failed to bind loopback listener: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("failed to read loopback port: {e}"))?
        .port();
    let redirect_uri = format!("http://127.0.0.1:{port}");

    let code_verifier = generate_code_verifier();
    let challenge = code_challenge(&code_verifier);

    let auth_url = format!(
        "{AUTH_URL}?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}\
         &code_challenge={challenge}&code_challenge_method=S256&access_type=offline&prompt=consent",
        client_id = urlencoding_encode(&client_id),
        redirect_uri = urlencoding_encode(&redirect_uri),
        scope = urlencoding_encode(GMAIL_SCOPE),
        challenge = urlencoding_encode(&challenge),
    );

    // Opened in the user's real system browser, never an embedded webview —
    // the whole point of the loopback-redirect flow is that Daimon never
    // sees the Google login form or password.
    app.opener()
        .open_url(&auth_url, None::<&str>)
        .map_err(|e| format!("failed to open the sign-in page in your browser: {e}"))?;

    // Three layers to unwrap here: the timeout itself (`Elapsed`), the
    // blocking task join (`JoinError`, if the thread panicked), and finally
    // `accept_redirect`'s own `Result`.
    let join_result: Result<Result<RedirectOutcome, String>, tokio::task::JoinError> =
        tokio::time::timeout(LOOPBACK_TIMEOUT, tokio::task::spawn_blocking(move || accept_redirect(listener)))
            .await
            .map_err(|_| "timed out waiting for Google sign-in".to_string())?;
    let outcome = join_result.map_err(|e| format!("loopback listener task panicked: {e}"))??;

    let code = match outcome {
        RedirectOutcome::Code(code) => code,
        RedirectOutcome::Error(err) => return Err(format!("Google sign-in did not complete: {err}")),
    };

    let tokens = exchange_code_for_tokens(&client_id, &code, &code_verifier, &redirect_uri).await?;
    let email = fetch_gmail_email(&tokens.access_token).await?;

    let refresh_token = tokens
        .refresh_token
        .ok_or_else(|| "Google did not return a refresh token — try disconnecting and reconnecting".to_string())?;
    store_refresh_token(&refresh_token)?;

    let account = ConnectedAccount {
        email,
        connected_at: now_rfc3339(),
    };
    store_account_meta(&account)?;

    Ok(account)
}

/// Minimal percent-encoding for query param values — covers the characters
/// that actually show up in a client ID, redirect URI, scope string, or
/// base64url PKCE challenge (letters, digits, and `-_.~:/` need no encoding;
/// everything else used here is one of `: / . -` which are all in the
/// unreserved-or-safe set for a query string except `:` and `/` in the
/// redirect URI, which we do encode).
fn urlencoding_encode(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for byte in input.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

// ---------------------------------------------------------------------
// Tauri commands — thin wrappers, mirroring settings.rs's style.
// ---------------------------------------------------------------------

#[tauri::command]
pub async fn get_google_client_id_status() -> Result<bool, String> {
    Ok(has_google_client_id())
}

#[tauri::command]
pub async fn set_google_client_id(client_id: String) -> Result<(), String> {
    store_google_client_id(&client_id).await
}

#[tauri::command]
pub async fn connect_gmail_account<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<ConnectedAccount, String> {
    run_gmail_connect_flow(app).await
}

#[tauri::command]
pub async fn get_gmail_account() -> Result<Option<ConnectedAccount>, String> {
    read_gmail_account()
}

#[tauri::command]
pub async fn disconnect_gmail_account() -> Result<(), String> {
    clear_gmail_account()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::ipc::CallbackFn;
    use tauri::test::{get_ipc_response, mock_builder, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::WebviewWindowBuilder;

    #[test]
    fn code_challenge_matches_known_vector() {
        // RFC 7636 Appendix B test vector.
        let verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
        let expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
        assert_eq!(code_challenge(verifier), expected_challenge);
    }

    #[test]
    fn generated_verifier_is_pkce_compliant_length() {
        let verifier = generate_code_verifier();
        assert!(verifier.len() >= 43 && verifier.len() <= 128);
    }

    #[test]
    fn urlencoding_roundtrips_reserved_characters() {
        let encoded = urlencoding_encode("http://127.0.0.1:5555");
        assert_eq!(urlencoding_decode(&encoded), "http://127.0.0.1:5555");
    }

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

    /// Same ACL-reachability check as `settings_commands_clear_the_acl` in
    /// `settings.rs` (see the comment there for why this matters — a missing
    /// `"allow-<command-name>"` capability entry compiles fine and only fails
    /// at runtime). Doesn't attempt a real Google OAuth round-trip — that's
    /// explicitly out of scope for a unit test — just proves these commands
    /// are wired through to the mock webview at all.
    #[test]
    fn oauth_commands_clear_the_acl() {
        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let status = get_ipc_response(
            &webview,
            invoke_request("get_google_client_id_status", serde_json::json!({})),
        )
        .expect("get_google_client_id_status should be allowed by the capability");
        let _: bool = status.deserialize().expect("expected a bool");

        let account = get_ipc_response(&webview, invoke_request("get_gmail_account", serde_json::json!({})))
            .expect("get_gmail_account should be allowed by the capability");
        let _: Option<ConnectedAccount> = account.deserialize().expect("expected Option<ConnectedAccount>");

        // set_google_client_id with an empty string is expected to be
        // rejected by its own validation — the point is that it's
        // *reachable* at all (an Err from application logic, not an ACL
        // "not allowed" Err).
        let set_result = get_ipc_response(
            &webview,
            invoke_request("set_google_client_id", serde_json::json!({ "clientId": "" })),
        );
        assert!(set_result.is_err(), "empty client id should be rejected");
        let message = set_result.unwrap_err();
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "set_google_client_id should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );

        // disconnect_gmail_account is idempotent even with nothing
        // connected, so it should succeed outright rather than just avoid
        // an ACL error.
        let disconnect_result = get_ipc_response(
            &webview,
            invoke_request("disconnect_gmail_account", serde_json::json!({})),
        )
        .expect("disconnect_gmail_account should be allowed by the capability");
        let _: () = disconnect_result.deserialize().expect("expected unit");
    }
}
