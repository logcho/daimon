"""Web Push, so a parked question can reach a phone that isn't looking.

Without this, "the agent is waiting on you" is only discoverable by opening
the app and noticing a badge — which is the one moment you were not going to
do it. It is the difference between remote control you remember to check and
remote control that reaches you.

Optional by construction. `pywebpush` brings a crypto stack (VAPID signing,
aes128gcm payload encryption) that an install which never leaves the machine
has no use for, so it is an extra and every entry point here degrades to a
no-op when it is missing rather than failing to start the gateway.

Note what is *not* sent: the question text. A push payload passes through a
push service run by Apple or Google, and a plan the agent is asking you to
approve can quote anything in the workspace. The notification says a session
wants you and names it; reading it means opening the app, where the transport
is yours end to end.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # optional extra — see the module docstring
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid01
    from py_vapid.utils import b64urlencode
    from pywebpush import WebPushException, webpush

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by not installing the extra
    AVAILABLE = False
    WebPushException = Exception  # type: ignore[assignment,misc]

#: Push services reject anything long, and we deliberately send almost nothing.
TTL_S = 600

#: The VAPID `sub` claim: who to contact about this application server.
#:
#: It must be a `mailto:` — py_vapid refuses anything else — and the domain has
#: to look routable. `mailto:daimon@localhost` seems reasonable for software
#: that only ever runs on your own machine, and Apple rejects it outright with
#: `403 BadJwtToken`, which surfaces as notifications silently never arriving.
#:
#: Nothing is ever sent to this address; it is an identifier in a signed token.
#: Deliberately not the user's real address — that would hand it to Apple and
#: Google for no benefit. Override with DAIMON_PUSH_SUBJECT if you want a
#: contact that reaches you.
DEFAULT_SUBJECT = "mailto:daimon@example.com"


@dataclass
class Subscription:
    """One browser's push endpoint, as handed over by the Push API."""

    device_id: str
    endpoint: str
    keys: dict[str, str]

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "endpoint": self.endpoint, "keys": self.keys}

    @classmethod
    def from_dict(cls, row: dict) -> "Subscription":
        return cls(
            device_id=str(row.get("device_id") or ""),
            endpoint=str(row["endpoint"]),
            keys=dict(row.get("keys") or {}),
        )


class PushStore:
    """VAPID identity plus the devices that asked to be notified.

    The VAPID key is this gateway's identity to the push services and must be
    stable: regenerating it invalidates every subscription ever handed out, so
    it is written once and reused.
    """

    def __init__(self, state_dir: Path, *, subject: str | None = None) -> None:
        self.state_dir = state_dir
        self.subject = subject or os.environ.get("DAIMON_PUSH_SUBJECT") or DEFAULT_SUBJECT
        self._subs: dict[str, Subscription] = {}
        self._load()

    # --- vapid ------------------------------------------------------------

    @property
    def key_path(self) -> Path:
        return self.state_dir / "vapid_private.pem"

    def _vapid(self) -> Any:
        if not AVAILABLE:
            return None
        if self.key_path.exists():
            return Vapid01.from_file(str(self.key_path))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        vapid = Vapid01()
        vapid.generate_keys()
        vapid.save_key(str(self.key_path))
        self.key_path.chmod(0o600)
        return vapid

    def public_key(self) -> str | None:
        """The applicationServerKey a browser subscribes with."""
        vapid = self._vapid()
        if vapid is None:
            return None
        # The uncompressed P-256 point, base64url — the exact shape
        # `pushManager.subscribe({applicationServerKey})` expects.
        raw = vapid.public_key.public_bytes(
            encoding=Encoding.X962, format=PublicFormat.UncompressedPoint,
        )
        return b64urlencode(raw)

    # --- subscriptions ----------------------------------------------------

    @property
    def _path(self) -> Path:
        return self.state_dir / "push.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in raw.get("subscriptions", []):
            try:
                sub = Subscription.from_dict(row)
            except (KeyError, TypeError):
                continue  # one bad row must not silence every device
            self._subs[sub.endpoint] = sub

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"subscriptions": [s.as_dict() for s in self._subs.values()]}
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._path.chmod(0o600)

    def add(self, sub: Subscription) -> None:
        self._subs[sub.endpoint] = sub
        self._save()

    def remove(self, endpoint: str) -> None:
        if self._subs.pop(endpoint, None) is not None:
            self._save()

    def forget_device(self, device_id: str) -> None:
        """Revoking a device has to take its notifications with it."""
        gone = [e for e, s in self._subs.items() if s.device_id == device_id]
        for endpoint in gone:
            del self._subs[endpoint]
        if gone:
            self._save()

    def subscriptions(self) -> list[Subscription]:
        return list(self._subs.values())

    @property
    def enabled(self) -> bool:
        return AVAILABLE and bool(self._subs)

    # --- sending ----------------------------------------------------------

    def notify(self, title: str, body: str, *, url: str = "/") -> int:
        """Send to every subscribed device. Returns how many were reached.

        Blocking, and called from a thread — pywebpush is synchronous. A device
        that has unsubscribed answers 404 or 410, and is dropped: a push
        endpoint that is gone stays gone, and retrying it forever is how a
        notification backlog builds up.
        """
        if not AVAILABLE:
            return 0
        vapid = self._vapid()
        if vapid is None:
            return 0
        payload = json.dumps({"title": title, "body": body, "url": url})
        sent = 0
        for sub in list(self._subs.values()):
            try:
                webpush(
                    subscription_info={"endpoint": sub.endpoint, "keys": sub.keys},
                    data=payload,
                    vapid_private_key=str(self.key_path),
                    vapid_claims={"sub": self.subject},
                    ttl=TTL_S,
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    self.remove(sub.endpoint)
                else:
                    print(f"[daimon-remote] push failed: {exc}", file=sys.stderr)
            except Exception as exc:  # never let a notification break a turn
                print(f"[daimon-remote] push failed: {exc}", file=sys.stderr)
        return sent
