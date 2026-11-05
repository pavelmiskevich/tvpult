"""PIN sign-in for phones on the home network (`serve --lan`).

The remote normally listens only on this computer. With --lan it also listens on the LAN address, and
there every API call needs a session: a phone enters a 6-digit PIN once (shown only on the computer itself)
and gets a random session token in an HttpOnly cookie. Only SHA-256 hashes of tokens are stored on disk,
so the sessions file alone can't be used to sign in. Sessions can be revoked from the computer.

Brute force: 5 wrong PINs from one address lock that address for 10 minutes; 30 wrong PINs in total
replace the PIN with a new one (a guesser can't keep going from many addresses).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

COOKIE = "tvpult_session"
SESSION_DAYS = 30
PER_ADDRESS_FAILS = 5
LOCK_SECONDS = 600
TOTAL_FAILS_BEFORE_NEW_PIN = 30


def new_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def device_label(user_agent: str | None) -> str:
    """A readable name for a session from the browser's User-Agent: 'iPhone · Safari', 'Android · Chrome'."""
    ua = user_agent or ""
    device = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux"))
                   if key in ua), "")
    browser = next((name for key, name in (("YaBrowser", "Yandex"), ("Edg/", "Edge"), ("OPR/", "Opera"),
                                           ("Firefox/", "Firefox"), ("SamsungBrowser", "Samsung Internet"),
                                           ("Chrome/", "Chrome"), ("CriOS", "Chrome"), ("Safari/", "Safari"))
                    if key in ua), "")
    return " · ".join(p for p in (device, browser) if p)  # empty: the page shows "Phone" in its language


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SignInError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    id: str  # short public id (for listing / revoking) — not the token
    token_hash: str
    created: float
    label: str


class LanAuth:
    def __init__(self, store: Path, clock: Callable[[], float] = time.time, pin: str | None = None):
        self.store = store
        self._clock = clock
        self.pin = pin or new_pin()
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}  # address -> times of wrong PINs
        self._total_fails = 0
        self._sessions: list[Session] = self._load()

    # --- persistence --------------------------------------------------------------
    def _load(self) -> list[Session]:
        try:
            raw = json.loads(self.store.read_text(encoding="utf-8"))
            sessions = [Session(str(s["id"]), str(s["token_hash"]), float(s["created"]), str(s.get("label", "")))
                        for s in raw.get("sessions", [])]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return []
        cutoff = self._clock() - SESSION_DAYS * 86400
        return [s for s in sessions if s.created >= cutoff and len(s.token_hash) == 64]

    def _save(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": [s.__dict__ for s in self._sessions]}, indent=2), encoding="utf-8")
        tmp.replace(self.store)

    # --- sign-in ----------------------------------------------------------------------
    def sign_in(self, address: str, pin: object, label: str = "") -> str:
        """Check the PIN; return a new session token, or raise SignInError (401 wrong, 429 locked)."""
        now = self._clock()
        with self._lock:
            recent = [t for t in self._fails.get(address, []) if now - t < LOCK_SECONDS]
            self._fails[address] = recent
            if len(recent) >= PER_ADDRESS_FAILS:
                raise SignInError(429, "too many wrong PINs — try again in 10 minutes")
            if not isinstance(pin, str) or not hmac.compare_digest(pin.strip().encode(), self.pin.encode()):
                recent.append(now)
                self._total_fails += 1
                if self._total_fails >= TOTAL_FAILS_BEFORE_NEW_PIN:
                    self.pin = new_pin()
                    self._total_fails = 0
                    log.warning("too many wrong PINs in total — a new PIN was made (see the computer)")
                raise SignInError(401, "wrong PIN")
            self._fails.pop(address, None)
            token = secrets.token_hex(32)
            self._sessions.append(Session(secrets.token_hex(4), _hash(token), now, label[:60]))
            self._save()
            log.info("a phone signed in (%s)", label[:60] or address)
            return token

    def valid(self, token: str | None) -> bool:
        if not token or len(token) != 64:
            return False
        h = _hash(token)
        cutoff = self._clock() - SESSION_DAYS * 86400
        with self._lock:
            return any(hmac.compare_digest(s.token_hash, h) and s.created >= cutoff for s in self._sessions)

    # --- management (computer only) ---------------------------------------------------
    def sessions(self) -> list[dict[str, object]]:
        with self._lock:
            return [{"id": s.id, "created": s.created, "label": s.label} for s in self._sessions]

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            before = len(self._sessions)
            self._sessions = [s for s in self._sessions if s.id != session_id]
            if len(self._sessions) == before:
                return False
            self._save()
            return True

    def renew_pin(self) -> str:
        with self._lock:
            self.pin = new_pin()
            self._total_fails = 0
            return self.pin


def cookie_token(header: str | None) -> str | None:
    """The session token from a Cookie header, if any."""
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def set_cookie(token: str) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"
