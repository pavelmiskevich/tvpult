"""#32: the HTTP server can't be framed, drops silent clients, keeps its errors to itself, caps key lists."""

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tvpult import server
from tvpult.devices import Home
from tvpult.server import MAX_KEYS, ApiError, dispatch, make_handler

X = {"X-Tvpult": "1", "Content-Type": "application/json"}


class FakeHome:
    config = None
    setup_needed = False

    def status(self, rediscover="none"):
        raise RuntimeError("secret detail C:\\Users\\someone\\token-living.txt")

    def close(self):
        pass


@pytest.fixture
def base():
    handler = make_handler(FakeHome(), {"127.0.0.1"})
    handler.timeout = 1  # REQUEST_TIMEOUT, shortened for the test
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", srv.server_address[1]
    srv.shutdown()


def _get(url, body=None):
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers=X,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


# --- 1, 2: framing and the content policy -------------------------------------------------------------
def test_the_page_cannot_be_framed_and_loads_nothing_foreign(base):
    status, html, headers = _get(base[0] + "/")
    assert status == 200 and headers["X-Frame-Options"] == "DENY"
    csp = headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp and "form-action 'none'" in csp
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">'.encode() in html
    assert html.count(b"<script") == 1  # one script, and it is the one with the nonce
    assert _get(base[0] + "/")[2]["Content-Security-Policy"] != csp  # a fresh nonce each time


def test_api_answers_cannot_be_framed_either(base):
    for status, _, headers in (_get(base[0] + "/api/nothing"), _get(base[0] + "/api/status")):
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_the_page_needs_nothing_the_policy_forbids():
    page = (server.resources.files("tvpult") / "static" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+=", page)  # no inline event handlers (script-src has only the nonce)
    assert not re.search(r"""(src|href)=["']https?:""", page)  # nothing loaded from elsewhere


# --- 3: a silent client is dropped ------------------------------------------------------------------------
def test_a_body_that_never_comes_doesnt_hold_a_thread(base):
    _, port = base
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.sendall(b"POST /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 500\r\n\r\n{")
        s.settimeout(5)
        started = time.monotonic()
        assert s.recv(100) == b""  # the server hangs up …
        assert time.monotonic() - started < 4  # … after its timeout, not never
    assert _get(base[0] + "/")[0] == 200  # and still serves others


# --- 4: a crash doesn't tell the caller its details --------------------------------------------------------
def test_an_unexpected_error_answers_a_fixed_text(base, caplog):
    status, body, _ = _get(base[0] + "/api/status")
    data = json.loads(body)
    assert status == 500 and data["code"] == "internal" and "secret" not in body.decode()
    assert "secret detail" in caplog.text  # the details are in the log


# --- 5: key lists and numbers ---------------------------------------------------------------------------
def _home():
    from tvpult.config import Config, TvConfig

    return Home(
        Config(tvs={"living": TvConfig(id="living", name="L", host="192.168.1.50")}),
        local_ip_fn=lambda: None,
        adapters_fn=list,
    )


def test_too_many_keys_are_refused():
    with pytest.raises(ApiError) as e:
        dispatch(_home(), "POST", "/api/tv/living/key", {"keys": ["ok"] * (MAX_KEYS + 1)})
    assert e.value.status == 400 and e.value.code == "too_many_keys" and e.value.params == {"max": str(MAX_KEYS)}
    with pytest.raises(ApiError) as e:
        dispatch(_home(), "POST", "/api/tv/living/key", {"keys": "ok"})
    assert e.value.code == "keys_required"


@pytest.mark.parametrize("body", [{"level": True}, {"delta": False}, {"level": 1.5}, {"level": "10"}])
def test_volume_wants_a_whole_number_not_true_or_false(body):
    from tvpult.config import Config, SoundbarConfig

    home = Home(
        Config(
            soundbars={
                "bar": SoundbarConfig(id="bar", name="B", host="192.168.1.60", uuid="6f1c2a3b1d2e4f508a6b00000000ba01")
            }
        ),
        local_ip_fn=lambda: None,
        adapters_fn=list,
    )
    with pytest.raises(ApiError) as e:
        dispatch(home, "POST", "/api/bar/bar/volume", body)
    assert e.value.code == "bad_volume"
