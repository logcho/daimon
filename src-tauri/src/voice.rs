//! Local, on-device dictation: a toggle-to-record hotkey command, batch
//! transcription via `whisper-rs` (whisper.cpp bindings), no cloud API/account
//! involved. See `ARCHITECTURE.md`'s Voice/Input row and `PROMPT.md`'s Phase 5
//! "Voice input" section for the product decisions this implements — in
//! particular: a *standard* global hotkey (simple toggle, unchanged), and an
//! explicit Settings download step for the model rather than an implicit
//! first-use download.
//!
//! This module owns three things:
//! - Model presence/download (`get_voice_model_status`/`download_voice_model`).
//! - Microphone capture via `cpal`, buffered raw at the device's native rate.
//! - Batch (not streaming) transcription via a lazily-loaded, cached
//!   `WhisperContext`, run off the async runtime via `spawn_blocking`.
//!
//! The recording state machine is exposed as two granular primitives —
//! `start_recording_internal`/`stop_recording_and_transcribe_internal` — plus
//! a status-only `emit_recording_locked`, rather than a single opaque
//! toggle. `toggle_dictation_internal` (the standard hotkey's simple
//! Idle/Recording/Transcribing cycle) is now just a thin composition of the
//! first two; `fn_key.rs`'s richer press/release gesture state machine
//! (hold-to-talk, double-tap-to-lock — see its module doc) calls all three
//! directly, since a bare toggle can't express "this press means start" vs.
//! "this press means stop" vs. "just relabel the already-running recording
//! as locked" independently of each other.
//!
//! Dictated text is handed back to the frontend purely as an event payload —
//! this module never simulates keystrokes or touches window focus, matching
//! `ARCHITECTURE.md` §5's non-disruption invariants.

use std::path::PathBuf;
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, LazyLock, Mutex as StdMutex, OnceLock};
use std::thread::JoinHandle;

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use futures_util::StreamExt;
use serde::Serialize;
use tauri::Emitter;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

use crate::workspace;

/// English-only "base" model: ~148MB, a good speed/accuracy balance for
/// short dictated instructions (not long-form transcription) — see
/// `PROMPT.md`'s Phase 5 section for why this specific model was chosen.
const MODEL_FILENAME: &str = "ggml-base.en.bin";
const MODEL_URL: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";

/// whisper.cpp's hard requirement — see `full()`'s docs in whisper-rs and
/// PROMPT.md's audio-capture guidance. Every buffer handed to `transcribe`
/// must already be resampled to this rate, mono, f32.
const WHISPER_SAMPLE_RATE: u32 = 16_000;

// Test-only override for `model_dir()` — see `HideModelFile` in the test
// module below for why this exists. A plain `Mutex`-guarded static, not a
// `thread_local!`: Tauri commands run on the async runtime, which very
// plausibly executes the command body on a different OS thread than the one
// that called `get_ipc_response` in the test — a `thread_local!` set on the
// test's own thread would then be invisible to `model_dir()` when it's
// actually invoked from inside the command, which is exactly what happened
// (this was tried first, and the test failed because of it). Safe as a
// process-wide static in practice because exactly one test in this suite
// (`voice_commands_clear_the_acl`) ever depends on `model_dir()`'s value —
// if a future test needs the same override concurrently, this assumption
// needs revisiting.
#[cfg(test)]
static TEST_MODEL_DIR_OVERRIDE: StdMutex<Option<PathBuf>> = StdMutex::new(None);

fn model_dir() -> PathBuf {
    #[cfg(test)]
    {
        if let Some(dir) = TEST_MODEL_DIR_OVERRIDE.lock().expect("test model dir override mutex poisoned").clone() {
            return dir;
        }
    }
    let dir = workspace::data_dir().join("whisper-models");
    migrate_legacy_model_once(&dir);
    dir
}

fn model_path() -> PathBuf {
    model_dir().join(MODEL_FILENAME)
}

/// Guards `migrate_legacy_model` so it only ever actually runs its
/// filesystem work once per process — `model_dir()` is called on essentially
/// every dictation-related operation (`get_voice_model_status`,
/// `start_recording_internal`, `whisper_context()`, …), and there's no
/// reason to re-stat both paths on every single one of those once this
/// process has already settled the question.
static MODEL_MIGRATION_DONE: OnceLock<()> = OnceLock::new();

fn migrate_legacy_model_once(new_dir: &std::path::Path) {
    MODEL_MIGRATION_DONE.get_or_init(|| {
        migrate_legacy_model(new_dir, &workspace::project_root().join("whisper-models"));
    });
}

/// One-time migration for the Docker-to-native-process directory change:
/// `model_dir()` used to resolve to `project_root().join("whisper-models")`
/// (a git-checkout-relative path), and now resolves to
/// `workspace::data_dir().join("whisper-models")` (the real, per-app OS data
/// directory — see `workspace.rs`'s module doc). Existing installs that
/// already downloaded the ~148MB model have it sitting at the *old* path,
/// which the new `model_dir()` never looks at — every dictation attempt was
/// silently hitting "model not downloaded" as a result, even though the file
/// was right there on disk.
///
/// A plain, best-effort move: `rename` first (fast, atomic, and the common
/// case — `project_root()` and the real app-data dir are virtually always on
/// the same volume on a real machine), falling back to copy+remove if
/// `rename` fails (e.g. `EXDEV`, a genuine cross-filesystem move) rather than
/// assuming `rename` always succeeds. If the old file is already gone (never
/// downloaded, or already migrated in a previous process's run), this is a
/// silent no-op — the normal "not downloaded yet, prompt the user" flow in
/// `start_recording_internal`/`get_voice_model_status` takes over exactly as
/// before, no new failure mode introduced. Also a no-op if a file already
/// exists at the new location (nothing to overwrite, and a fresh
/// `download_voice_model` run should never be clobbered by a stale old
/// copy).
///
/// Split out from `migrate_legacy_model_once` as a plain, pure-ish function
/// over two explicit paths (rather than reading `workspace::project_root()`
/// directly inline) so it's directly testable against disposable temp
/// directories — see the `migrate_legacy_model_*` tests below — without ever
/// touching this crate's own real dev-checkout `whisper-models/` directory.
fn migrate_legacy_model(new_dir: &std::path::Path, legacy_dir: &std::path::Path) {
    let new_path = new_dir.join(MODEL_FILENAME);
    if new_path.is_file() {
        return;
    }
    let legacy_path = legacy_dir.join(MODEL_FILENAME);
    if !legacy_path.is_file() {
        return;
    }

    if let Err(e) = std::fs::create_dir_all(new_dir) {
        log::error!(
            "voice: failed to create {} while migrating the whisper model from its legacy location: {e}",
            new_dir.display()
        );
        return;
    }

    match std::fs::rename(&legacy_path, &new_path) {
        Ok(()) => {
            log::info!(
                "voice: migrated whisper model from legacy path {} to {}",
                legacy_path.display(),
                new_path.display()
            );
        }
        Err(rename_err) => {
            log::warn!(
                "voice: rename failed migrating whisper model ({rename_err}) — falling back to copy+remove \
                 (likely a cross-device move between {} and {})",
                legacy_path.display(),
                new_path.display()
            );
            match std::fs::copy(&legacy_path, &new_path) {
                Ok(_) => match std::fs::remove_file(&legacy_path) {
                    Ok(()) => log::info!(
                        "voice: migrated whisper model via copy+remove from legacy path {} to {}",
                        legacy_path.display(),
                        new_path.display()
                    ),
                    Err(remove_err) => log::warn!(
                        "voice: copied whisper model to {} but failed to remove the old copy at {}: {remove_err} \
                         (harmless — the new copy is what's actually used from here on)",
                        new_path.display(),
                        legacy_path.display()
                    ),
                },
                Err(copy_err) => {
                    // Deliberately not deleting `legacy_path` in this branch —
                    // leaving the old file in place (however unreachable) is
                    // strictly safer than losing the only copy of a 148MB
                    // download the user would otherwise have to refetch.
                    log::error!(
                        "voice: failed to migrate whisper model from legacy path {} to {}: {copy_err} \
                         — leaving the old file in place; dictation will report \"not downloaded\" until this is resolved",
                        legacy_path.display(),
                        new_path.display()
                    );
                }
            }
        }
    }
}

