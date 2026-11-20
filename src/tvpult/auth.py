"""PIN sign-in for phones on the home network (`serve --lan`).

The remote normally listens only on this computer. With --lan it also listens on the LAN address, and
there every API call needs a session: a phone enters a 6-digit PIN once (shown only on the computer itself)
and gets a random session token in an HttpOnly cookie. Only SHA-256 hashes of tokens are stored on disk,
so the sessions file alone can't be used to sign in. Sessions can be revoked from the computer.

Brute force: 5 wrong PINs from one address lock that address for 10 minutes. A device on the Wi-Fi isn't
limited to one address, so there is an overall limit too: 20 wrong PINs from all addresses together within an
hour pause sign-in for everyone until someone presses "New PIN" on the computer, whose page says why. So no
more than 20 guesses are ever checked against one PIN (a 1 in 50,000 chance), however many addresses they
come from.
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

from .errors import TvpultError

log = logging.getLogger(__name__)

COOKIE = "tvpult_session"
SESSION_DAYS = 30
PER_ADDRESS_FAILS = 5
LOCK_SECONDS = 600
TOTAL_FAILS = 20  # wrong PINs from all addresses together …
TOTAL_WINDOW = 3600  # … within this time pause sign-in until a new PIN is made on the computer


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


class SignInError(TvpultError):
    def __init__(self, status: int, code: str):
        super().__init__(code=code)
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
        self._fails: dict[str, list[float]] = {}  # address -> times of wrong PINs (last LOCK_SECONDS)
        self._all_fails: list[float] = []  # times of wrong PINs from every address (last TOTAL_WINDOW)
        self.paused = False  # too many wrong PINs: nobody signs in until renew_pin()
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
    def _forget_old(self, now: float) -> None:
        """Failures age out; addresses without recent failures and expired sessions are dropped."""
        self._fails = {a: kept for a, times in self._fails.items() if (kept := [t for t in times
                                                                                  if now - t < LOCK_SECONDS])}
        self._all_fails = [t for t in self._all_fails if now - t < TOTAL_WINDOW]
        cutoff = now - SESSION_DAYS * 86400
        self._sessions = [s for s in self._sessions if s.created >= cutoff]

    def sign_in(self, address: str, pin: object, label: str = "") -> str:
        """Check the PIN; return a new session token, or raise SignInError (401 wrong, 429 locked or paused)."""
        now = self._clock()
        with self._lock:
            self._forget_old(now)
            if self.paused:  # even the right PIN: otherwise the pause wouldn't stop a guesser
                raise SignInError(429, "pin_paused")
            if len(self._fails.get(address, [])) >= PER_ADDRESS_FAILS:
                raise SignInError(429, "pin_locked")
            if not isinstance(pin, str) or not hmac.compare_digest(pin.strip().encode(), self.pin.encode()):
                self._fails.setdefault(address, []).append(now)
                self._all_fails.append(now)
                if len(self._all_fails) >= TOTAL_FAILS:
                    self.paused = True
                    log.warning("%d wrong PINs within %d minutes — sign-in paused until a new PIN is made "
                                "on the computer", len(self._all_fails), TOTAL_WINDOW // 60)
                raise SignInError(401, "wrong_pin")
            self._fails.pop(address, None)
            self._all_fails.clear()  # the right PIN: earlier typos don't count towards the pause any more
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
            self._forget_old(self._clock())
            return [{"id": s.id, "created": s.created, "label": s.label} for s in self._sessions]

    def wrong_pins(self) -> int:
        """Wrong PINs from all addresses within the last TOTAL_WINDOW (shown on the computer)."""
        with self._lock:
            self._forget_old(self._clock())
            return len(self._all_fails)

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            before = len(self._sessions)
            self._sessions = [s for s in self._sessions if s.id != session_id]
            if len(self._sessions) == before:
                return False
            self._save()
            return True

    def renew_pin(self) -> str:
        """A new PIN (pressed on the computer); also ends a pause after too many wrong PINs."""
        with self._lock:
            self.pin = new_pin()
            self._all_fails.clear()
            self.paused = False
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
