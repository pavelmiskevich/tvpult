"""#4: settings page — editing devices.toml safely, forgetting and re-pairing TVs, the quick-launch row, the API."""

import threading

import pytest

from tvpult import config
from tvpult.config import (
    Config,
    SoundbarConfig,
    TvConfig,
    load_config,
    remove_device,
    set_quick_apps,
    update_device,
)
from tvpult.devices import DeviceError, Home, SamsungTv
from tvpult.server import ApiError, dispatch

TV_A = "02:00:00:00:55:01"

CONFIG = """\
# my notes — keep me
[tv.living]
name = "Living room"
host = "192.168.1.50"
mac = "02:00:00:00:55:01"     # Wi-Fi MAC
wake_on_lan = true

# the old one in the bedroom
[tv.bedroom]
name = "Bedroom"
host = "192.168.1.51"
assume_apps = { browser = "org.tizen.browser" }

[soundbar.bar]
name = "Soundbar"
host = "192.168.1.60"
uuid = "6f1c2a3b-1d2e-4f50-8a6b-00000000ba01"

[ui]
quick_apps = ["youtube"]   # mine

[apps]
myapp = "123"
"""


@pytest.fixture
def cfg_file():
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _home():
    return Home(load_config(), local_ip_fn=lambda: "192.168.1.20", adapters_fn=list,
                locate_fn=lambda wanted, subnets, hints=None: {})


def _token(tv_id, text="t"):
    f = config.data_dir() / f"token-{tv_id}.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


# --- editing devices.toml ----------------------------------------------------------------------------------
def test_rename_keeps_comments_and_everything_else(cfg_file):
    update_device("tv", "living", name='  Гостиная  "№1" \\ ')
    text = cfg_file.read_text(encoding="utf-8")
    for kept in ("# my notes — keep me", "# Wi-Fi MAC", "# the old one in the bedroom", "# mine"):
        assert kept in text
    cfg = load_config()
    assert cfg.tvs["living"].name == 'Гостиная "№1" \\'
    assert cfg.tvs["bedroom"].assume_apps == {"browser": "org.tizen.browser"}
    assert cfg.quick_apps == ["youtube"] and cfg.extra_apps == {"myapp": ["123"]}
    assert text.replace('Гостиная \\"№1\\" \\\\', "Living room") == CONFIG  # only the name changed


def test_wake_on_lan_switch(cfg_file):
    update_device("tv", "bedroom", wake_on_lan=True)
    assert load_config().tvs["bedroom"].wake_on_lan
    update_device("tv", "bedroom", wake_on_lan=False)
    assert not load_config().tvs["bedroom"].wake_on_lan


def test_rename_a_soundbar(cfg_file):
    update_device("soundbar", "bar", name="Колонка")
    assert load_config().soundbars["bar"].name == "Колонка"


@pytest.mark.parametrize("call", [
    lambda: update_device("tv", "nope", name="X"),
    lambda: update_device("tv", "living", name="   "),
    lambda: update_device("soundbar", "bar", wake_on_lan=True),
    lambda: update_device("speaker", "bar", name="X"),
    lambda: remove_device("tv", "nope"),
    lambda: set_quick_apps(["ok", "not ok"]),
    lambda: set_quick_apps([f"a{i}" for i in range(13)]),
])
def test_bad_edits_leave_the_file_alone(cfg_file, call):
    with pytest.raises((KeyError, ValueError)):
        call()
    assert cfg_file.read_text(encoding="utf-8") == CONFIG


def test_a_result_that_does_not_parse_is_never_written(cfg_file, monkeypatch):
    def broken(text):
        raise ValueError("pretend the new text is broken")

    monkeypatch.setattr(config, "parse_config", broken)
    with pytest.raises(ValueError):
        update_device("tv", "living", name="X")
    assert cfg_file.read_text(encoding="utf-8") == CONFIG
    assert not list(cfg_file.parent.glob("*.tmp"))


