import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tvpult.catalog import UnknownKey, normalize_key
from tvpult.config import load_config
from tvpult.devices import DeviceError
from tvpult.server import ApiError, dispatch, host_allowed, make_handler, origin_ok
from tvpult.wol import magic_packet, subnet_broadcast


# --- keys ---------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("ok", "KEY_ENTER"), ("vol+", "KEY_VOLUP"), ("KEY_HOME", "KEY_HOME"),
    ("home", "KEY_HOME"), ("up", "KEY_UP"), ("5", "KEY_5"), ("key_mute", "KEY_MUTE"),
])
def test_normalize_key_accepts_aliases(raw, expected):
    assert normalize_key(raw) == expected


@pytest.mark.parametrize("raw", ["KEY_FACTORY", "KEY_3SPEED", "factory", "KEY_", "rm -rf"])
def test_service_menu_and_junk_keys_are_rejected(raw):
    with pytest.raises(UnknownKey):
        normalize_key(raw)


# --- wake on lan --------------------------------------------------------------
def test_magic_packet_layout():
    pkt = magic_packet("02:00:00:00:55:01")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("020000005501")
    assert pkt[6:] == bytes.fromhex("020000005501") * 16


@pytest.mark.parametrize("mac", ["02-00-00-00-55-01", "020000005501"])
def test_magic_packet_mac_formats(mac):
    assert magic_packet(mac)[6:12] == bytes.fromhex("020000005501")


def test_magic_packet_rejects_bad_mac():
    with pytest.raises(ValueError):
        magic_packet("not-a-mac")


def test_subnet_broadcast():
    assert subnet_broadcast("192.168.1.50") == "192.168.1.255"


# --- config -------------------------------------------------------------------
def test_example_config_loads():
    from tvpult.config import EXAMPLE_CONFIG

    cfg = load_config(EXAMPLE_CONFIG)
    assert set(cfg.tvs) == {"living", "bedroom"}
    assert cfg.tvs["living"].wake_on_lan and not cfg.tvs["bedroom"].wake_on_lan
    assert cfg.tvs["living"].token_file.name == "token-living.txt"
    assert cfg.tvs["bedroom"].assume_apps == {"browser": "org.tizen.browser"}
    assert "bar" in cfg.soundbars and cfg.soundbars["bar"].mac
    assert cfg.quick_apps[:2] == ["youtube", "netflix"]


