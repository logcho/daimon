//! Spotify OAuth2 (PKCE, loopback redirect) connect flow.
//!
//! Structurally a direct mirror of `oauth.rs`'s Gmail flow — same shape,
//! deliberately not sharing code with it, matching that module's own doc
//! comment ("Outlook ... is a deliberately separate, later pass"): consent
//! happens in the user's real system browser (never an embedded webview,
//! which would be able to observe the Spotify password field), and only the
//! refresh token + minimal account metadata are persisted, in the OS
//! keychain — never plaintext on disk.
//!
//! Exists so the agent can actually search for and play a specific track on
//! the user's real desktop Spotify app (`agents/src/spotify.ts`'s
//! `play_music_by_name` tool) — `music_control`'s AppleScript can only do
//! transport control (play/pause/next/previous) and can't search by name;
//! Spotify's own Web API is the only way to do that *and* target the real
//! desktop app (via Spotify Connect's device-targeted playback) rather than
//! the web player.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rand::RngCore;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::Duration;
use tauri_plugin_opener::OpenerExt;

use crate::oauth::ConnectedAccount;

const KEYRING_SERVICE: &str = "com.daimon.app";
const KEYRING_REFRESH_TOKEN_ACCOUNT: &str = "spotify_refresh_token";
const KEYRING_META_ACCOUNT: &str = "spotify_account_meta";
// `user-read-email` is only needed so `ConnectedAccount.email` (a field
// shared with the Gmail flow's own struct) has something real to show —
// the actual playback capability only needs the other two.
const SPOTIFY_SCOPE: &str = "user-read-playback-state user-modify-playback-state user-read-email";
const AUTH_URL: &str = "https://accounts.spotify.com/authorize";
const TOKEN_URL: &str = "https://accounts.spotify.com/api/token";
const PROFILE_URL: &str = "https://api.spotify.com/v1/me";
const LOOPBACK_TIMEOUT: Duration = Duration::from_secs(120);
/// Unlike Google's OAuth (which allows any `127.0.0.1:<port>` for a Desktop
/// app client with no pre-registration — confirmed the ephemeral-port
/// approach `oauth.rs` uses for Gmail relies on exactly that), Spotify
/// requires an exact, fixed redirect URI registered in the app dashboard
/// ahead of time ("must exactly match ... including ... terminating
/// slashes," per Spotify's own PKCE flow docs) — no dynamic-port support.
/// So this binds a fixed port rather than `127.0.0.1:0`; the user registers
/// this exact URI once (Settings shows it) when creating their Spotify app.
const REDIRECT_URI: &str = "http://127.0.0.1:38214/callback";

// ---------------------------------------------------------------------
// Client ID configuration — same `.env` upsert pattern as
// `oauth.rs`'s Google client ID handling, just a different key.
// ---------------------------------------------------------------------

pub fn has_spotify_client_id() -> bool {
    std::env::var("SPOTIFY_CLIENT_ID")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

async fn store_spotify_client_id(client_id: &str) -> Result<(), String> {
    let client_id = client_id.trim();
    if client_id.is_empty() {
        return Err("Spotify client ID cannot be empty".into());
    }
    crate::workspace::set_env_var("SPOTIFY_CLIENT_ID", client_id)
}

fn configured_client_id() -> Result<String, String> {
    std::env::var("SPOTIFY_CLIENT_ID")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| "connect a Spotify client ID first".to_string())
}

// ---------------------------------------------------------------------
// PKCE — identical approach to oauth.rs, not shared code (see module doc).
// ---------------------------------------------------------------------

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
// Loopback redirect listener — same shape as oauth.rs's `accept_redirect`.
// ---------------------------------------------------------------------

enum RedirectOutcome {
    Code(String),
    Error(String),
}

fn accept_redirect(listener: TcpListener) -> Result<RedirectOutcome, String> {
    let (mut stream, _) = listener.accept().map_err(|e| format!("failed to accept loopback connection: {e}"))?;

    let mut reader = BufReader::new(stream.try_clone().map_err(|e| format!("failed to clone loopback stream: {e}"))?);
    let mut request_line = String::new();
    reader
        .read_line(&mut request_line)
        .map_err(|e| format!("failed to read loopback request: {e}"))?;

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
// Token exchange + profile lookup
// ---------------------------------------------------------------------

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: Option<String>,
}