// --------------------------------------------------------------------------
// Model status/download
// --------------------------------------------------------------------------

#[derive(Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VoiceModelStatus {
    downloaded: bool,
    model_name: String,
}

#[tauri::command]
pub async fn get_voice_model_status() -> Result<VoiceModelStatus, String> {
    Ok(VoiceModelStatus {
        downloaded: model_path().is_file(),
        model_name: MODEL_FILENAME.to_string(),
    })
}

/// Downloads the model file if it isn't already present — idempotent, so a
/// Settings "Download" button can be safely clicked more than once (e.g. the
/// user re-opens Settings later) without re-fetching 148MB. Streams the
/// response straight to disk (never buffers the whole model in memory) and
/// emits best-effort progress on the `voice-model-download` event channel —
/// additive to the required `dictation-status` contract, not a replacement
/// for it; a frontend that ignores it just sees a single completed `await`.
#[tauri::command]
pub async fn download_voice_model<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    let final_path = model_path();
    if final_path.is_file() {
        log::info!("voice: model already present at {}", final_path.display());
        return Ok(());
    }

    log::info!("voice: downloading model from {MODEL_URL}");
    std::fs::create_dir_all(model_dir()).map_err(|e| format!("failed to create whisper-models directory: {e}"))?;

    let response = reqwest::get(MODEL_URL)
        .await
        .map_err(|e| format!("failed to reach the model download host: {e}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "model download failed with HTTP status {}",
            response.status()
        ));
    }
    let total_bytes = response.content_length();

    // Downloaded to a `.part` sibling first, then renamed into place once
    // complete — so a crash or quit mid-download never leaves a truncated
    // file that `get_voice_model_status` would mistake for a real model
    // (whisper-rs would then fail confusingly deep inside whisper.cpp
    // instead of a clean "not downloaded yet" error).
    let tmp_path = final_path.with_extension("bin.part");
    let mut file = tokio::fs::File::create(&tmp_path)
        .await
        .map_err(|e| format!("failed to create model file: {e}"))?;

    let mut stream = response.bytes_stream();
    let mut downloaded_bytes: u64 = 0;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("model download was interrupted: {e}"))?;
        tokio::io::AsyncWriteExt::write_all(&mut file, &chunk)
            .await
            .map_err(|e| format!("failed to write model file: {e}"))?;
        downloaded_bytes += chunk.len() as u64;
        let _ = app.emit(
            "voice-model-download",
            serde_json::json!({ "downloadedBytes": downloaded_bytes, "totalBytes": total_bytes, "done": false }),
        );
    }
    drop(file);

    tokio::fs::rename(&tmp_path, &final_path)
        .await
        .map_err(|e| format!("failed to finalize model file: {e}"))?;
    log::info!("voice: model download complete ({downloaded_bytes} bytes) at {}", final_path.display());

    let _ = app.emit(
        "voice-model-download",
        serde_json::json!({ "downloadedBytes": downloaded_bytes, "totalBytes": total_bytes, "done": true }),
    );
    Ok(())
}

// --------------------------------------------------------------------------
// Recording state machine
// --------------------------------------------------------------------------

/// Everything needed to stop a running capture and recover its audio: the
/// shared buffer the mic thread is appending to, the rate it's capturing at
/// (native device rate — resampling to 16kHz happens once, after capture
/// stops, not per-callback), a channel to tell that thread to stop, and its
/// `JoinHandle` so we can be sure it (and therefore the `cpal::Stream` it
/// owns) has actually torn down before reading the buffer back out.
struct ActiveRecording {
    buffer: Arc<StdMutex<Vec<f32>>>,
    source_sample_rate: u32,
    stop_tx: std_mpsc::Sender<()>,
    join_handle: JoinHandle<()>,
}

enum DictationState {
    Idle,
    Recording(ActiveRecording),
    /// Set the moment a stop is requested, cleared once the background
    /// transcription task settles (success or error) — guards against a
    /// second hotkey press racing the still-in-flight whisper inference for
    /// the previous recording.
    Transcribing,
}

static DICTATION_STATE: LazyLock<StdMutex<DictationState>> = LazyLock::new(|| StdMutex::new(DictationState::Idle));

/// Loading a `WhisperContext` is comparatively expensive (parses/mmaps the
/// whole model), so it's cached here and reused across every dictation call
/// in this process rather than reloaded per-toggle. Deliberately *not*
/// touched until a recording is actually about to be transcribed (see
/// `spawn_recording_thread`'s model-presence check) — that way a "not
/// downloaded yet" state is never permanently cached here; only a
/// successfully-loaded context (or a genuine load failure with the file
/// present) ever gets stored.
static WHISPER_CONTEXT: OnceLock<Result<Arc<WhisperContext>, String>> = OnceLock::new();

fn whisper_context() -> Result<Arc<WhisperContext>, String> {
    WHISPER_CONTEXT
        .get_or_init(|| {
            let path = model_path();
            WhisperContext::new_with_params(&path, WhisperContextParameters::default())
                .map(Arc::new)
                .map_err(|e| format!("failed to load whisper model: {e}"))
        })
        .clone()
}