def test_config_apps_accept_string_or_list(tmp_path):
    p = tmp_path / "d.toml"
    p.write_text('[apps]\nokko = "111"\nkinopoisk = ["222", "333"]\n[ui]\nquick_apps = ["okko"]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.extra_apps == {"okko": ["111"], "kinopoisk": ["222", "333"]}
    assert cfg.quick_apps == ["okko"]


def test_app_catalog_user_ids_first_and_merged():
    from tvpult.catalog import app_catalog

    cat = app_catalog({"browser": ["my.browser"], "myapp": ["111"]})
    assert cat["browser"][0] == "my.browser" and "org.tizen.browser" in cat["browser"]
    assert cat["myapp"] == ["111"]
    assert app_catalog()["browser"][0] == "org.tizen.browser"  # built-ins not mutated


# --- API routing ----------------------------------------------------------------
class FakeTv:
    def __init__(self):
        self.calls = []

    def send_keys(self, keys):
        self.calls.append(("keys", keys))
        return [normalize_key(k) for k in keys]

    def launch(self, app):
        if app == "nope":
            raise DeviceError("unknown app: nope")
        self.calls.append(("launch", app))
        return "111299001912"

    def power_on(self):
        return "wake-on-lan sent"

    def power_off(self):
        return "turned off"

    def send_text(self, text, submit=False):
        self.calls.append(("text", text, submit))
        return len(text)


class FakeBar:
    def __init__(self):
        self.volume, self.muted = 20, False

    def set_volume(self, level):
        self.volume = max(0, min(100, level))
        return self.volume

    def change_volume(self, delta):
        return self.set_volume(self.volume + delta)

    def set_muted(self, muted=None):
        self.muted = (not self.muted) if muted is None else muted
        return self.muted


class FakeHome:
    def __init__(self):
        self.tvs = {"tv_a": FakeTv()}
        self.bars = {"bar": FakeBar()}

    def tv(self, i):
        if i not in self.tvs:
            raise DeviceError(f"no TV '{i}'")
        return self.tvs[i]

    def bar(self, i=None):
        if i not in self.bars:
            raise DeviceError(f"no soundbar '{i}'")
        return self.bars[i]

    def status(self, rediscover="none"):
        return [{"id": "tv_a", "kind": "tv"}]

    def close(self):
        pass


def test_dispatch_keys_and_launch():
    home = FakeHome()
    assert dispatch(home, "POST", "/api/tv/tv_a/key", {"keys": ["ok", "home"]}) == {"sent": ["KEY_ENTER", "KEY_HOME"]}
    assert dispatch(home, "POST", "/api/tv/tv_a/launch", {"app": "youtube"}) == {"launched": "111299001912"}
    assert dispatch(home, "GET", "/api/status", {}) == {"devices": [{"id": "tv_a", "kind": "tv"}], "quick": []}


def test_dispatch_soundbar():
    home = FakeHome()
    assert dispatch(home, "POST", "/api/bar/bar/volume", {"level": 150}) == {"volume": 100}
    assert dispatch(home, "POST", "/api/bar/bar/volume", {"delta": -30}) == {"volume": 70}
    assert dispatch(home, "POST", "/api/bar/bar/mute", {"muted": None}) == {"muted": True}


@pytest.mark.parametrize("path,body,status", [
    ("/api/tv/tv_a/key", {"keys": ["KEY_FACTORY"]}, 400),
    ("/api/tv/tv_a/key", {}, 400),
    ("/api/tv/tv_a/power", {"on": "yes"}, 400),
    ("/api/tv/tv99/key", {"keys": ["ok"]}, 409),
    ("/api/tv/tv_a/launch", {"app": "nope"}, 409),
    ("/api/tv/tv_a/explode", {}, 404),
    ("/api/bar/bar/volume", {}, 400),
    ("/etc/passwd", {}, 404),
])
def test_dispatch_errors(path, body, status):
    with pytest.raises(ApiError) as e:
        dispatch(FakeHome(), "POST", path, body)
    assert e.value.status == status


# --- request guards -------------------------------------------------------------
def test_host_allowed():
    allowed = {"localhost", "127.0.0.1", "100.118.198.62"}
    assert host_allowed("127.0.0.1:8765", allowed)
    assert host_allowed("localhost:8765", allowed)
    assert host_allowed("100.118.198.62:8765", allowed)
    assert not host_allowed("evil.example:8765", allowed)  # DNS rebinding
    assert not host_allowed(None, allowed)


def test_origin_ok():
    assert origin_ok(None, "127.0.0.1:8765")
    assert origin_ok("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert not origin_ok("http://evil.example", "127.0.0.1:8765")


@pytest.fixture
def live_server():
    home = FakeHome()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(home, {"127.0.0.1", "localhost"}))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", home
    srv.shutdown()


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_live_post_requires_custom_header(live_server):
    base, home = live_server
    status, _ = _post(base + "/api/tv/tv_a/key", {"keys": ["ok"]})
    assert status == 403
    assert home.tvs["tv_a"].calls == []


def test_live_post_rejects_foreign_origin(live_server):
    base, home = live_server
    status, _ = _post(base + "/api/tv/tv_a/key", {"keys": ["ok"]},
                      {"X-Tvpult": "1", "Origin": "http://evil.example"})
    assert status == 403
    assert home.tvs["tv_a"].calls == []


def test_live_post_ok_and_page_served(live_server):
    base, home = live_server
    status, data = _post(base + "/api/tv/tv_a/key", {"keys": ["ok"]}, {"X-Tvpult": "1"})
    assert (status, data) == (200, {"sent": ["KEY_ENTER"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + "/", timeout=5) as r:
        assert r.status == 200 and b"<title>" in r.read()


# --- per-TV app resolution ----------------------------------------------------
def _tv_with_rest(answers, extra=None):
    from tvpult.config import TvConfig
    from tvpult.devices import SamsungTv

    tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0"), extra)
    tv._rest = lambda path, timeout=3.0: answers.get(path.removeprefix("applications/"))
    return tv


def test_installed_apps_picks_first_id_the_tv_knows():
    tv = _tv_with_rest({"3202010022079": {"name": "Internet"}, "111299001912": {"name": "YouTube"}})
    apps = {a["alias"]: a for a in tv.installed_apps()}
    assert apps["browser"]["id"] == "3202010022079" and apps["browser"]["name"] == "Internet"
    assert apps["youtube"]["id"] == "111299001912"
    assert "netflix" not in apps


def test_resolve_app_by_alias_or_tv_name_and_missing():
    tv = _tv_with_rest({"org.tizen.browser": {"name": "Internet"}, "777": {"name": "Кинопоиск"}},
                       {"kinopoisk": ["777"]})
    assert tv.resolve_app("browser") == "org.tizen.browser"
    assert tv.resolve_app("кинопоиск") == "777"
    with pytest.raises(DeviceError):
        tv.resolve_app("netflix")  # known alias, not on this TV
    with pytest.raises(DeviceError):
        tv.resolve_app("whatever")


def test_assume_apps_fill_in_what_rest_cannot_see():
    from tvpult.config import TvConfig
    from tvpult.devices import SamsungTv

    tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0", assume_apps={"browser": "org.tizen.browser"}))
    tv._rest = lambda path, timeout=3.0: None  # REST knows nothing
    apps = {a["alias"]: a["id"] for a in tv.installed_apps()}
    assert apps == {"browser": "org.tizen.browser"}
    assert tv.resolve_app("browser") == "org.tizen.browser"


def test_dispatch_text():
    home = FakeHome()
    assert dispatch(home, "POST", "/api/tv/tv_a/text", {"text": "абв", "submit": True}) == {"typed": 3}
    assert home.tvs["tv_a"].calls[-1] == ("text", "абв", True)
    with pytest.raises(ApiError) as e:
        dispatch(home, "POST", "/api/tv/tv_a/text", {"text": ""})
    assert e.value.status == 400


def test_network_label():
    from tvpult.config import TvConfig
    from tvpult.devices import SamsungTv

    tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0"), None, {"02:00:00:00:a9:21": "HomeWiFi"})
    assert tv.network_label({"networkType": "wireless", "ssid": "02:00:00:00:A9:21"}) == "Wi-Fi HomeWiFi"
    assert tv.network_label({"networkType": "wireless", "ssid": "aa:bb:cc:dd:ee:ff"}) == "Wi-Fi aa:bb:cc:dd:ee:ff"
    assert tv.network_label({"networkType": "wired"}) == "Кабель"


def test_send_text_validates_length():
    from tvpult.config import TvConfig
    from tvpult.devices import SamsungTv

    tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0"))
    with pytest.raises(DeviceError):
        tv.send_text("")
    with pytest.raises(DeviceError):
        tv.send_text("x" * 300)


def test_warm_up_only_when_keep_alive(tmp_path, monkeypatch):
    import tvpult.config as config_mod
    from tvpult.config import TvConfig
    from tvpult.devices import SamsungTv

    config_mod.use_paths(data_dir=tmp_path)
    (tmp_path / "token-t.txt").write_text("x")
    opened = []
    for keep in (False, True):
        tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0"), keep_alive=keep)
        tv._remote = lambda: opened.append(keep) or type("R", (), {"open": lambda self: None})()
        tv.warm_up()
    import time

    time.sleep(0.2)
    assert opened == [True]  # one-shot CLI commands never open the remote channel
