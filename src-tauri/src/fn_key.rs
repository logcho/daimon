//! macOS-only global "Fn key" trigger for dictation, additive alongside the
//! standard `CommandOrControl+Shift+D` hotkey registered from the frontend
//! (see `voice.rs`'s module doc for why that one was chosen as the *initial*
//! trigger, and this module's existence for the deferred "real" one).
//!
//! The bare Fn key isn't a normal key/hotkey — it's a hardware modifier flag
//! that Tauri's `global-shortcut` plugin (and every standard cross-platform
//! hotkey crate) has no visibility into. The only reliable way to observe it
//! is an `NSEvent` global monitor watching `.flagsChanged` events and
//! comparing the `Function` modifier bit across consecutive events: a
//! transition from unset to set is a "rising edge" (press), unset-to-set-to-
//! unset the reverse is a "falling edge" (release). This is the same
//! technique real dictation apps that trigger off Fn use.
//!
//! Unlike the standard hotkey (a simple toggle — unchanged, see `voice.rs`),
//! the Fn key gives genuine press/release granularity, so it drives three
//! distinct gestures instead of one flat toggle:
//! - **Hold-to-talk:** press and hold -> records while held; release ->
//!   stops and transcribes.
//! - **Double-tap-to-lock:** two quick taps -> keeps recording hands-free
//!   until the *next* single press, which stops and transcribes.
//! - Both start recording on the very first press unconditionally — a
//!   press-down can't be distinguished from "start of a hold" vs. "first
//!   half of a double-tap" until it's released (and possibly followed by
//!   another press), so the ambiguity is resolved after the fact rather than
//!   guessed up front.
//!
//! See `GesturePhase`/`on_fn_key_edge`/`on_double_tap_timeout` for the actual
//! state machine, kept as pure, synchronously-testable logic separate from
//! the real `NSEvent` plumbing (which isn't unit-testable without a live
//! AppKit run loop and a physical key).
//!
//! A global `NSEvent` monitor only receives events at all if this process is
//! "trusted" for Accessibility — see `accessibility_trusted()`. That check
//! doesn't gate installing the monitor (there's no harm in trying, and the
//! user may grant trust after launch), but its result is logged clearly
//! either way: a trigger that silently never fires, with zero signal as to
//! why, is exactly the failure mode `voice.rs`'s hotkey path had to be
//! debugged out of before — this module logs instead of repeating that.
//!
//! A **global** monitor (`addGlobalMonitorForEventsMatchingMask:handler:`)
//! only ever receives events posted to *other* applications — per Apple's
//! own documentation for that API — never events directed at this app's own
//! windows while this app is key/active. Daimon's panel auto-expands (and
//! its chat textarea autofocuses) the moment a recording starts, so it's
//! entirely normal for Daimon itself to hold focus by the time the user
//! presses Fn again — e.g. to stop a double-tap-locked recording. A
//! global-only monitor would simply never see that second press. So this
//! module installs a **second**, `addLocalMonitorForEventsMatchingMask:
//! handler:` monitor for the same event mask, feeding the exact same
//! `on_fn_key_edge` state machine through the exact same shared
//! `GestureState`. A local monitor's handler must return the event it was
//! given (not swallow it) so it keeps propagating normally to whatever
//! Daimon UI would otherwise receive it — this module only observes, never
//! consumes. Because a global monitor and a local monitor are mutually
//! exclusive per physical event (each `.flagsChanged` event is either
//! directed at this app or at another one, never both), the two monitors
//! never double-fire for the same key transition; they're complementary
//! halves of one continuous observation, which is exactly why they must
//! share one `GestureState`/`previous_flags` pair rather than each keeping
//! their own — a gesture that starts while some other app has focus and
//! ends after Daimon's own window gained focus mid-gesture must still
//! resolve through one continuous state machine.

use std::time::Duration;

/// A press+release faster than this counts as a "tap" (the first half of a
/// potential double-tap, or a belated single-tap-release if no second press
/// follows); slower counts as a deliberate "hold". 280ms sits comfortably in
/// the ~250-300ms range typical for this kind of gesture disambiguation —
/// long enough that an ordinary fast tap reliably clears it, short enough
/// that a genuine hold-to-talk press (meant to be held for at least a
/// half-second or so of speech) never gets misread as a tap.
const TAP_MAX_HOLD_MS: u64 = 280;

/// The max gap, after a tap's release, within which a *new* press counts as
/// the second half of a double-tap rather than an unrelated later single
/// press. 400ms sits in the ~350-450ms range typical for double-tap/double-
/// click style gestures — enough time for a deliberate second tap, not so
/// long that two genuinely separate presses get merged into a false lock.
const DOUBLE_TAP_WINDOW_MS: u64 = 400;