#[derive(Deserialize)]
struct ProfileResponse {
    display_name: Option<String>,
    email: Option<String>,
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
        .map_err(|e| format!("failed to reach Spotify's token endpoint: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("token exchange failed ({status}): {text}"));
    }

    resp.json::<TokenResponse>()
        .await
        .map_err(|e| format!("failed to parse token response: {e}"))
}

/// Exchanges the stored refresh token for a fresh access token — called at
/// every session spawn (`workspace.rs`'s `spawn_node_agent`) so the Node
/// agent process gets a short-lived, always-current `SPOTIFY_ACCESS_TOKEN`
/// rather than the daemon ever handing it the long-lived refresh token
/// itself. Returns `Ok(None)` (not an error) if no account is connected —
/// callers should just skip setting the env var in that case.
pub async fn fresh_access_token() -> Result<Option<String>, String> {
    let Some(refresh_token) = read_refresh_token()? else {
        return Ok(None);
    };
    let client_id = configured_client_id()?;

    let client = reqwest::Client::new();
    let resp = client
        .post(TOKEN_URL)
        .form(&[
            ("client_id", client_id.as_str()),
            ("refresh_token", refresh_token.as_str()),
            ("grant_type", "refresh_token"),
        ])
        .send()
        .await
        .map_err(|e| format!("failed to reach Spotify's token endpoint: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("failed to refresh Spotify token ({status}): {text}"));
    }

    let token = resp
        .json::<TokenResponse>()
        .await
        .map_err(|e| format!("failed to parse refresh response: {e}"))?;
    // Spotify sometimes rotates the refresh token on refresh — persist the
    // new one if given one, otherwise keep using the existing one.
    if let Some(new_refresh_token) = token.refresh_token {
        store_refresh_token(&new_refresh_token)?;
    }
    Ok(Some(token.access_token))
}

async fn fetch_spotify_profile(access_token: &str) -> Result<(String, Option<String>), String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(PROFILE_URL)
        .bearer_auth(access_token)
        .send()
        .await
        .map_err(|e| format!("failed to reach Spotify profile endpoint: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("failed to fetch Spotify profile ({status}): {text}"));
    }

    let profile = resp
        .json::<ProfileResponse>()
        .await
        .map_err(|e| format!("failed to parse Spotify profile response: {e}"))?;
    let label = profile.email.clone().or(profile.display_name).unwrap_or_else(|| "connected".to_string());
    Ok((label, profile.email))
}

fn now_rfc3339() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs();

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

fn read_refresh_token() -> Result<Option<String>, String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN_ACCOUNT)
        .map_err(|e| format!("failed to open keychain entry: {e}"))?;
    match entry.get_password() {
        Ok(token) => Ok(Some(token)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("failed to read refresh token from keychain: {e}")),
    }
}

fn store_account_meta(account: &ConnectedAccount) -> Result<(), String> {
    let json = serde_json::to_string(account).map_err(|e| format!("failed to serialize account metadata: {e}"))?;
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_META_ACCOUNT)
        .map_err(|e| format!("failed to open keychain entry: {e}"))?;
    entry
        .set_password(&json)
        .map_err(|e| format!("failed to store account metadata in keychain: {e}"))
}

fn read_spotify_account() -> Result<Option<ConnectedAccount>, String> {
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

fn clear_spotify_account() -> Result<(), String> {
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

async fn run_spotify_connect_flow<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<ConnectedAccount, String> {
    let client_id = configured_client_id()?;

    // Fixed port/URI, not `127.0.0.1:0` — see `REDIRECT_URI`'s doc comment;
    // Spotify (unlike Google) requires this to exactly match what's
    // pre-registered in the app dashboard.
    let listener = TcpListener::bind("127.0.0.1:38214").map_err(|e| {
        format!(
            "failed to bind the OAuth callback port (127.0.0.1:38214) — is another Daimon sign-in already \
             in progress, or something else using that port? ({e})"
        )
    })?;
    let redirect_uri = REDIRECT_URI;

    let code_verifier = generate_code_verifier();
    let challenge = code_challenge(&code_verifier);

    let auth_url = format!(
        "{AUTH_URL}?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scope}\
         &code_challenge={challenge}&code_challenge_method=S256",
        client_id = urlencoding_encode(&client_id),
        redirect_uri = urlencoding_encode(redirect_uri),
        scope = urlencoding_encode(SPOTIFY_SCOPE),
        challenge = urlencoding_encode(&challenge),
    );

    // Opened in the user's real system browser, never an embedded webview —
    // same reasoning as oauth.rs's Gmail flow: Daimon never sees the
    // Spotify login form or password.
    app.opener()
        .open_url(&auth_url, None::<&str>)
        .map_err(|e| format!("failed to open the sign-in page in your browser: {e}"))?;

    let join_result: Result<Result<RedirectOutcome, String>, tokio::task::JoinError> =
        tokio::time::timeout(LOOPBACK_TIMEOUT, tokio::task::spawn_blocking(move || accept_redirect(listener)))
            .await
            .map_err(|_| "timed out waiting for Spotify sign-in".to_string())?;
    let outcome = join_result.map_err(|e| format!("loopback listener task panicked: {e}"))??;

    let code = match outcome {
        RedirectOutcome::Code(code) => code,
        RedirectOutcome::Error(err) => return Err(format!("Spotify sign-in did not complete: {err}")),
    };

    let tokens = exchange_code_for_tokens(&client_id, &code, &code_verifier, redirect_uri).await?;
    let (label, email) = fetch_spotify_profile(&tokens.access_token).await?;
    let _ = label; // the display name isn't persisted separately — email (or "connected") is what's shown

    let refresh_token = tokens
        .refresh_token
        .ok_or_else(|| "Spotify did not return a refresh token — try disconnecting and reconnecting".to_string())?;
    store_refresh_token(&refresh_token)?;

    let account = ConnectedAccount {
        email: email.unwrap_or_else(|| "connected".to_string()),
        connected_at: now_rfc3339(),
    };
    store_account_meta(&account)?;

    Ok(account)
}

// ---------------------------------------------------------------------
// Tauri commands — thin wrappers, mirroring oauth.rs's style.
// ---------------------------------------------------------------------

#[tauri::command]
pub async fn get_spotify_client_id_status() -> Result<bool, String> {
    Ok(has_spotify_client_id())
}

#[tauri::command]
pub async fn set_spotify_client_id(client_id: String) -> Result<(), String> {
    store_spotify_client_id(&client_id).await
}

#[tauri::command]
pub async fn connect_spotify_account<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<ConnectedAccount, String> {
    run_spotify_connect_flow(app).await
}

#[tauri::command]
pub async fn get_spotify_account() -> Result<Option<ConnectedAccount>, String> {
    read_spotify_account()
}

#[tauri::command]
pub async fn disconnect_spotify_account() -> Result<(), String> {
    clear_spotify_account()
}