fn log_stream_error(err: cpal::Error) {
    log::error!("dictation: microphone stream error: {err}");
}

/// Appends one callback's worth of samples to the shared buffer, converting
/// to mono by averaging channels (whisper.cpp wants mono) but leaving the
/// sample *rate* alone — the whole buffer is resampled to 16kHz once, after
/// capture stops (see `resample_linear`), rather than per chunk.
fn append_mono_samples<T: Copy>(
    buffer: &Arc<StdMutex<Vec<f32>>>,
    data: &[T],
    channels: usize,
    to_f32: impl Fn(T) -> f32,
) {
    let mut buf = buffer.lock().expect("dictation buffer mutex poisoned");
    if channels <= 1 {
        buf.extend(data.iter().map(|&s| to_f32(s)));
    } else {
        buf.extend(data.chunks_exact(channels).map(|frame| {
            let sum: f32 = frame.iter().map(|&s| to_f32(s)).sum();
            sum / channels as f32
        }));
    }
}

/// Spins up a dedicated OS thread that owns the `cpal::Stream` for its
/// entire lifetime. This isn't incidental: `cpal::Stream` on at least the
/// CoreAudio backend isn't guaranteed movable/shareable the way a plain
/// value would be, and in general cpal expects a stream to live on the
/// thread that built it. Rather than lean on that being `Send` (fragile
/// across cpal versions/hosts), the mic device/config/stream are all
/// constructed *inside* this thread, and the only things that cross back out
/// are a shared, mutex-guarded sample buffer, a stop signal, and the
/// `JoinHandle` itself — all ordinary `Send` types.
fn spawn_recording_thread(
    buffer: Arc<StdMutex<Vec<f32>>>,
) -> (std_mpsc::Sender<()>, JoinHandle<()>, std_mpsc::Receiver<Result<u32, String>>) {
    let (ready_tx, ready_rx) = std_mpsc::channel::<Result<u32, String>>();
    let (stop_tx, stop_rx) = std_mpsc::channel::<()>();

    let join_handle = std::thread::spawn(move || {
        let outcome: Result<(cpal::Stream, u32), String> = (|| {
            let host = cpal::default_host();
            let device = host
                .default_input_device()
                .ok_or_else(|| "no input (microphone) device found".to_string())?;
            log::info!("voice: using input device {device}");
            let supported_config = device
                .default_input_config()
                .map_err(|e| format!("failed to read default microphone config: {e}"))?;

            let sample_format = supported_config.sample_format();
            let source_sample_rate = supported_config.sample_rate();
            let channels = supported_config.channels() as usize;
            let stream_config: cpal::StreamConfig = supported_config.config();
            log::info!(
                "voice: input config — format={sample_format:?}, sample_rate={source_sample_rate}, channels={channels}"
            );

            let cb_buffer = buffer.clone();
            let stream = match sample_format {
                cpal::SampleFormat::F32 => device.build_input_stream(
                    stream_config,
                    move |data: &[f32], _: &cpal::InputCallbackInfo| {
                        append_mono_samples(&cb_buffer, data, channels, |s| s)
                    },
                    log_stream_error,
                    None,
                ),
                cpal::SampleFormat::I16 => device.build_input_stream(
                    stream_config,
                    move |data: &[i16], _: &cpal::InputCallbackInfo| {
                        append_mono_samples(&cb_buffer, data, channels, |s| s as f32 / i16::MAX as f32)
                    },
                    log_stream_error,
                    None,
                ),
                cpal::SampleFormat::U16 => device.build_input_stream(
                    stream_config,
                    move |data: &[u16], _: &cpal::InputCallbackInfo| {
                        append_mono_samples(&cb_buffer, data, channels, |s| (s as f32 - 32768.0) / 32768.0)
                    },
                    log_stream_error,
                    None,
                ),
                other => return Err(format!("unsupported microphone sample format: {other:?}")),
            }
            .map_err(|e| format!("failed to open microphone stream: {e}"))?;

            stream
                .play()
                .map_err(|e| format!("failed to start microphone stream: {e}"))?;
            Ok((stream, source_sample_rate))
        })();

        match outcome {
            Ok((stream, source_sample_rate)) => {
                let _ = ready_tx.send(Ok(source_sample_rate));
                // Blocks this (dedicated) thread until `toggle_dictation`
                // sends a stop signal. The stream keeps recording into
                // `buffer` the whole time via its callback, which runs on
                // cpal's own audio thread, not this one.
                let _ = stop_rx.recv();
                drop(stream);
            }
            Err(e) => {
                let _ = ready_tx.send(Err(e));
            }
        }
    });

    (stop_tx, join_handle, ready_rx)
}

fn start_recording() -> Result<ActiveRecording, String> {
    let buffer: Arc<StdMutex<Vec<f32>>> = Arc::new(StdMutex::new(Vec::new()));
    let (stop_tx, join_handle, ready_rx) = spawn_recording_thread(buffer.clone());

    // Wait for the capture thread to confirm the stream actually started
    // before telling the caller recording began — surfaces a real mic error
    // (no device, permission denied, etc.) synchronously instead of
    // reporting "recording" while silently capturing nothing.
    match ready_rx.recv() {
        Ok(Ok(source_sample_rate)) => {
            log::info!("voice: recording started (source_sample_rate={source_sample_rate})");
            Ok(ActiveRecording {
                buffer,
                source_sample_rate,
                stop_tx,
                join_handle,
            })
        }
        Ok(Err(message)) => {
            log::error!("voice: failed to start recording: {message}");
            let _ = join_handle.join();
            Err(message)
        }
        Err(_) => {
            log::error!("voice: microphone capture thread ended unexpectedly before it could start");
            let _ = join_handle.join();
            Err("microphone capture thread ended unexpectedly before it could start".to_string())
        }
    }
}

/// A simple linear-interpolation resample — adequate for speech-to-text
/// preprocessing (this doesn't need broadcast-quality resampling, per
/// `PROMPT.md`'s guidance). Applied once, to the whole captured buffer,
/// rather than per audio callback.
fn resample_linear(input: &[f32], from_rate: u32, to_rate: u32) -> Vec<f32> {
    if input.is_empty() || from_rate == to_rate || from_rate == 0 {
        return input.to_vec();
    }

    let ratio = from_rate as f64 / to_rate as f64;
    let out_len = ((input.len() as f64) / ratio).round().max(0.0) as usize;
    let mut output = Vec::with_capacity(out_len);
    let last_index = input.len() - 1;

    for i in 0..out_len {
        let src_pos = i as f64 * ratio;
        let idx = src_pos.floor() as usize;
        let frac = (src_pos - idx as f64) as f32;
        let s0 = input[idx.min(last_index)];
        let s1 = input[(idx + 1).min(last_index)];
        output.push(s0 + (s1 - s0) * frac);
    }
    output
}

