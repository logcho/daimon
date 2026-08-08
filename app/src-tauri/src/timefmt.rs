//! One RFC3339 formatter, shared — ported from `legacy/src-tauri/src/timefmt.rs`.

use std::time::SystemTime;

use chrono::{DateTime, SecondsFormat, Utc};

/// Current time as `2026-08-07T12:34:56Z`.
pub(crate) fn now_rfc3339() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// A `SystemTime` (e.g. a file's mtime) as `2026-08-07T12:34:56Z`.
///
/// Falls back to the Unix epoch for a time the platform can't represent —
/// a filesystem returning a pre-1970 mtime shouldn't fail a directory listing.
pub(crate) fn format_rfc3339(time: SystemTime) -> String {
    DateTime::<Utc>::from(time).to_rfc3339_opts(SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn formats_the_epoch() {
        assert_eq!(
            format_rfc3339(SystemTime::UNIX_EPOCH),
            "1970-01-01T00:00:00Z"
        );
    }

    #[test]
    fn formats_a_known_instant() {
        let t = SystemTime::UNIX_EPOCH + Duration::from_secs(1_785_069_296);
        assert_eq!(format_rfc3339(t), "2026-07-26T12:34:56Z");
    }

    #[test]
    fn now_and_format_agree_on_shape() {
        let now = now_rfc3339();
        let formatted = format_rfc3339(SystemTime::now());
        assert_eq!(now.len(), formatted.len());
        assert!(now.ends_with('Z'));
        assert!(formatted.ends_with('Z'));
    }
}