def test_remove_takes_only_that_section(cfg_file):
    remove_device("tv", "bedroom")
    text = cfg_file.read_text(encoding="utf-8")
    assert "[tv.bedroom]" not in text and "org.tizen.browser" not in text
    assert "[tv.living]" in text and "# mine" in text and "# Wi-Fi MAC" in text
    # (a comment line *above* a removed section stays: TOML can't tell whose comment it was)
    remove_device("tv", "living")
    remove_device("soundbar", "bar")
    cfg = load_config()
    assert cfg.empty and cfg.quick_apps == ["youtube"]
    assert "[tv]" not in cfg_file.read_text(encoding="utf-8")


def test_quick_apps_order_and_duplicates(cfg_file):
    set_quick_apps(["okko", "youtube", "okko", "my.app"])
    assert load_config().quick_apps == ["okko", "youtube", "my.app"]
    set_quick_apps([])
    assert load_config().quick_apps == []


def test_quick_apps_creates_the_ui_table():
    set_quick_apps(["youtube"])
    assert load_config().quick_apps == ["youtube"]


# --- Home -----------------------------------------------------------------------------------------------------
def test_home_rename_applies_at_once(cfg_file):
    home = _home()
    assert home.update_device("tv", "living", name="Гостиная") == {"id": "living", "name": "Гостиная"}
    assert home.tv("living").cfg.name == "Гостиная" and home.config.tvs["living"].name == "Гостиная"
    assert load_config().tvs["living"].name == "Гостиная"


def test_home_wake_on_lan_needs_a_mac(cfg_file):
    home = _home()
    with pytest.raises(DeviceError, match="MAC"):
        home.update_device("tv", "bedroom", wake_on_lan=True)  # no mac configured for it
    home.update_device("tv", "living", wake_on_lan=False)
    assert not home.tv("living").cfg.wake_on_lan


def test_home_remove_forgets_token_and_address(cfg_file):
    home = _home()
    token = _token("living")
    home.book.hosts["living"] = "192.168.1.50"
    home.book.save()
    assert home.remove_device("tv", "living") == {"removed": "living"}
    assert "living" not in home.tvs and "living" not in home.config.tvs
    assert not token.exists() and "living" not in home.book.hosts
    assert "living" not in load_config().tvs
    with pytest.raises(DeviceError):
        home.tv("living")


def test_home_remove_unknown_device(cfg_file):
    with pytest.raises(DeviceError):
        _home().remove_device("tv", "nope")


def test_home_unpair_deletes_only_the_token(cfg_file):
    home = _home()
    token = _token("living")
    assert home.unpair("living") == {"id": "living", "unpaired": True}
    assert not token.exists() and "living" in load_config().tvs
    assert home.unpair("living") == {"id": "living", "unpaired": False}


def test_repair_gets_a_new_token(cfg_file, monkeypatch):
    seen = []

    def fake_pair(self, timeout=45):
        seen.append(self.cfg.token_file.exists())  # the old token must be gone, or the TV just reuses it
        self.cfg.token_file.write_text("new", encoding="utf-8")

    monkeypatch.setattr(SamsungTv, "pair", fake_pair)
    token = _token("living", "old")
    assert _home().repair("living") == {"id": "living", "paired": True}
    assert seen == [False] and token.read_text(encoding="utf-8") == "new"


def test_failed_repair_brings_the_old_token_back(cfg_file, monkeypatch):
    def declined(self, timeout=45):
        raise DeviceError("not allowed on the TV")

    monkeypatch.setattr(SamsungTv, "pair", declined)
    token = _token("living", "old")
    with pytest.raises(DeviceError, match="not allowed"):
        _home().repair("living")
    assert token.read_text(encoding="utf-8") == "old"


