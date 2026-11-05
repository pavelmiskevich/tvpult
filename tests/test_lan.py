"""#7: phones on the home network sign in with a PIN; the PIN and sessions stay on the computer."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tvpult.auth import (
    COOKIE, LOCK_SECONDS, PER_ADDRESS_FAILS, TOTAL_FAILS_BEFORE_NEW_PIN, LanAuth, SignInError, cookie_token,
    new_pin, set_cookie,
)
from tvpult.server import make_handler


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(tmp_path, clock):
    return LanAuth(tmp_path / "sessions.json", clock=clock, pin="123456")


# --- the PIN and sessions --------------------------------------------------------------------
def test_new_pin_is_six_digits():
    pins = {new_pin() for _ in range(50)}
    assert all(len(p) == 6 and p.isdigit() for p in pins) and len(pins) > 1


def test_sign_in_gives_a_session(auth):
    token = auth.sign_in("192.168.1.30", "123456", "Phone")
    assert len(token) == 64 and auth.valid(token)
    assert not auth.valid("0" * 64) and not auth.valid(None) and not auth.valid("short")


def test_only_hashes_are_stored(auth, tmp_path):
    token = auth.sign_in("192.168.1.30", "123456")
    stored = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert token not in stored and len(json.loads(stored)["sessions"][0]["token_hash"]) == 64


def test_sessions_survive_a_restart_and_expire(tmp_path, clock, auth):
    token = auth.sign_in("192.168.1.30", "123456")
    again = LanAuth(tmp_path / "sessions.json", clock=clock)
    assert again.valid(token)
    clock.t += 31 * 86400
    assert not again.valid(token)
    assert LanAuth(tmp_path / "sessions.json", clock=clock).sessions() == []


@pytest.mark.parametrize("pin", ["000000", "12345", "1234567", "", None, 123456, ["123456"]])
def test_wrong_pins_are_refused(auth, pin):
    with pytest.raises(SignInError) as e:
        auth.sign_in("192.168.1.30", pin)
    assert e.value.status == 401


def test_too_many_wrong_pins_lock_that_address_only(auth, clock):
    for _ in range(PER_ADDRESS_FAILS):
        with pytest.raises(SignInError):
            auth.sign_in("192.168.1.66", "000000")
    with pytest.raises(SignInError) as e:
        auth.sign_in("192.168.1.66", "123456")  # even the right PIN, while locked
    assert e.value.status == 429
    assert auth.valid(auth.sign_in("192.168.1.30", "123456"))  # someone else is fine
    clock.t += LOCK_SECONDS + 1
    assert auth.valid(auth.sign_in("192.168.1.66", "123456"))


def test_too_many_wrong_pins_in_total_make_a_new_pin(auth):
    for i in range(TOTAL_FAILS_BEFORE_NEW_PIN):
        with pytest.raises(SignInError):
            auth.sign_in(f"10.0.{i}.1", "000000")  # many addresses, a few tries each
    assert auth.pin != "123456"
    with pytest.raises(SignInError):
        auth.sign_in("10.9.9.9", "123456")


def test_revoke_and_renew(auth):
    token = auth.sign_in("192.168.1.30", "123456", "Phone")
    sid = auth.sessions()[0]["id"]
    assert sid not in token
    assert auth.revoke(sid) and not auth.valid(token) and not auth.revoke(sid)
    old = auth.pin
    assert auth.renew_pin() != old or auth.pin != old


def test_cookie_helpers():
    assert cookie_token(f"a=1; {COOKIE}=abc; b=2") == "abc"
    assert cookie_token(None) is None and cookie_token("x=1") is None
    c = set_cookie("tok")
    assert "HttpOnly" in c and "SameSite=Strict" in c and "Path=/" in c and c.startswith(f"{COOKIE}=tok")


# --- over HTTP, per listener role --------------------------------------------------------------
class FakeHome:
    config = None
    setup_needed = False

    def status(self, rediscover="none"):
        return []

    def close(self):
        pass


def _server(role, auth):
    handler = make_handler(FakeHome(), {"127.0.0.1"}, role=role, auth=auth, lan_url="http://192.168.1.20:8765/")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _call(url, body=None, headers=None, cookie=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        h["Cookie"] = f"{COOKIE}={cookie}"
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST", headers=h)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            is_json = "json" in r.headers.get("Content-Type", "")
            return r.status, json.loads(r.read() or b"{}") if is_json else {}, r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


X = {"X-Tvpult": "1"}


@pytest.fixture
def lan(auth):
    srv, base = _server("lan", auth)
    yield base, auth
    srv.shutdown()


@pytest.fixture
def local(auth):
    srv, base = _server("local", auth)
    yield base, auth
    srv.shutdown()


def test_lan_page_is_served_but_the_api_needs_a_pin(lan):
    base, _ = lan
    status, _, _ = _call(base + "/")
    assert status == 200
    assert _call(base + "/api/status")[:2] == (401, {"error": "pin_required"})
    assert _call(base + "/api/tv/living/key", {"keys": ["ok"]}, X)[0] == 401
    assert _call(base + "/api/setup/scan", {}, X)[0] == 401
    assert _call(base + "/api/hello", {"client": "0" * 32}, X)[0] == 401
    assert _call(base + "/api/bye", {"client": "0" * 32})[0] == 401


def test_lan_never_shows_the_pin(lan):
    base, _ = lan
    assert _call(base + "/api/lan")[0] == 404
    assert _call(base + "/api/lan/pin", {}, X)[0] == 404
    assert _call(base + "/api/lan/revoke", {"id": "x"}, X)[0] == 404


def test_lan_sign_in_flow(lan):
    base, auth = lan
    assert _call(base + "/api/login", {"pin": "123456"})[0] == 403  # no custom header: a foreign page can't
    assert _call(base + "/api/login", {"pin": "000000"}, X)[0] == 401
    status, data, headers = _call(base + "/api/login", {"pin": "123456"}, X)
    assert (status, data) == (200, {"ok": True})
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    token = cookie_token(cookie.split(";")[0])
    assert _call(base + "/api/status", cookie=token)[0] == 200
    assert _call(base + "/api/hello", {"client": "a" * 32}, X, cookie=token)[0] == 200
    auth.revoke(auth.sessions()[0]["id"])
    assert _call(base + "/api/status", cookie=token)[0] == 401


def test_lan_locks_out_after_wrong_pins(lan):
    base, _ = lan
    for _ in range(PER_ADDRESS_FAILS):
        _call(base + "/api/login", {"pin": "999999"}, X)
    assert _call(base + "/api/login", {"pin": "123456"}, X)[0] == 429


def test_local_sees_pin_qr_and_sessions(local):
    base, auth = local
    status, info, _ = _call(base + "/api/lan")
    assert status == 200 and info["enabled"] and info["pin"] == "123456"
    assert info["url"] == "http://192.168.1.20:8765/" and info["qr"].startswith("data:image/svg+xml")
    assert _call(base + "/api/status")[0] == 200  # no PIN on this computer
    assert _call(base + "/api/login", {"pin": "123456"}, X)[0] == 404  # nothing to sign in to here
    auth.sign_in("192.168.1.30", "123456", "Phone")
    sid = _call(base + "/api/lan")[1]["sessions"][0]["id"]
    assert _call(base + "/api/lan/revoke", {"id": sid}, X)[1] == {"revoked": True}
    assert _call(base + "/api/lan/pin", {}, X)[1]["pin"] == auth.pin


def test_local_without_lan_mode_reports_disabled():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeHome(), {"127.0.0.1"}, role="local"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _call(f"http://127.0.0.1:{srv.server_address[1]}/api/lan")[1] == {"enabled": False}
    finally:
        srv.shutdown()


def test_lan_listener_cannot_be_made_without_pin():
    with pytest.raises(ValueError):
        make_handler(FakeHome(), {"127.0.0.1"}, role="lan", auth=None)


def test_shortcut_with_lan_adds_the_flag(monkeypatch, tmp_path):
    from tvpult import cli

    seen = {}
    monkeypatch.setattr(cli, "shortcut_target", lambda: ("pythonw.exe", "-m tvpult app"))
    monkeypatch.setattr(cli, "shortcut_script", lambda t, a, w, i: seen.setdefault("args", a) and "")

    class Done(Exception):
        pass

    def stop(*a, **k):
        raise Done

    monkeypatch.setattr(cli.subprocess, "run", stop)
    with pytest.raises(Done):
        cli._make_shortcut(lan=True)
    assert seen["args"] == "-m tvpult app --lan"


@pytest.mark.parametrize("ua,label", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
     "Version/18.0 Mobile/15E148 Safari/604.1", "iPhone · Safari"),
    ("Mozilla/5.0 (Linux; Android 14; 23053RN02Y) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36",
     "Android · Chrome"),
    ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 SamsungBrowser/26.0 Chrome/122.0 Mobile Safari/537.36",
     "Android · Samsung Internet"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36 Edg/130.0",
     "Windows · Edge"),
    ("", "Телефон"), (None, "Телефон"),
])
def test_device_label(ua, label):
    from tvpult.auth import device_label

    assert device_label(ua) == label