/// One whisper.cpp segment's text plus the model's own assessment of how
/// likely that segment is to be non-speech — whisper.cpp computes this per
/// segment specifically to flag "this probably isn't speech" (see
/// `whisper_full_get_segment_no_speech_prob_from_state`, exposed by
/// whisper-rs 0.16 as `WhisperSegment::no_speech_probability`). Carried
/// alongside the text (rather than `transcribe` just returning a
/// concatenated `String` as before) so `is_meaningful_transcription` can use
/// it as its primary signal instead of pattern-matching the text alone.
#[derive(Debug)]
struct TranscriptSegment {
    text: String,
    no_speech_probability: f32,
}

/// The full result of one `transcribe()` call: the plain concatenated text
/// (what actually gets surfaced to the frontend on a "result" event) plus
/// the per-segment breakdown `is_meaningful_transcription` inspects.
#[derive(Debug)]
struct TranscriptionResult {
    text: String,
    segments: Vec<TranscriptSegment>,
}

fn transcribe(samples_16khz_mono: &[f32]) -> Result<TranscriptionResult, String> {
    let ctx = whisper_context()?;
    let mut state = ctx
        .create_state()
        .map_err(|e| format!("failed to create whisper inference state: {e}"))?;

    let n_threads = std::thread::available_parallelism()
        .map(|n| n.get() as i32)
        .unwrap_or(4);

    let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
    params.set_n_threads(n_threads);
    params.set_translate(false);
    params.set_language(Some("en"));
    params.set_print_progress(false);
    params.set_print_special(false);
    params.set_print_realtime(false);
    params.set_print_timestamps(false);
    // whisper.cpp computes `no_speech_probability` per segment regardless of
    // this threshold (as of whisper-rs 0.16 / whisper.cpp, `set_no_speech_thold`
    // is documented as "not implemented" — it doesn't gate anything internally),
    // so the value set here is inert; the actual filtering happens afterwards
    // in `is_meaningful_transcription`, using each segment's raw probability.
    // Left at whisper.cpp's own documented default (0.6) for clarity.
    params.set_no_speech_thold(0.6);

    state
        .full(params, samples_16khz_mono)
        .map_err(|e| format!("whisper inference failed: {e}"))?;

    let num_segments = state.full_n_segments();
    let mut text = String::new();
    let mut segments = Vec::with_capacity(num_segments.max(0) as usize);
    for i in 0..num_segments {
        if let Some(segment) = state.get_segment(i) {
            let no_speech_probability = segment.no_speech_probability();
            if let Ok(segment_text) = segment.to_str() {
                text.push_str(segment_text);
                segments.push(TranscriptSegment {
                    text: segment_text.to_string(),
                    no_speech_probability,
                });
            }
        }
    }
    Ok(TranscriptionResult {
        text: text.trim().to_string(),
        segments,
    })
}

/// The no-speech-probability threshold above which a segment is treated as
/// "the model itself doesn't think this is speech" — whisper.cpp's own
/// default for this exact purpose (see `set_no_speech_thold`'s doc comment
/// in whisper-rs, and `full_default_params` in whisper.cpp upstream).
const NO_SPEECH_PROBABILITY_THRESHOLD: f32 = 0.6;

/// Whether the *entire* trimmed transcript is a single bracket/paren-wrapped
/// non-speech annotation and nothing else — e.g. `[BLANK_AUDIO]`, `(silence)`,
/// `[no audio]`, `[music]`. This is specifically how whisper(.cpp) denotes a
/// detected non-speech audio event, distinct from real dictated speech, which
/// would essentially never come out as one bracket-wrapped phrase and nothing
/// else. Used as a fallback/defense-in-depth layer alongside the
/// no-speech-probability check, since it needs no model state to evaluate and
/// catches cases the probability threshold might not (e.g. a hallucinated
/// annotation the model was nonetheless fairly "confident" in).
fn is_bracketed_annotation(trimmed: &str) -> bool {
    let mut chars = trimmed.chars();
    let (Some(first), Some(last)) = (chars.next(), trimmed.chars().last()) else {
        return false;
    };
    // Require the brackets to actually pair up (`[...]` or `(...)`), and at
    // least one character between them — `[]` alone isn't a real annotation
    // whisper emits, and requiring a matched pair (rather than the loosest
    // possible "starts with an opener, ends with a closer" reading) avoids
    // false positives like `[whatever the user says] (this way)`.
    let pair_matches = matches!((first, last), ('[', ']') | ('(', ')'));
    pair_matches && trimmed.len() > 2
}

/// Whether a transcription result is worth surfacing to the frontend as a
/// usable dictation result, vs. treating it the same as if nothing
/// meaningful happened at all. Silence and near-silent audio are a
/// documented weak spot for whisper/whisper.cpp: instead of returning an
/// empty string, the model frequently hallucinates plausible-sounding filler
/// text, or emits a bracketed non-speech annotation it was trained to use
/// for exactly this case — a plain "is the string empty" check (the previous
/// version of this function) does nothing against either.
///
/// Primary signal: whisper.cpp's own per-segment `no_speech_probability` —
/// if every segment the model produced is above
/// `NO_SPEECH_PROBABILITY_THRESHOLD`, the model itself is telling us this
/// wasn't speech, regardless of what text came out. Falls back to trusting
/// the text when there are no segments to check (e.g. a synthetic/test
/// `TranscriptionResult` with an empty `segments` vec) rather than silently
/// treating every such case as meaningless.
///
/// Secondary/fallback layer, applied regardless of the probability check:
/// rejects a transcript that's nothing but a single bracket/paren-wrapped
/// annotation (see `is_bracketed_annotation`) — defense-in-depth in case a
/// hallucinated annotation happens to score a lower no-speech probability
/// than the threshold.
///
/// Pure logic over a `TranscriptionResult`, deliberately factored out of
/// `finish_recording_and_transcribe`'s emit decision so it's testable
/// without a real `WhisperContext`/model file.
fn is_meaningful_transcription(result: &TranscriptionResult) -> bool {
    let trimmed = result.text.trim();
    if trimmed.is_empty() {
        return false;
    }
    if is_bracketed_annotation(trimmed) {
        return false;
    }
    if !result.segments.is_empty()
        && result
            .segments
            .iter()
            .all(|s| s.no_speech_probability >= NO_SPEECH_PROBABILITY_THRESHOLD)
    {
        return false;
    }
    true
}

fn emit_dictation_status<R: tauri::Runtime>(app: &tauri::AppHandle<R>, event: serde_json::Value) {
    let _ = app.emit("dictation-status", event);
}