def test_no_unpair_while_a_pairing_runs(cfg_file, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_pair(self, timeout=45):
        started.set()
        release.wait(5)

    monkeypatch.setattr(SamsungTv, "pair", slow_pair)
    home = _home()
    t = threading.Thread(target=lambda: home.repair("living"))
    t.start()
    assert started.wait(5)
    with pytest.raises(DeviceError, match="right now"):
        home.unpair("bedroom")
    release.set()
    t.join(5)


def test_search_is_rate_limited(cfg_file):
    calls = []

    def locate(wanted, subnets, hints=None):
        calls.append(1)
        return {}

    home = Home(load_config(), local_ip_fn=lambda: "192.168.1.20", adapters_fn=list, locate_fn=locate)
    assert home.search() == {}
    assert home.search() is None  # right after: no second scan
    assert len(calls) == 1
    assert dispatch(home, "POST", "/api/settings/search", {}) == {"found": {}, "recent": True}


def test_quick_apps_apply_at_once(cfg_file):
    home = _home()
    assert home.set_quick_apps(["okko", "youtube"]) == ["okko", "youtube"]
    assert dispatch(home, "GET", "/api/status", {})["quick"] == ["okko", "youtube"]


# --- the API -----------------------------------------------------------------------------------------------------
def test_api_rename_and_quick(cfg_file):
    home = _home()
    assert dispatch(home, "POST", "/api/settings/device",
                    {"kind": "soundbar", "id": "bar", "name": "Колонка"})["name"] == "Колонка"
    assert dispatch(home, "POST", "/api/settings/quick", {"apps": ["okko"]}) == {"quick": ["okko"]}
    assert dispatch(home, "POST", "/api/settings/remove", {"kind": "tv", "id": "bedroom"}) == {"removed": "bedroom"}
    assert set(load_config().tvs) == {"living"}


@pytest.mark.parametrize("path,body,status", [
    ("device", {}, 400),
    ("device", {"kind": "tv", "id": "living"}, 400),  # nothing to change
    ("device", {"kind": "tv", "id": "living", "name": ""}, 400),
    ("device", {"kind": "tv", "id": "living", "name": "x" * 41}, 400),
    ("device", {"kind": "tv", "id": "living", "name": 5}, 400),
    ("device", {"kind": "tv", "id": "living", "wake_on_lan": "yes"}, 400),
    ("device", {"kind": "soundbar", "id": "bar", "wake_on_lan": True}, 400),
    ("device", {"kind": "tv", "id": "../etc", "name": "X"}, 400),
    ("device", {"kind": "tv", "id": "nope", "name": "X"}, 409),
    ("remove", {"kind": "lamp", "id": "living"}, 400),
    ("unpair", {"kind": "soundbar", "id": "bar"}, 400),
    ("pair", {"kind": "soundbar", "id": "bar"}, 400),
    ("quick", {"apps": "youtube"}, 400),
    ("quick", {"apps": ["you tube"]}, 400),
    ("quick", {"apps": [1]}, 400),
    ("quick", {"apps": [f"a{i}" for i in range(13)]}, 400),
    ("nothing", {}, 404),
])
def test_api_validation(cfg_file, path, body, status):
    with pytest.raises(ApiError) as e:
        dispatch(_home(), "POST", f"/api/settings/{path}", body)
    assert e.value.status == status
    assert cfg_file.read_text(encoding="utf-8") == CONFIG


def test_settings_need_post():
    with pytest.raises(ApiError) as e:
        dispatch(Home(Config()), "GET", "/api/settings/quick", {})
    assert e.value.status == 404


def test_home_without_file_rename_fails_cleanly():
    home = Home(Config(tvs={"x": TvConfig("x", "X", "192.168.1.50", TV_A)},
                       soundbars={"b": SoundbarConfig("b", "B", "192.168.1.60", "u")}))
    with pytest.raises(DeviceError, match="not changed"):
        home.update_device("tv", "x", name="Y")  # nothing on disk to edit
    assert home.tv("x").cfg.name == "X"
