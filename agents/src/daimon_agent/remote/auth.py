"""Who is allowed to drive this machine remotely.

Three mechanisms, and the split between them is the point:

- **Pairing codes** are short, human-typeable, and short-lived. They exist for
  exactly one exchange: proving you are standing in front of the machine (or
  on its LAN) long enough to be handed a real credential. They are never
  written to disk.
- **Tokens** are long, random, and durable. The host stores only their SHA-256,
  so a leaked `tokens.json` reveals nothing usable — the plaintext exists once,
  in the reply to a pairing request, and thereafter only on the device.
- **Tickets** are single-use and valid for seconds. They exist because browsers
  cannot set an `Authorization` header on a WebSocket handshake. The
  alternatives are worse: a cookie is sent cross-origin on WS handshakes and
  would hand us a CSRF surface to then close, and putting the durable token in
  the query string leaks it into URLs and every proxy log between here and the
  device. A ticket in a URL is worthless thirty seconds later.

Tailscale identity is handled in the gateway rather than here, because it is
not a credential this process issues — it is a header the proxy adds, and
trusting it depends on a property of the *bind*, not of any secret.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: No O/0 or I/1: this gets read off one screen and typed into another.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LENGTH = 8

#: Long enough to pair from another room, short enough that a code left on a
#: screen is not a standing invitation.
CODE_TTL_S = 600.0
#: Over a tunnel the endpoint is world-reachable and the code is the weakest
#: link in the whole design, so it gets much less time.
CODE_TTL_TUNNEL_S = 300.0

#: Wrong guesses before the code is burned. A code is one exchange; anything
#: that looks like searching for it means it is already compromised.
MAX_CODE_ATTEMPTS = 5

TICKET_TTL_S = 30.0


def _now() -> float:
    return time.monotonic()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Device:
    """A paired device. Note the absence of the token itself."""

    id: str
    label: str
    token_sha256: str
    created_at: float
    last_seen_at: float | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }


class TokenStore:
    """Paired devices, persisted as hashes.

    Written with restrictive permissions and never logged. The file is small
    and rewritten whole; there is no concurrent writer (one gateway per
    machine) so nothing more elaborate is warranted.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._devices: dict[str, Device] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in raw.get("devices", []):
            try:
                device = Device(
                    id=str(row["id"]),
                    label=str(row.get("label") or "device"),
                    token_sha256=str(row["token_sha256"]),
                    created_at=float(row.get("created_at") or 0.0),
                    last_seen_at=row.get("last_seen_at"),
                )
            except (KeyError, TypeError, ValueError):
                continue  # one corrupt row must not lock everyone out
            self._devices[device.id] = device

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The directory as much as the file: a world-readable parent makes the
        # 0600 on the file itself pointless against anything that can list it.
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        payload = {"devices": [
            {
                "id": d.id, "label": d.label, "token_sha256": d.token_sha256,
                "created_at": d.created_at, "last_seen_at": d.last_seen_at,
            }
            for d in self._devices.values()
        ]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def issue(self, label: str) -> tuple[str, Device]:
        """Mint a token for a newly paired device.

        Returns the plaintext exactly once — it is not stored and cannot be
        recovered. A device that loses it pairs again.
        """
        token = secrets.token_urlsafe(32)
        device = Device(
            id=uuid.uuid4().hex[:12],
            label=(label or "device")[:60],
            token_sha256=hash_token(token),
            created_at=time.time(),
        )
        self._devices[device.id] = device
        self._save()
        return token, device

    def verify(self, token: str) -> Device | None:
        digest = hash_token(token)
        for device in self._devices.values():
            # compare_digest on the hashes: both are fixed-length hex, so this
            # leaks nothing through timing about which device matched.
            if secrets.compare_digest(device.token_sha256, digest):
                device.last_seen_at = time.time()
                return device
        return None

    def revoke(self, device_id: str) -> bool:
        if self._devices.pop(device_id, None) is None:
            return False
        self._save()
        return True

    def devices(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.created_at)

    def touch(self) -> None:
        self._save()


@dataclass
class PairingCode:
    code: str
    expires_at: float
    attempts: int = 0


class Pairing:
    """One pairing code at a time, in memory only."""

    def __init__(self, *, ttl_s: float = CODE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._active: PairingCode | None = None

    def start(self) -> str:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._active = PairingCode(code=code, expires_at=_now() + self.ttl_s)
        return code

    def stop(self) -> None:
        self._active = None

    @property
    def pending(self) -> bool:
        return self._active is not None and _now() < self._active.expires_at

    def redeem(self, attempt: str) -> bool:
        """Check a code and consume it. Single use, whatever the outcome of
        the exchange that follows."""
        active = self._active
        if active is None or _now() >= active.expires_at:
            self._active = None
            return False
        active.attempts += 1
        if active.attempts > MAX_CODE_ATTEMPTS:
            self._active = None
            return False
        # Normalised: this is typed by hand, and case or spacing is not a
        # security property.
        cleaned = attempt.strip().replace("-", "").replace(" ", "").upper()
        if not secrets.compare_digest(cleaned, active.code):
            return False
        self._active = None
        return True


class Tickets:
    """Single-use, seconds-long credentials for the WebSocket handshake."""

    def __init__(self, *, ttl_s: float = TICKET_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._issued: dict[str, tuple[str, float]] = {}  # ticket -> (device id, expiry)

    def issue(self, device_id: str) -> str:
        self._prune()
        ticket = secrets.token_urlsafe(24)
        self._issued[ticket] = (device_id, _now() + self.ttl_s)
        return ticket

    def redeem(self, ticket: str) -> str | None:
        self._prune()
        entry = self._issued.pop(ticket, None)
        if entry is None:
            return None
        device_id, expires_at = entry
        return device_id if _now() < expires_at else None

    def _prune(self) -> None:
        now = _now()
        for ticket in [t for t, (_d, exp) in self._issued.items() if now >= exp]:
            del self._issued[ticket]