const TAP_MAX_HOLD: Duration = Duration::from_millis(TAP_MAX_HOLD_MS);
const DOUBLE_TAP_WINDOW: Duration = Duration::from_millis(DOUBLE_TAP_WINDOW_MS);

// ----------------------------------------------------------------------------
// Gesture state machine — pure logic, no AppKit/audio/haptics involved, so it
// can be exercised directly by unit tests with synthetic `Instant`s. See the
// module doc above for the three gestures this implements and `on_fn_key_edge`
// for the transition table.
// ----------------------------------------------------------------------------

/// Which of the three gestures the Fn key is currently mid-way through.
/// `Instant`s here are the timestamp of the event that caused entry into
/// that phase (a press for `HeldWaitingRelease`, a release for
/// `PendingDoubleTap`) — compared against a *later* event's timestamp to
/// decide the next transition.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum GesturePhase {
    /// Nothing in progress; the next press starts a fresh recording.
    Idle,
    /// Recording is running; this press hasn't been released yet.
    HeldWaitingRelease { pressed_at: std::time::Instant },
    /// Recording is running; the first tap already released, waiting to see
    /// if a second press arrives within `DOUBLE_TAP_WINDOW`.
    PendingDoubleTap { first_release_at: std::time::Instant },
    /// Recording is running, hands-free; the next press stops it.
    Locked,
}

/// Gesture state plus a generation counter bumped on every transition.
/// The counter exists purely to let a scheduled double-tap-window timeout
/// (see `on_double_tap_timeout`) tell whether the `PendingDoubleTap` episode
/// it was scheduled for is still the current one, or whether the state has
/// since moved on (a second press confirmed the double-tap, or — in the rare
/// race handled below — a later, unrelated press arrived) — a stale timer
/// must never act on a state it no longer describes.
#[derive(Debug, Clone, Copy)]
pub(super) struct GestureState {
    phase: GesturePhase,
    generation: u64,
}

impl GestureState {
    pub(super) fn new() -> Self {
        Self {
            phase: GesturePhase::Idle,
            generation: 0,
        }
    }
}

/// What the caller (the real `NSEvent` handler, or a test) should do as a
/// result of one edge or one timeout check. Never more than a couple of
/// these are returned from one call, in order — e.g. a rare race in
/// `on_fn_key_edge` can resolve a stale pending tap (`StopAndTranscribe`)
/// *and* start a new recording (`StartRecording`) from the very same press.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum GestureAction {
    /// Begin a brand new (unlocked) recording.
    StartRecording,
    /// A double-tap just confirmed hands-free lock on the recording that's
    /// already running — relabel it as locked, don't touch capture itself.
    ConfirmDoubleTapLock,
    /// Stop the active recording and begin transcription.
    StopAndTranscribe,
    /// Schedule a check, `DOUBLE_TAP_WINDOW` from now, of whether this tap's
    /// `PendingDoubleTap` episode is still current by the time it fires (see
    /// `on_double_tap_timeout`). Carries the generation captured at the
    /// moment this action was produced, for that guard.
    ScheduleDoubleTapCheck { generation: u64 },
}

fn transition(state: &mut GestureState, phase: GesturePhase) {
    state.phase = phase;
    state.generation = state.generation.wrapping_add(1);
}

