"""#37: small rough edges of the command line and the shortcut mode, one test (or a written decision) each."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest
import requests

from tvpult import cli, config, discovery
from tvpult.auth import COOKIE, LanAuth
from tvpult.config import Config, TvConfig
from tvpult.devices import DeviceError, Home, SamsungTv
from tvpult.i18n import use_lang
from tvpult.server import Listener, Presence, make_handler, serve


# --- 1. a busy port: one line, not a traceback; no listener left open -------------------------------------------
def test_a_busy_port_says_which_and_suggests_another():
    holder = Listener(("127.0.0.1", 0), BaseHTTPRequestHandler)  # another program on the port
    port = holder.server_address[1]
    try:
        with pytest.raises(DeviceError) as e:
            serve(Home(Config()), ["127.0.0.1"], port)
        assert e.value.code == "port_busy" and e.value.params["port"] == str(port)
        assert f"--port {port + 1}" in str(e.value)
    finally:
        holder.server_close()


def test_a_second_tvpult_cannot_share_the_port():
    first = Listener(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        with pytest.raises(OSError):  # Windows: SO_REUSEADDR would have let it in
            Listener(("127.0.0.1", first.server_address[1]), BaseHTTPRequestHandler)
    finally:
        first.server_close()


def test_when_the_second_listener_fails_the_first_is_closed():
    probe = Listener(("127.0.0.1", 0), BaseHTTPRequestHandler)  # find a free port
    port = probe.server_address[1]
    probe.server_close()
    with pytest.raises(DeviceError):  # the first listener opens, the second can't have the same address
        serve(Home(Config()), ["127.0.0.1", "127.0.0.1"], port)
    Listener(("127.0.0.1", port), BaseHTTPRequestHandler).server_close()  # the first one was let go


def test_cli_busy_port_is_one_line(capsys):
    holder = Listener(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        use_lang("en")
        assert cli.main(["serve", "--port", str(holder.server_address[1])]) == 1
        err = capsys.readouterr().err
        assert "is taken by another program" in err and "Traceback" not in err
    finally:
        holder.server_close()


# --- 2. `app` when one already runs without --lan ----------------------------------------------------------------
@pytest.mark.parametrize(("has_lan", "warned"), [(False, True), (True, False)])
def test_app_lan_on_a_running_remote_without_lan_says_so(monkeypatch, capsys, has_lan, warned):
    opened = []
    monkeypatch.setattr(cli, "_our_server_running", lambda port: True)
    monkeypatch.setattr(cli, "_running_with_lan", lambda port: has_lan)
    monkeypatch.setattr(cli, "_open_remote", opened.append)
    use_lang("en")
    try:
        assert cli.main(["app", "--lan"]) == 0
    finally:
        import logging

        for h in [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]:
            logging.getLogger().removeHandler(h)
            h.close()
    assert opened  # the window opens either way
    assert ("without phone access" in capsys.readouterr().err) == warned


# --- 3. the TV drops between online() and info() ------------------------------------------------------------------
def _tv():
    return SamsungTv(TvConfig(id="living", name="Living room", host="192.168.1.50"))


@pytest.mark.parametrize("action", ["power_on", "power_off"])
def test_a_tv_dropping_mid_way_is_offline_not_a_traceback(action):
    tv = _tv()
    tv.online = lambda: True

    def gone():
        raise requests.ConnectionError("gone")

    tv.info = gone
    with pytest.raises(DeviceError) as e:
        getattr(tv, action)()
    assert e.value.code == "offline"


# --- 4. a key sequence is never sent twice --------------------------------------------------------------------------
def test_a_reconnect_carries_on_from_the_key_that_failed(monkeypatch):
    monkeypatch.setattr("tvpult.devices.time.sleep", lambda s: None)
    tv = _tv()
    tv._require_online = lambda: None
    sent, failed = [], []

    class Remote:
        def send_key(self, key):
            if len(sent) == 2 and not failed:
                failed.append(key)
                raise ConnectionError("socket dropped")
            sent.append(key)

        def close(self):
            pass

    tv._remote = lambda: Remote()
    tv.send_keys(["vol+", "vol+", "vol+"])
    assert len(sent) == 3  # not five


# --- 5. `vol` arguments and which soundbar ------------------------------------------------------------------------
BARS = (
    '[soundbar.a]\nhost = "192.168.1.60"\nuuid = "6f1c2a3b-1d2e-4f50-8a6b-00000000ba01"\n'
    '[soundbar.b]\nhost = "192.168.1.61"\nuuid = "6f1c2a3b-1d2e-4f50-8a6b-00000000ba02"\n'
)


@pytest.mark.parametrize(
    ("args", "text"),
    [
        (["vol", ""], "is not a volume"),
        (["vol", "loud"], '"loud" is not a volume'),
        (["vol", "30"], "say which with --bar (a, b)"),
    ],
)
def test_vol_explains_itself(capsys, args, text):
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BARS, encoding="utf-8")
    use_lang("en")
    assert cli.main(args) == 1
    err = capsys.readouterr().err
    assert text in err and "Traceback" not in err and "None" not in err


def test_no_soundbar_at_all():
    with pytest.raises(DeviceError) as e:
        Home(Config(tvs={"t": TvConfig(id="t", name="T", host="192.168.1.50")})).bar()
    assert e.value.code == "no_soundbars"


# --- 6, 7. phones and shortcut mode -----------------------------------------------------------------------------------
class FakeHome:
    config = None
    setup_needed = False

    def status(self, rediscover="none"):
        return []


def _server(role, auth, presence):
    handler = make_handler(
        FakeHome(), {"127.0.0.1"}, presence, role=role, auth=auth, lan_url="http://192.168.1.20:8765/"
    )
    srv = Listener(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(url, body, cookie=None):
    headers = {"X-Tvpult": "1", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
        return json.loads(r.read())


def test_a_phone_doesnt_keep_shortcut_mode_alive(tmp_path):
    # Decision: shortcut mode is the window on this computer. A phone's page doesn't count for "last page
    # closed", so closing the window stops the server (and the LAN listener) as before.
    auth = LanAuth(tmp_path / "s.json", pin="123456")
    presence = Presence(idle_exit=True)
    srv, base = _server("lan", auth, presence)
    try:
        token = auth.sign_in("192.168.1.30", "123456")
        assert _post(base + "/api/hello", {"client": "a" * 32}, token)["hello"]
        assert presence.count() == 0
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(("now", "moved"), [("192.168.1.20", False), ("10.0.0.5", True)])
def test_the_computer_page_says_when_the_lan_address_changed(tmp_path, monkeypatch, now, moved):
    monkeypatch.setattr(discovery, "default_local_ip", lambda: now)
    srv, base = _server("local", LanAuth(tmp_path / "s.json"), Presence())
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "/api/lan", timeout=5) as r:
            assert json.loads(r.read())["moved"] is moved
    finally:
        srv.shutdown()
        srv.server_close()


# --- 8. Wake-on-LAN with the real mask -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tv_host", "targets"),
    [
        ("192.168.2.50", ("192.168.3.255", "255.255.255.255")),  # in the laptop's /22
        ("10.0.0.50", ("10.0.0.255", "192.168.3.255", "255.255.255.255")),  # elsewhere: taken as a /24
    ],
)
def test_wol_uses_the_networks_real_mask(monkeypatch, tv_host, targets):
    monkeypatch.setattr(discovery, "default_local_ip", lambda: "192.168.0.20")
    monkeypatch.setattr(discovery, "adapter_addresses", lambda: [("192.168.0.20", 22)])
    tv = SamsungTv(TvConfig(id="living", name="L", host=tv_host))
    assert tv.wol_targets() == targets