/// Stops the mic thread, recovers its buffer, resamples, and transcribes —
/// everything here is either blocking I/O (joining a thread) or CPU-bound
/// (resampling, whisper inference), so both halves run via `spawn_blocking`
/// rather than on the async runtime the rest of the app (sessions,
/// automations scheduler) shares.
async fn finish_recording_and_transcribe(active: ActiveRecording) -> Result<TranscriptionResult, String> {
    let (raw_samples, source_sample_rate) = tauri::async_runtime::spawn_blocking(move || {
        let _ = active.stop_tx.send(());
        active
            .join_handle
            .join()
            .map_err(|_| "microphone capture thread panicked".to_string())?;
        let samples = active
            .buffer
            .lock()
            .expect("dictation buffer mutex poisoned")
            .clone();
        Ok::<(Vec<f32>, u32), String>((samples, active.source_sample_rate))
    })
    .await
    .map_err(|e| format!("failed to join microphone capture thread: {e}"))??;

    log::info!(
        "voice: recording stopped, captured {} raw samples at {source_sample_rate}Hz ({:.1}s)",
        raw_samples.len(),
        raw_samples.len() as f32 / source_sample_rate as f32
    );
    if raw_samples.is_empty() {
        log::warn!("voice: no audio captured — check microphone permission/device");
        return Err("no audio captured — try again".to_string());
    }

    let result = tauri::async_runtime::spawn_blocking(move || {
        let resampled = resample_linear(&raw_samples, source_sample_rate, WHISPER_SAMPLE_RATE);
        transcribe(&resampled)
    })
    .await
    .map_err(|e| format!("transcription task failed: {e}"))?;

    match &result {
        Ok(transcription) => log::info!(
            "voice: transcription result: {:?} (segments: {:?})",
            transcription.text,
            transcription
                .segments
                .iter()
                .map(|s| (s.text.as_str(), s.no_speech_probability))
                .collect::<Vec<_>>()
        ),
        Err(message) => log::error!("voice: transcription failed: {message}"),
    }
    result
}

/// Thin IPC wrapper — see `toggle_dictation_internal` for the actual logic.
/// Kept separate so both trigger paths (this frontend hotkey IPC call, and
/// `fn_key.rs`'s native Fn-key monitor) run the exact same toggle code
/// instead of two parallel implementations of "toggle dictation".
#[tauri::command]
pub async fn toggle_dictation<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    toggle_dictation_internal(app).await
}

/// Starts microphone capture if idle and emits `{type:"recording", locked}`.
/// `locked` is purely a status label for the frontend (see this module's doc
/// comment on the event contract) — it has no effect on capture itself; a
/// "locked" recording is stopped exactly the same way as an unlocked one
/// (`stop_recording_and_transcribe_internal`), just triggered by a different
/// gesture upstream in `fn_key.rs`.
///
/// Errors exactly as the old `toggle_dictation_internal`'s Idle branch did:
/// model not downloaded, or any underlying microphone failure. Also errors
/// (new, since this is no longer a toggle) if a recording is already active
/// or a previous one is still being transcribed — starting is not
/// idempotent, unlike a toggle which would have interpreted a second call as
/// "stop".
pub(crate) async fn start_recording_internal<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    locked: bool,
) -> Result<(), String> {
    log::info!("voice: start_recording invoked (locked={locked})");

    {
        let mut state = DICTATION_STATE.lock().expect("dictation state mutex poisoned");
        match &*state {
            DictationState::Idle => {
                if !model_path().is_file() {
                    log::warn!("voice: start_recording rejected — model not downloaded");
                    return Err(
                        "voice model not downloaded — download the voice model in Settings first".to_string(),
                    );
                }
                match start_recording() {
                    Ok(active) => *state = DictationState::Recording(active),
                    Err(message) => return Err(message),
                }
            }
            DictationState::Recording(_) => {
                log::warn!("voice: start_recording rejected — already recording");
                return Err("a recording is already in progress".to_string());
            }
            DictationState::Transcribing => {
                log::warn!("voice: start_recording rejected — still transcribing the previous recording");
                return Err("still transcribing the previous recording — one moment".to_string());
            }
        }
    }

    emit_dictation_status(&app, serde_json::json!({ "type": "recording", "locked": locked }));
    Ok(())
}

/// Stops the active recording and transcribes it — same logic previously
/// inline in `toggle_dictation_internal`'s Recording branch. Errors if
/// there's no active recording to stop (idle) or one is already being
/// transcribed.
pub(crate) async fn stop_recording_and_transcribe_internal<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
) -> Result<(), String> {
    log::info!("voice: stop_recording_and_transcribe invoked");

    let active = {
        let mut state = DICTATION_STATE.lock().expect("dictation state mutex poisoned");
        match &*state {
            DictationState::Recording(_) => {
                let previous = std::mem::replace(&mut *state, DictationState::Transcribing);
                match previous {
                    DictationState::Recording(active) => active,
                    _ => unreachable!("just matched Recording above"),
                }
            }
            DictationState::Idle => {
                log::warn!("voice: stop_recording rejected — nothing is recording");
                return Err("no recording is in progress".to_string());
            }
            DictationState::Transcribing => {
                log::warn!("voice: stop_recording rejected — already transcribing");
                return Err("still transcribing the previous recording — one moment".to_string());
            }
        }
    };

    emit_dictation_status(&app, serde_json::json!({ "type": "transcribing" }));
    let app_for_task = app.clone();
    tauri::async_runtime::spawn(async move {
        let outcome = finish_recording_and_transcribe(active).await;
        match outcome {
            Ok(transcription) if is_meaningful_transcription(&transcription) => {
                emit_dictation_status(
                    &app_for_task,
                    serde_json::json!({ "type": "result", "text": transcription.text }),
                )
            }
            Ok(transcription) => {
                // Silence, a very brief accidental trigger, background noise
                // whisper decoded as empty/whitespace-only text, or a
                // hallucinated non-speech result (filler text or a
                // `[BLANK_AUDIO]`-style annotation the model emits instead of
                // an empty string for silent/near-silent audio — see
                // `is_meaningful_transcription`'s doc comment). Deliberately
                // *not* emitted as a `"result"` event — that would hand the
                // frontend blank/junk "dictated" content to act on. Logged
                // (not warned) since this is an expected, benign outcome, not
                // a failure.
                //
                // Still emits *something* — a dedicated, lightweight
                // "no_speech" event — rather than nothing at all: the
                // frontend's displayed state moved to "transcribing" when
                // this turn started, and with no event to move it back,
                // it would stay stuck showing "transcribing…" indefinitely
                // after every silent/blank recording, with nothing ever
                // telling it otherwise.
                log::debug!(
                    "voice: transcription produced no meaningful text ({:?}) — treating as a no-op",
                    transcription.text
                );
                emit_dictation_status(&app_for_task, serde_json::json!({ "type": "no_speech" }));
            }
            Err(message) => {
                emit_dictation_status(&app_for_task, serde_json::json!({ "type": "error", "message": message }))
            }
        }
        // Whatever the outcome, dictation is done with this recording —
        // always return to idle so the next start-recording call (whichever
        // gesture/hotkey it comes from) begins a fresh one.
        *DICTATION_STATE.lock().expect("dictation state mutex poisoned") = DictationState::Idle;
    });
    Ok(())
}