/// Pure transition logic for one `.flagsChanged` edge (`pressed = true` on a
/// rising edge, `false` on a falling edge) of the Fn key. `now` is the
/// event's timestamp. Returns the ordered actions the caller should perform.
///
/// Mirrors the transition table in `fn_key.rs`'s implementer-facing spec
/// exactly, with one addition for a race the spec doesn't explicitly
/// enumerate: a rising edge that arrives while `PendingDoubleTap` but
/// *after* `DOUBLE_TAP_WINDOW` has already elapsed (the scheduled timeout
/// just hasn't fired yet). Rather than misinterpret that press as
/// confirming a double-tap it's too late for, this resolves the stale
/// pending tap first (as the timeout would have) and then treats the press
/// as the start of a genuinely new, unrelated gesture — exactly the
/// outcome a perfectly-timed timeout firing a moment earlier would have
/// produced.
pub(super) fn on_fn_key_edge(
    state: &mut GestureState,
    pressed: bool,
    now: std::time::Instant,
) -> Vec<GestureAction> {
    match (pressed, state.phase) {
        (true, GesturePhase::Idle) => {
            transition(state, GesturePhase::HeldWaitingRelease { pressed_at: now });
            vec![GestureAction::StartRecording]
        }
        (true, GesturePhase::PendingDoubleTap { first_release_at }) => {
            if now.saturating_duration_since(first_release_at) <= DOUBLE_TAP_WINDOW {
                transition(state, GesturePhase::Locked);
                vec![GestureAction::ConfirmDoubleTapLock]
            } else {
                // Race: the pending tap's window has technically elapsed
                // but its scheduled timeout hasn't run yet. Resolve it first
                // (as that timeout would), then this press starts fresh.
                transition(state, GesturePhase::HeldWaitingRelease { pressed_at: now });
                vec![GestureAction::StopAndTranscribe, GestureAction::StartRecording]
            }
        }
        (true, GesturePhase::Locked) => {
            transition(state, GesturePhase::Idle);
            vec![GestureAction::StopAndTranscribe]
        }
        // Shouldn't happen (the key is already down) — no-op defensively.
        (true, GesturePhase::HeldWaitingRelease { .. }) => vec![],
        (false, GesturePhase::HeldWaitingRelease { pressed_at }) => {
            let held_duration = now.saturating_duration_since(pressed_at);
            if held_duration >= TAP_MAX_HOLD {
                transition(state, GesturePhase::Idle);
                vec![GestureAction::StopAndTranscribe]
            } else {
                transition(state, GesturePhase::PendingDoubleTap { first_release_at: now });
                vec![GestureAction::ScheduleDoubleTapCheck {
                    generation: state.generation,
                }]
            }
        }
        // Falling edges in every other phase correspond to presses whose
        // "stop" action already fired on the press itself (Locked -> stop),
        // or don't need release-driven action at all — no-op.
        (false, GesturePhase::PendingDoubleTap { .. } | GesturePhase::Locked | GesturePhase::Idle) => vec![],
    }
}

