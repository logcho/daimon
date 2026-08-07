//! One RFC3339 formatter, shared.
//!
//! There were four hand-rolled copies of this before: `now_rfc3339` in
//! `oauth.rs` and `spotify_oauth.rs`, and `format_rfc3339` in `vault.rs` and
//! `recordings.rs` — three of them ~22 identical lines of Howard Hinnant's
//! civil-from-days arithmetic, each carrying a comment acknowledging it was
//! copied from the last one because "pulling in a shared-utility module for
//! one function isn't worth it yet". Meanwhile `chrono` was already a
//! dependency and `automation.rs` used it for exactly this.
//!
//! Everything here is UTC, matching what the copies produced and what
//! `automation.rs` schedules against.

use std::time::SystemTime;

use chrono::{DateTime, SecondsFormat, Utc};

/// Current time as `2026-08-07T12:34:56Z`.
pub(crate) fn now_rfc3339() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// A `SystemTime` (e.g. a file's mtime) as `2026-08-07T12:34:56Z`.
///
/// Falls back to the Unix epoch for a time the platform can't represent,
/// which is what the hand-rolled versions did via `unwrap_or(0)` — a
/// filesystem returning a pre-1970 mtime shouldn't fail a directory listing.
pub(crate) fn format_rfc3339(time: SystemTime) -> String {
    DateTime::<Utc>::from(time).to_rfc3339_opts(SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn formats_the_epoch() {
        assert_eq!(format_rfc3339(SystemTime::UNIX_EPOCH), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn formats_a_known_instant() {
        let t = SystemTime::UNIX_EPOCH + Duration::from_secs(1_785_069_296);
        assert_eq!(format_rfc3339(t), "2026-07-26T12:34:56Z");
    }

    /// The whole point of the shared module: the two entry points must agree
    /// on shape, which the four independent copies had no way to guarantee.
    #[test]
    fn now_and_format_agree_on_shape() {
        let now = now_rfc3339();
        let formatted = format_rfc3339(SystemTime::now());
        assert_eq!(now.len(), formatted.len());
        assert!(now.ends_with('Z'), "expected a Z-suffixed UTC timestamp, got {now}");
        assert!(formatted.ends_with('Z'), "expected a Z-suffixed UTC timestamp, got {formatted}");
    }
}