/// Re-emits `{type:"recording", locked:true}` on an *already-running*
/// recording — a pure status update for the frontend the moment a
/// double-tap confirms hands-free lock. Deliberately doesn't touch
/// `DICTATION_STATE` or the actual audio capture at all: nothing about the
/// recording itself changes when it becomes "locked", only how `fn_key.rs`
/// interprets the *next* Fn press.
pub(crate) fn emit_recording_locked<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    emit_dictation_status(app, serde_json::json!({ "type": "recording", "locked": true }));
}

/// The standard `CommandOrControl+Shift+D` hotkey's simple toggle — Idle
/// starts a (never-locked) recording, Recording stops and transcribes it,
/// Transcribing is rejected. Reimplemented as thin composition of
/// `start_recording_internal`/`stop_recording_and_transcribe_internal` (the
/// same primitives `fn_key.rs`'s gesture state machine calls directly) —
/// same external behavior as before this split, no regression.
pub(crate) async fn toggle_dictation_internal<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    // Logged unconditionally, before anything else — this is the single most
    // useful line in the whole module for diagnosing "the hotkey doesn't
    // seem to do anything": if this never shows up in the log after
    // pressing it, the problem is the hotkey registration/OS layer, not
    // anything below this point (model, mic, whisper). If it *does* show up
    // every time, the fault is somewhere further down and the rest of this
    // function's logging takes over from here.
    log::info!("voice: toggle_dictation invoked");

    enum Branch {
        Idle,
        Recording,
        Transcribing,
    }

    let branch = {
        let state = DICTATION_STATE.lock().expect("dictation state mutex poisoned");
        match &*state {
            DictationState::Idle => Branch::Idle,
            DictationState::Recording(_) => Branch::Recording,
            DictationState::Transcribing => Branch::Transcribing,
        }
    };

    match branch {
        Branch::Idle => start_recording_internal(app, false).await,
        Branch::Recording => stop_recording_and_transcribe_internal(app).await,
        Branch::Transcribing => {
            log::warn!("voice: toggle_dictation rejected — still transcribing the previous recording");
            Err("still transcribing the previous recording — one moment".to_string())
        }
    }
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

    #[test]
    fn resample_linear_scales_length_by_rate_ratio() {
        let input = vec![0.0_f32; 48_000];
        let resampled = super::resample_linear(&input, 48_000, 16_000);
        assert_eq!(resampled.len(), 16_000);
    }

    #[test]
    fn resample_linear_is_a_no_op_at_equal_rates() {
        let input = vec![1.0, -1.0, 0.5];
        let resampled = super::resample_linear(&input, 16_000, 16_000);
        assert_eq!(resampled, input);
    }

    #[test]
    fn resample_linear_handles_empty_input() {
        let resampled = super::resample_linear(&[], 48_000, 16_000);
        assert!(resampled.is_empty());
    }

    /// Builds a synthetic `TranscriptionResult` for `is_meaningful_transcription`
    /// tests, without needing a real `WhisperContext`/model — one segment per
    /// given no-speech probability, all sharing `text` as the concatenated
    /// result (matching what a single-segment `transcribe()` call would
    /// produce; multi-segment cases construct segments directly where the
    /// per-segment text matters).
    fn transcription(text: &str, segment_no_speech_probabilities: &[f32]) -> super::TranscriptionResult {
        super::TranscriptionResult {
            text: text.to_string(),
            segments: segment_no_speech_probabilities
                .iter()
                .map(|&no_speech_probability| super::TranscriptSegment {
                    text: text.to_string(),
                    no_speech_probability,
                })
                .collect(),
        }
    }

    #[test]
    fn is_meaningful_transcription_rejects_blank_text() {
        assert!(!super::is_meaningful_transcription(&transcription("", &[0.05])));
        assert!(!super::is_meaningful_transcription(&transcription("   ", &[0.05])));
        assert!(!super::is_meaningful_transcription(&transcription("\n\t  \n", &[0.05])));
        // No segments at all (e.g. whisper returned zero segments) with blank
        // text should still be rejected on the text-emptiness check alone.
        assert!(!super::is_meaningful_transcription(&transcription("", &[])));
    }

    #[test]
    fn is_meaningful_transcription_accepts_real_text_with_low_no_speech_probability() {
        assert!(super::is_meaningful_transcription(&transcription(
            "apply to the jobs",
            &[0.05]
        )));
        // Leading/trailing whitespace around otherwise-real text still counts
        // — `transcribe()` already trims before this guard ever sees it, but
        // the guard itself shouldn't depend on that upstream trimming.
        assert!(super::is_meaningful_transcription(&transcription("  hello  ", &[0.1])));
    }

    #[test]
    fn is_meaningful_transcription_accepts_text_with_no_segments() {
        // No segment metadata to check (e.g. a hand-built result, or a future
        // whisper-rs version that fails to report segments) — falls back to
        // trusting non-blank, non-bracketed text rather than rejecting
        // everything just because there was nothing to check.
        assert!(super::is_meaningful_transcription(&transcription(
            "apply to the jobs",
            &[]
        )));
    }

    #[test]
    fn is_meaningful_transcription_rejects_high_no_speech_probability_despite_hallucinated_text() {
        // The documented whisper.cpp failure mode this fix targets: silent
        // audio decoded as plausible-sounding filler text ("Thank you.", "you")
        // rather than an empty string. A plain non-empty-string check would
        // wrongly accept this; the no-speech-probability signal should catch
        // it even though the text itself looks like ordinary speech.
        assert!(!super::is_meaningful_transcription(&transcription("Thank you.", &[0.9])));
    }

    #[test]
    fn is_meaningful_transcription_accepts_when_only_some_segments_are_high_no_speech() {
        // A multi-segment transcript where at least one segment is
        // confidently real speech should still be surfaced, even if another
        // segment (e.g. a leading/trailing silent stretch) scores high.
        assert!(super::is_meaningful_transcription(&transcription(
            "apply to the jobs",
            &[0.9, 0.05]
        )));
    }

    #[test]
    fn is_meaningful_transcription_rejects_bracketed_non_speech_annotations() {
        // Whisper's own convention for flagging a detected non-speech audio
        // event — distinct from real dictated speech. Given a low no-speech
        // probability here deliberately, to prove the bracket check catches
        // it independently of the probability signal (defense-in-depth).
        for annotation in ["[BLANK_AUDIO]", "[SILENCE]", "[no audio]", "(silence)", "[music]"] {
            assert!(
                !super::is_meaningful_transcription(&transcription(annotation, &[0.05])),
                "expected {annotation:?} to be rejected as a non-speech annotation"
            );
        }
    }

    #[test]
    fn is_meaningful_transcription_accepts_text_merely_containing_brackets() {
        // Only rejected when the *entire* trimmed transcript is one
        // bracket-wrapped span with nothing outside it — real dictated speech
        // that happens to mention brackets should still pass.
        assert!(super::is_meaningful_transcription(&transcription(
            "the file is called [draft].txt",
            &[0.05]
        )));
    }

    #[test]
    fn is_bracketed_annotation_matches_whisper_style_annotations() {
        for annotation in ["[BLANK_AUDIO]", "[SILENCE]", "(silence)", "[music]"] {
            assert!(super::is_bracketed_annotation(annotation));
        }
    }

    #[test]
    fn is_bracketed_annotation_rejects_ordinary_text() {
        for text in ["hello", "[draft].txt mentions brackets", "[]", "(", "["] {
            assert!(!super::is_bracketed_annotation(text));
        }
    }

    #[test]
    fn is_bracketed_annotation_rejects_mismatched_bracket_types() {
        // First/last characters are both "brackets" but don't form a matched
        // pair — shouldn't be treated as one of whisper's real annotations.
        assert!(!super::is_bracketed_annotation("[mismatched)"));
    }

    /// A disposable pair of temp directories standing in for
    /// `project_root().join("whisper-models")` (legacy) and
    /// `data_dir().join("whisper-models")` (new) — every
    /// `migrate_legacy_model` test below operates entirely on these, never on
    /// this crate's own real dev-checkout `whisper-models/` directory (which,
    /// on a machine that's actually downloaded the model, is exactly the
    /// real file this migration exists to move — the last thing a test
    /// should touch).
    struct MigrationDirs {
        base: std::path::PathBuf,
        legacy_dir: std::path::PathBuf,
        new_dir: std::path::PathBuf,
    }

    impl MigrationDirs {
        fn new(label: &str) -> Self {
            let base = std::env::temp_dir().join(format!(
                "daimon-voice-migration-test-{}-{}-{label}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::SystemTime::UNIX_EPOCH)
                    .expect("system clock before the epoch")
                    .as_nanos()
            ));
            let legacy_dir = base.join("legacy");
            let new_dir = base.join("new");
            std::fs::create_dir_all(&legacy_dir).expect("failed to create isolated legacy dir");
            Self { base, legacy_dir, new_dir }
        }
    }

    impl Drop for MigrationDirs {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.base);
        }
    }

    #[test]
    fn migrate_legacy_model_moves_the_file_to_the_new_location() {
        let dirs = MigrationDirs::new("moves");
        let legacy_path = dirs.legacy_dir.join(super::MODEL_FILENAME);
        std::fs::write(&legacy_path, b"fake ggml model bytes").expect("failed to write fake legacy model file");

        super::migrate_legacy_model(&dirs.new_dir, &dirs.legacy_dir);

        let new_path = dirs.new_dir.join(super::MODEL_FILENAME);
        assert!(new_path.is_file(), "expected the model file to exist at the new location");
        assert!(!legacy_path.exists(), "expected the legacy file to be gone after a successful migration");
        assert_eq!(
            std::fs::read(&new_path).expect("failed to read migrated file"),
            b"fake ggml model bytes",
            "migrated file content should be unchanged"
        );
    }

    #[test]
    fn migrate_legacy_model_is_a_no_op_when_no_legacy_file_exists() {
        // The common case for a brand-new install that's never downloaded
        // the model at all — must not create the new directory or error,
        // just leave the normal "not downloaded yet" flow to take over.
        let dirs = MigrationDirs::new("no-legacy");

        super::migrate_legacy_model(&dirs.new_dir, &dirs.legacy_dir);

        assert!(
            !dirs.new_dir.join(super::MODEL_FILENAME).exists(),
            "should not fabricate a model file when there's nothing to migrate"
        );
    }

    #[test]
    fn migrate_legacy_model_does_not_overwrite_an_existing_new_file() {
        // Guards against a stale legacy copy clobbering a model that was
        // already freshly downloaded (or already migrated) at the new
        // location — the legacy file should be left completely untouched in
        // this case, not silently deleted.
        let dirs = MigrationDirs::new("already-present");
        let legacy_path = dirs.legacy_dir.join(super::MODEL_FILENAME);
        std::fs::write(&legacy_path, b"old legacy bytes").expect("failed to write fake legacy model file");
        std::fs::create_dir_all(&dirs.new_dir).expect("failed to create new dir");
        let new_path = dirs.new_dir.join(super::MODEL_FILENAME);
        std::fs::write(&new_path, b"already-downloaded bytes").expect("failed to write fake new model file");

        super::migrate_legacy_model(&dirs.new_dir, &dirs.legacy_dir);

        assert_eq!(
            std::fs::read(&new_path).unwrap(),
            b"already-downloaded bytes",
            "existing new-location file should not be overwritten by a stale legacy copy"
        );
        assert!(legacy_path.is_file(), "legacy file should be left alone, not deleted, when the new file already exists");
    }

    /// Points `model_dir()` at a fresh, empty temp directory for the
    /// duration of this test, via `TEST_MODEL_DIR_OVERRIDE`, so
    /// `model_path().is_file()` is guaranteed false without ever touching a
    /// developer's real downloaded model file.
    ///
    /// This replaces an earlier version that renamed the *real* model file
    /// out of the way and relied on a `Drop` impl to rename it back — which
    /// turned out not to be crash-safe in practice: a hard process abort
    /// (verified the hard way — a segfault in an unrelated test during this
    /// project's own development, from a native FFI bug since fixed,
    /// happened to kill the whole `cargo test` process while this guard's
    /// stash was in effect) skips `Drop` entirely, silently leaving the real
    /// model permanently renamed with no error surfaced anywhere — exactly
    /// what happened. Operating on a disposable per-test temp directory
    /// instead means there's no shared state left to restore, so there's
    /// nothing for a crash to corrupt.
    struct HideModelFile {
        temp_dir: std::path::PathBuf,
    }

    impl HideModelFile {
        fn new() -> Self {
            let temp_dir = std::env::temp_dir().join(format!("daimon-voice-test-{}", std::process::id()));
            std::fs::create_dir_all(&temp_dir).expect("failed to create isolated test model directory");
            *super::TEST_MODEL_DIR_OVERRIDE
                .lock()
                .expect("test model dir override mutex poisoned") = Some(temp_dir.clone());
            Self { temp_dir }
        }
    }

    impl Drop for HideModelFile {
        fn drop(&mut self) {
            *super::TEST_MODEL_DIR_OVERRIDE
                .lock()
                .expect("test model dir override mutex poisoned") = None;
            let _ = std::fs::remove_dir_all(&self.temp_dir);
        }
    }

    /// Same ACL-reachability pattern as `vault.rs`/`settings.rs` — a missing
    /// `"allow-<command>"` capability entry compiles fine and only fails at
    /// runtime.
    ///
    /// `toggle_dictation` is deliberately exercised only via the
    /// model-not-downloaded error path (guaranteed by `HideModelFile`, not
    /// merely assumed from a clean checkout): letting it fall through to
    /// real microphone capture here would mean an automated `cargo test` run
    /// could pop a real macOS mic-permission prompt and/or actually start
    /// recording live audio with nothing left running to ever stop it — a
    /// real instance of exactly what `ARCHITECTURE.md` §5 exists to prevent,
    /// not just a flaky test.
    #[test]
    fn voice_commands_clear_the_acl() {
        let _hide_model = HideModelFile::new();

        let app = crate::build_app(mock_builder());
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");

        let status = get_ipc_response(&webview, invoke_request("get_voice_model_status", serde_json::json!({})))
            .expect("get_voice_model_status should be allowed by the capability");
        let status: super::VoiceModelStatus = status.deserialize().expect("expected a VoiceModelStatus");
        assert!(!status.downloaded, "model file should be hidden for the duration of this test");

        let toggle_result = get_ipc_response(&webview, invoke_request("toggle_dictation", serde_json::json!({})));
        let message = toggle_result.expect_err("toggle_dictation should error with no model downloaded");
        let message = message.as_str().unwrap_or_default();
        assert!(
            !message.contains("not allowed"),
            "toggle_dictation should be ACL-allowed even though this call errors for other reasons, got: {message}"
        );
        assert!(
            message.contains("not downloaded"),
            "expected the model-not-downloaded error (never reaching real mic capture), got: {message}"
        );

        // download_voice_model is deliberately not invoked here — even the
        // idempotent "already downloaded" path aside, kicking off a network
        // call from this reachability test would make `cargo test` flaky/slow
        // for reasons unrelated to what this test checks. Its own real,
        // network-touching behavior is covered by the `#[ignore]`d test
        // below, run manually.
    }

    /// A real, one-time end-to-end check of the legacy-model migration (see
    /// `migrate_legacy_model`'s doc comment): resolves this crate's *real*
    /// OS app-data directory via a real `AppHandle`'s
    /// `app.path().app_data_dir()` (not `HideModelFile`'s temp-dir override),
    /// then confirms `model_path()` — and therefore every real dictation
    /// attempt from here on — resolves to an actually-present, migrated
    /// file. This is the literal scenario the migration exists to fix: a
    /// pre-migration install with the model already downloaded at the old
    /// `project_root()`-relative location.
    ///
    /// `#[ignore]`d, like the network-touching test below, for a different
    /// reason: this one specifically calls `workspace::init_data_dir`, which
    /// sets `workspace::DATA_DIR` — a process-global `OnceLock` — for the
    /// remaining lifetime of whatever process runs it. Every other test in
    /// this crate relies on that `OnceLock` staying unset (so `data_dir()`
    /// keeps falling back to the git-checkout-relative `project_root()`);
    /// running this test alongside any of them in the same `cargo test`
    /// process would silently corrupt their view of where data lives. Run
    /// alone: `cargo test --lib -- --ignored --test-threads=1
    /// real_app_data_dir_migration_moves_the_legacy_model_into_place`.
    #[test]
    #[ignore]
    fn real_app_data_dir_migration_moves_the_legacy_model_into_place() {
        let app = crate::build_app(mock_builder());
        crate::workspace::init_data_dir(app.handle());

        let path = super::model_path();
        eprintln!("resolved real (post-migration) model path: {}", path.display());
        assert!(
            path.is_file(),
            "expected the whisper model to be present (migrated from the legacy path, or already there) at {}",
            path.display()
        );

        // Also confirm the IPC surface a real Settings screen (or
        // `start_recording_internal`'s "is the model there" check) actually
        // relies on agrees — not just that the raw file exists on disk.
        let webview = WebviewWindowBuilder::new(&app, "pill", Default::default())
            .build()
            .expect("failed to build mock webview");
        let status = get_ipc_response(&webview, invoke_request("get_voice_model_status", serde_json::json!({})))
            .expect("get_voice_model_status should succeed")
            .deserialize::<super::VoiceModelStatus>()
            .expect("expected a VoiceModelStatus");
        assert!(
            status.downloaded,
            "get_voice_model_status should report the model as downloaded after migration"
        );
    }

    /// Exercises the real download path end-to-end (a real HTTPS request
    /// landing at `model_path()`) and then proves a `WhisperContext` loads
    /// from the resulting file and can run inference without crashing.
    /// `#[ignore]`d by default — this pulls ~148MB over the network and
    /// shouldn't run on every `cargo test`; run explicitly with
    /// `cargo test -- --ignored` when validating this module.
    #[tokio::test(flavor = "multi_thread")]
    #[ignore]
    async fn download_and_transcribe_synthetic_tone_end_to_end() {
        super::download_voice_model(app_handle_for_test())
            .await
            .expect("download_voice_model should succeed against the real model host");
        assert!(super::model_path().is_file(), "model file should exist after download");

        // A short synthetic tone, not speech — this only proves the
        // inference call itself doesn't crash on a real 16kHz mono f32
        // buffer, not that transcription is accurate (whisper will very
        // likely emit nonsense or near-empty text for a pure tone).
        let sample_rate = super::WHISPER_SAMPLE_RATE;
        let duration_secs = 1.0_f32;
        let frequency = 440.0_f32;
        let samples: Vec<f32> = (0..(sample_rate as f32 * duration_secs) as usize)
            .map(|i| (2.0 * std::f32::consts::PI * frequency * (i as f32 / sample_rate as f32)).sin() * 0.1)
            .collect();

        let result = tauri::async_runtime::spawn_blocking(move || super::transcribe(&samples))
            .await
            .expect("transcribe task should not panic");
        assert!(
            result.is_ok(),
            "whisper inference should not error on a well-formed 16kHz mono f32 buffer, got: {result:?}"
        );
    }

    fn app_handle_for_test() -> tauri::AppHandle<tauri::test::MockRuntime> {
        let app = crate::build_app(mock_builder());
        app.handle().clone()
    }
}