/// Invoked once `DOUBLE_TAP_WINDOW` has elapsed since a tap's release — the
/// belated resolution of "no second press arrived, so that was just a
/// single tap after all". Guarded by `generation`: only acts if the state is
/// *still* `PendingDoubleTap` from the exact same episode this check was
/// scheduled for; any other current phase (or the same phase reached via a
/// different, later episode) means this timer is stale and must no-op.
pub(super) fn on_double_tap_timeout(state: &mut GestureState, generation: u64) -> Vec<GestureAction> {
    match state.phase {
        GesturePhase::PendingDoubleTap { .. } if state.generation == generation => {
            transition(state, GesturePhase::Idle);
            vec![GestureAction::StopAndTranscribe]
        }
        _ => vec![],
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use std::cell::Cell;
    use std::ptr::NonNull;
    use std::rc::Rc;
    use std::sync::{Arc, Mutex as StdMutex};

    use block2::RcBlock;
    use objc2::rc::Retained;
    use objc2::runtime::AnyObject;
    use objc2_app_kit::{
        NSEvent, NSEventMask, NSEventModifierFlags, NSHapticFeedbackManager, NSHapticFeedbackPattern,
        NSHapticFeedbackPerformanceTime, NSHapticFeedbackPerformer,
    };

    use super::{on_double_tap_timeout, on_fn_key_edge, GestureAction, GestureState, DOUBLE_TAP_WINDOW};

    // `AXIsProcessTrusted()` is a plain C function on `ApplicationServices`/
    // `HIServices` — a full binding crate would be overkill for one
    // boolean-returning, no-argument call. Its real return type is `Boolean`
    // (a `typedef unsigned char` in CoreFoundation, guaranteed to only ever
    // hold 0 or 1), so it's declared here as `c_uchar` and compared
    // explicitly rather than assumed FFI-compatible with Rust's `bool`.
    #[link(name = "ApplicationServices", kind = "framework")]
    unsafe extern "C" {
        fn AXIsProcessTrusted() -> std::os::raw::c_uchar;
    }

    /// Whether this process is currently trusted for Accessibility — a
    /// prerequisite for the global `NSEvent` monitor below to receive any
    /// events at all. Doesn't (and, per this feature's explicit scope, isn't
    /// meant to) trigger the system permission *prompt* — see this module's
    /// doc comment and the IPC command in `lib.rs` for how that status is
    /// surfaced to the user instead.
    pub fn accessibility_trusted() -> bool {
        unsafe { AXIsProcessTrusted() != 0 }
    }

    /// Which of the three haptic patterns fires at each meaningful gesture
    /// transition. Deliberately three *distinct* patterns (not two reused
    /// across four events) so each feels distinguishable on the trackpad:
    /// - `Generic` — recording just started (any gesture: a fresh press,
    ///   whether it turns out to be a hold or the first half of a
    ///   double-tap — both start capture identically).
    /// - `LevelChange` — recording just stopped and transcription began.
    /// - `Alignment` — a double-tap just engaged hands-free lock. This is
    ///   the one transition with no other physical confirmation (no key
    ///   being held down), so it's worth a haptic distinct from the other
    ///   two.
    fn fire_haptic(pattern: NSHapticFeedbackPattern) {
        let performer = NSHapticFeedbackManager::defaultPerformer();
        performer.performFeedbackPattern_performanceTime(pattern, NSHapticFeedbackPerformanceTime::Now);
    }

    /// Executes one `GestureAction` against the real world: starts/stops
    /// real microphone capture via `voice.rs`'s granular primitives, emits
    /// the locked-status update, fires the matching haptic, and — for
    /// `ScheduleDoubleTapCheck` — spawns the delayed timeout check itself
    /// (which may recursively call back into this function once it fires).
    fn perform_action<R: tauri::Runtime>(
        app: &tauri::AppHandle<R>,
        gesture_state: &Arc<StdMutex<GestureState>>,
        action: GestureAction,
    ) {
        match action {
            GestureAction::StartRecording => {
                fire_haptic(NSHapticFeedbackPattern::Generic);
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(message) = crate::voice::start_recording_internal(app, false).await {
                        log::warn!("fn_key: start_recording failed: {message}");
                    }
                });
            }
            GestureAction::ConfirmDoubleTapLock => {
                fire_haptic(NSHapticFeedbackPattern::Alignment);
                crate::voice::emit_recording_locked(app);
            }
            GestureAction::StopAndTranscribe => {
                fire_haptic(NSHapticFeedbackPattern::LevelChange);
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(message) = crate::voice::stop_recording_and_transcribe_internal(app).await {
                        log::warn!("fn_key: stop_recording_and_transcribe failed: {message}");
                    }
                });
            }
            GestureAction::ScheduleDoubleTapCheck { generation } => {
                let app = app.clone();
                let gesture_state = gesture_state.clone();
                tauri::async_runtime::spawn(async move {
                    tokio::time::sleep(DOUBLE_TAP_WINDOW).await;
                    let actions = {
                        let mut guard = gesture_state.lock().expect("fn_key gesture state mutex poisoned");
                        on_double_tap_timeout(&mut guard, generation)
                    };
                    for action in actions {
                        perform_action(&app, &gesture_state, action);
                    }
                });
            }
        }
    }

    /// One `.flagsChanged` event, shared verbatim by both the global and
    /// local monitors installed below: detects whether this event is a
    /// rising/falling edge on the Function modifier bit specifically (vs.
    /// some other modifier changing, which is a no-op here), and if so runs
    /// it through the shared `GestureState` via `on_fn_key_edge`, performing
    /// whatever actions come out.
    ///
    /// Deliberately the *only* place this logic is written — both monitors
    /// call this function rather than each embedding their own copy, which
    /// is exactly what keeps a gesture that starts while some other app has
    /// focus and ends after Daimon's own window gained focus (or vice versa)
    /// resolving through one continuous state machine instead of two
    /// independent ones that could each think they're starting fresh.
    fn handle_flags_changed_event<R: tauri::Runtime>(
        app: &tauri::AppHandle<R>,
        previous_flags: &Cell<NSEventModifierFlags>,
        gesture_state: &Arc<StdMutex<GestureState>>,
        event: NonNull<NSEvent>,
    ) {
        // SAFETY: AppKit guarantees the event pointer handed to a
        // flags-changed monitor (global or local) is a valid, live `NSEvent`
        // for the duration of this call.
        let flags = unsafe { event.as_ref() }.modifierFlags();
        let previous = previous_flags.replace(flags);

        let is_function_now = flags.contains(NSEventModifierFlags::Function);
        let was_function_before = previous.contains(NSEventModifierFlags::Function);
        if is_function_now == was_function_before {
            // Not an edge on the Function bit — some other modifier
            // changed (Shift, Control, ...); nothing to do.
            return;
        }
        let pressed = is_function_now;
        let now = std::time::Instant::now();

        log::info!("fn_key: Fn key {} detected", if pressed { "press" } else { "release" });

        let actions = {
            let mut guard = gesture_state.lock().expect("fn_key gesture state mutex poisoned");
            on_fn_key_edge(&mut guard, pressed, now)
        };
        for action in actions {
            perform_action(app, gesture_state, action);
        }
    }

    /// Installs a process-lifetime **global** monitor for `.flagsChanged`
    /// events — receives Fn-key presses only while some *other* application
    /// is key/active (see `addGlobalMonitorForEventsMatchingMask:handler:`'s
    /// own documentation), i.e. whenever Daimon's own window doesn't have
    /// focus. See `install_local_fn_key_monitor` for the complementary half.
    fn install_global_fn_key_monitor<R: tauri::Runtime>(
        app: tauri::AppHandle<R>,
        previous_flags: Rc<Cell<NSEventModifierFlags>>,
        gesture_state: Arc<StdMutex<GestureState>>,
    ) {
        let handler: RcBlock<dyn Fn(NonNull<NSEvent>)> = RcBlock::new(move |event: NonNull<NSEvent>| {
            handle_flags_changed_event(&app, &previous_flags, &gesture_state, event);
        });

        // SAFETY: `addGlobalMonitorForEventsMatchingMask:handler:` is a
        // plain AppKit API that copies the block internally; passing a
        // well-formed block by reference is the documented, safe usage this
        // binding exposes (it isn't marked `unsafe fn`, unlike the local
        // variant which hands back an event the caller must manage).
        let monitor: Option<Retained<AnyObject>> =
            NSEvent::addGlobalMonitorForEventsMatchingMask_handler(NSEventMask::FlagsChanged, &handler);

        match monitor {
            Some(monitor) => {
                log::info!("fn_key: global Fn-key monitor installed");
                // This monitor (and the block backing it) is meant to live
                // for the whole process lifetime, exactly like the tray icon
                // or the global-shortcut registration — there's no shutdown
                // path that calls `removeMonitor:`, so intentionally leak
                // both the handle and the block rather than invent a static
                // to hold a non-`Send` `Retained<AnyObject>`/`RcBlock` for no
                // real benefit.
                std::mem::forget(monitor);
            }
            None => {
                log::warn!(
                    "fn_key: addGlobalMonitorForEventsMatchingMask:handler: returned no monitor \
                     handle — the Fn-key trigger will not fire while some other app has focus \
                     (this is the documented failure mode when Accessibility permission is \
                     missing, matching the warning above if it was logged)"
                );
            }
        }

        std::mem::forget(handler);
    }

    /// Installs a process-lifetime **local** monitor for `.flagsChanged`
    /// events — the complement to `install_global_fn_key_monitor`: this one
    /// receives Fn-key presses only while *this* app (Daimon) itself is key/
    /// active, which a global monitor never does. Daimon's own panel
    /// auto-expands and can grab keyboard focus the instant a recording
    /// starts, so without this monitor a Fn press meant to stop an
    /// already-running (e.g. double-tap-locked) recording could land on
    /// Daimon's own now-focused window and never reach a global-only
    /// listener at all.
    ///
    /// A local monitor's handler is required to return the event it was
    /// given (or null to swallow it) so the event keeps propagating
    /// normally afterward; this handler only observes the event to drive
    /// the gesture state machine and always hands it straight back
    /// unmodified — it never consumes it.
    fn install_local_fn_key_monitor<R: tauri::Runtime>(
        app: tauri::AppHandle<R>,
        previous_flags: Rc<Cell<NSEventModifierFlags>>,
        gesture_state: Arc<StdMutex<GestureState>>,
    ) {
        let handler: RcBlock<dyn Fn(NonNull<NSEvent>) -> *mut NSEvent> =
            RcBlock::new(move |event: NonNull<NSEvent>| -> *mut NSEvent {
                handle_flags_changed_event(&app, &previous_flags, &gesture_state, event);
                // Hand the event straight back unmodified — this monitor
                // only observes; it must never swallow the event, since
                // Daimon's own UI (or the rest of AppKit) still needs to
                // see it dispatched normally.
                event.as_ptr()
            });

        // SAFETY: per this binding's own safety note on
        // `addLocalMonitorForEventsMatchingMask:handler:`, the block's
        // return must be a valid pointer or null — `event.as_ptr()` above is
        // exactly the (already valid, AppKit-owned) pointer this handler was
        // called with, so it trivially satisfies that.
        let monitor: Option<Retained<AnyObject>> =
            unsafe { NSEvent::addLocalMonitorForEventsMatchingMask_handler(NSEventMask::FlagsChanged, &handler) };

        match monitor {
            Some(monitor) => {
                log::info!("fn_key: local Fn-key monitor installed");
                // Same process-lifetime leak rationale as the global
                // monitor above — no shutdown path calls `removeMonitor:`.
                std::mem::forget(monitor);
            }
            None => {
                log::warn!(
                    "fn_key: addLocalMonitorForEventsMatchingMask:handler: returned no monitor \
                     handle — the Fn-key trigger will not fire while Daimon's own window has \
                     focus"
                );
            }
        }

        std::mem::forget(handler);
    }

    /// Installs both the global and local `.flagsChanged` monitors and
    /// drives the Fn-key gesture state machine (see the top-level module
    /// doc and `on_fn_key_edge`) off them — replacing the simple
    /// rising-edge-only toggle this module used to implement.
    ///
    /// Call once, after the real `AppHandle` exists (mirrors the timing
    /// `lib.rs::run()` already uses for the automation scheduler loop).
    pub fn install_fn_key_monitors<R: tauri::Runtime>(app: tauri::AppHandle<R>) {
        if !accessibility_trusted() {
            log::warn!(
                "fn_key: Accessibility permission is not granted for this process — the Fn-key \
                 dictation trigger will not fire until it is. Grant it in System Settings -> \
                 Privacy & Security -> Accessibility, then restart Daimon. (The existing \
                 Cmd+Shift+D hotkey is unaffected by this.)"
            );
        }

        // Shared by *both* monitors below — deliberately, not one pair per
        // monitor. See this module's top-level doc comment for why: a
        // global and a local monitor are mutually exclusive per physical
        // event (each `.flagsChanged` event is either directed at this app
        // or at another one, never both), so they're complementary halves
        // of one continuous observation, not two independent streams — the
        // edge-detection state and the gesture state machine both need to
        // see that one continuous stream to resolve a gesture correctly
        // across a focus change mid-gesture.
        //
        // `previous_flags` is `Rc<Cell<_>>` rather than `Arc<Mutex<_>>`:
        // both monitors' handlers are `.flagsChanged` callbacks delivered
        // serially on AppKit's run loop (in practice the main thread) —
        // never concurrently with each other — so no real cross-thread
        // synchronization is needed for this one value; see
        // `voice.rs`'s `spawn_recording_thread` doc comment for the same
        // kind of "what actually needs Send/thread-safety here" reasoning
        // applied to a different capture.
        //
        // The gesture state itself is a different story: it's read and
        // mutated from these callbacks *and* the delayed double-tap-window
        // timer task spawned from `perform_action`, which genuinely can run
        // concurrently with the next NSEvent callback — hence the real
        // `Mutex`, not a `Cell`, guarding `GestureState`.
        let previous_flags = Rc::new(Cell::new(NSEventModifierFlags::empty()));
        let gesture_state = Arc::new(StdMutex::new(GestureState::new()));

        install_global_fn_key_monitor(app.clone(), previous_flags.clone(), gesture_state.clone());
        install_local_fn_key_monitor(app, previous_flags, gesture_state);
    }
}

#[cfg(target_os = "macos")]
pub use imp::{accessibility_trusted, install_fn_key_monitors};

// Daimon only ships for macOS today (see CLAUDE.md's project status), so
// these stubs exist purely so the crate still compiles elsewhere — not as a
// real cross-platform implementation. `accessibility_trusted` returning
// `false` here is an honest "not applicable"/"not trusted", not a bug to fix.
#[cfg(not(target_os = "macos"))]
pub fn accessibility_trusted() -> bool {
    false
}

#[cfg(not(target_os = "macos"))]
pub fn install_fn_key_monitors<R: tauri::Runtime>(app: tauri::AppHandle<R>) {
    let _ = app;
    log::info!("fn_key: the Fn-key monitors are macOS-only and were not installed on this platform");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    /// Doesn't assert a particular value (this sandboxed/CI environment has
    /// no Accessibility trust to grant, and a developer's real machine might
    /// have granted it) — only that the FFI call itself runs to completion
    /// without crashing.
    #[test]
    fn accessibility_trusted_runs_without_crashing() {
        let _ = super::accessibility_trusted();
    }

    // ------------------------------------------------------------------
    // Gesture state machine — pure, synthetic-timestamp tests. `Instant`
    // can't be constructed at an arbitrary point directly, but
    // `Instant::now() - Duration` is well-defined and lets these tests
    // fabricate "N ms ago"/"N ms apart" timestamps without any real
    // sleeping, so the whole suite runs instantly.
    // ------------------------------------------------------------------

    fn ago(ms: u64) -> Instant {
        Instant::now() - Duration::from_millis(ms)
    }

    /// Hold-to-talk: press, hold well past `TAP_MAX_HOLD_MS`, release ->
    /// starts recording on press, stops+transcribes on release, no
    /// scheduled double-tap check at all.
    #[test]
    fn hold_past_threshold_starts_then_stops_on_release() {
        let mut state = GestureState::new();

        let press_at = ago(500);
        let actions = on_fn_key_edge(&mut state, true, press_at);
        assert_eq!(actions, vec![GestureAction::StartRecording]);
        assert!(matches!(state.phase, GesturePhase::HeldWaitingRelease { .. }));

        // Released 400ms later — comfortably past TAP_MAX_HOLD_MS (280ms).
        let release_at = press_at + Duration::from_millis(400);
        let actions = on_fn_key_edge(&mut state, false, release_at);
        assert_eq!(actions, vec![GestureAction::StopAndTranscribe]);
        assert_eq!(state.phase, GesturePhase::Idle);
    }

    /// A quick tap (released well within `TAP_MAX_HOLD_MS`) with no
    /// follow-up press: the release starts a `PendingDoubleTap` wait (not an
    /// immediate stop), and once the timeout guard fires for that exact
    /// generation, it resolves to stop+transcribe as a belated single tap.
    #[test]
    fn quick_tap_with_no_second_press_resolves_via_timeout() {
        let mut state = GestureState::new();

        let press_at = ago(500);
        on_fn_key_edge(&mut state, true, press_at);

        // Released after only 50ms — a real tap, not a hold.
        let release_at = press_at + Duration::from_millis(50);
        let actions = on_fn_key_edge(&mut state, false, release_at);
        let generation = match actions.as_slice() {
            [GestureAction::ScheduleDoubleTapCheck { generation }] => *generation,
            other => panic!("expected a single ScheduleDoubleTapCheck action, got {other:?}"),
        };
        assert!(matches!(state.phase, GesturePhase::PendingDoubleTap { .. }));

        // No second press ever arrives; the timeout fires for the same
        // generation it was scheduled with.
        let actions = on_double_tap_timeout(&mut state, generation);
        assert_eq!(actions, vec![GestureAction::StopAndTranscribe]);
        assert_eq!(state.phase, GesturePhase::Idle);
    }

    /// The core double-tap-to-lock sequence: press, quick release, a second
    /// press arrives comfortably inside `DOUBLE_TAP_WINDOW_MS` -> confirms
    /// the lock (without starting a second recording), and a single
    /// subsequent press stops it.
    #[test]
    fn double_tap_within_window_locks_then_a_press_stops_it() {
        let mut state = GestureState::new();

        let first_press = ago(1000);
        assert_eq!(
            on_fn_key_edge(&mut state, true, first_press),
            vec![GestureAction::StartRecording]
        );

        let first_release = first_press + Duration::from_millis(60);
        assert!(matches!(
            on_fn_key_edge(&mut state, false, first_release).as_slice(),
            [GestureAction::ScheduleDoubleTapCheck { .. }]
        ));

        // Second press arrives 150ms after the first release — well within
        // the 400ms double-tap window.
        let second_press = first_release + Duration::from_millis(150);
        let actions = on_fn_key_edge(&mut state, true, second_press);
        assert_eq!(actions, vec![GestureAction::ConfirmDoubleTapLock]);
        assert_eq!(state.phase, GesturePhase::Locked);

        // The second press's own release is a no-op — the lock is already
        // engaged and doesn't care about this key being physically down.
        let second_release = second_press + Duration::from_millis(30);
        assert_eq!(on_fn_key_edge(&mut state, false, second_release), Vec::new());
        assert_eq!(state.phase, GesturePhase::Locked);

        // A later, independent press stops the locked recording.
        let stop_press = second_release + Duration::from_millis(2000);
        let actions = on_fn_key_edge(&mut state, true, stop_press);
        assert_eq!(actions, vec![GestureAction::StopAndTranscribe]);
        assert_eq!(state.phase, GesturePhase::Idle);
    }

    /// If a double-tap is confirmed before the original tap's timeout ever
    /// fires, that timeout must be a no-op when it eventually does fire —
    /// this is the generation-counter guard against a stale timer acting on
    /// a state that has already moved on.
    #[test]
    fn stale_timeout_after_confirmed_double_tap_is_a_no_op() {
        let mut state = GestureState::new();

        let first_press = ago(1000);
        on_fn_key_edge(&mut state, true, first_press);
        let first_release = first_press + Duration::from_millis(60);
        let generation = match on_fn_key_edge(&mut state, false, first_release).as_slice() {
            [GestureAction::ScheduleDoubleTapCheck { generation }] => *generation,
            other => panic!("expected ScheduleDoubleTapCheck, got {other:?}"),
        };

        let second_press = first_release + Duration::from_millis(100);
        on_fn_key_edge(&mut state, true, second_press);
        assert_eq!(state.phase, GesturePhase::Locked);

        // The stale timeout for the *first* tap's generation fires late,
        // after the lock already engaged — must not touch state.
        let actions = on_double_tap_timeout(&mut state, generation);
        assert_eq!(actions, Vec::new());
        assert_eq!(state.phase, GesturePhase::Locked);
    }

    /// Same stale-timer guard, different trigger: a hold (not a tap)
    /// starting a *new* gesture right after an old `PendingDoubleTap`
    /// resolved would bump the generation again — the old timeout must
    /// still no-op even though the phase briefly cycled back through
    /// `Idle`.
    #[test]
    fn stale_timeout_after_new_gesture_started_is_a_no_op() {
        let mut state = GestureState::new();

        let first_press = ago(1000);
        on_fn_key_edge(&mut state, true, first_press);
        let first_release = first_press + Duration::from_millis(60);
        let stale_generation = match on_fn_key_edge(&mut state, false, first_release).as_slice() {
            [GestureAction::ScheduleDoubleTapCheck { generation }] => *generation,
            other => panic!("expected ScheduleDoubleTapCheck, got {other:?}"),
        };

        // The real timeout fires first (simulated directly), resolving to
        // Idle.
        let actions = on_double_tap_timeout(&mut state, stale_generation);
        assert_eq!(actions, vec![GestureAction::StopAndTranscribe]);
        assert_eq!(state.phase, GesturePhase::Idle);

        // A brand new press starts a fresh gesture.
        let new_press = first_release + Duration::from_millis(500);
        on_fn_key_edge(&mut state, true, new_press);
        assert!(matches!(state.phase, GesturePhase::HeldWaitingRelease { .. }));

        // Calling the timeout again with the old (now stale) generation
        // must not disturb the new, unrelated gesture in progress.
        let actions = on_double_tap_timeout(&mut state, stale_generation);
        assert_eq!(actions, Vec::new());
        assert!(matches!(state.phase, GesturePhase::HeldWaitingRelease { .. }));
    }

    /// A press while `HeldWaitingRelease` (the key already down — shouldn't
    /// happen with real hardware/AppKit, but must not panic if it somehow
    /// does) is a pure no-op: no action, no phase change.
    #[test]
    fn rising_edge_while_already_held_is_a_defensive_no_op() {
        let mut state = GestureState::new();
        let press_at = ago(500);
        on_fn_key_edge(&mut state, true, press_at);
        let phase_before = state.phase;

        let actions = on_fn_key_edge(&mut state, true, press_at + Duration::from_millis(10));
        assert_eq!(actions, Vec::new());
        assert_eq!(state.phase, phase_before);
    }

    /// The race this module's doc calls out explicitly: a rising edge lands
    /// in `PendingDoubleTap` but *after* `DOUBLE_TAP_WINDOW_MS` has already
    /// elapsed (the scheduled timeout just hasn't run yet). This must
    /// resolve the stale pending tap and start a fresh recording, not
    /// misinterpret the press as confirming a too-late double-tap.
    #[test]
    fn late_press_after_double_tap_window_expired_resolves_old_tap_and_starts_new() {
        let mut state = GestureState::new();

        let first_press = ago(2000);
        on_fn_key_edge(&mut state, true, first_press);
        let first_release = first_press + Duration::from_millis(60);
        on_fn_key_edge(&mut state, false, first_release);
        assert!(matches!(state.phase, GesturePhase::PendingDoubleTap { .. }));

        // Arrives 500ms after the release — outside the 400ms window.
        let late_press = first_release + Duration::from_millis(500);
        let actions = on_fn_key_edge(&mut state, true, late_press);
        assert_eq!(
            actions,
            vec![GestureAction::StopAndTranscribe, GestureAction::StartRecording]
        );
        assert!(matches!(state.phase, GesturePhase::HeldWaitingRelease { .. }));
    }

    /// A tap held for exactly `TAP_MAX_HOLD_MS` counts as a hold (the
    /// transition uses `>=`), not a tap — pins down the boundary explicitly
    /// rather than leaving it to whichever comparison happened to be
    /// written.
    #[test]
    fn held_for_exactly_the_threshold_counts_as_a_hold() {
        let mut state = GestureState::new();
        let press_at = ago(1000);
        on_fn_key_edge(&mut state, true, press_at);

        let release_at = press_at + super::TAP_MAX_HOLD;
        let actions = on_fn_key_edge(&mut state, false, release_at);
        assert_eq!(actions, vec![GestureAction::StopAndTranscribe]);
        assert_eq!(state.phase, GesturePhase::Idle);
    }
}
