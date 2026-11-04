"""#3: first-run setup wizard — writing the config safely, pairing only scanned candidates, the API."""

import threading
import tomllib

import pytest

from tvpult import config
from tvpult.config import Config, TvConfig, add_tv_to_config, load_config, slugify, toml_string
from tvpult.devices import DeviceError, Home, SamsungTv
from tvpult.discovery import TvSighting, normalize_mac
from tvpult.server import ApiError, dispatch

TV_A = "02:00:00:00:55:01"
TV_B = "02:00:00:00:32:01"


# --- ids and TOML ---------------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("Гостиная", "gostinaya"), ("Living room", "living-room"), ("Детская №2", "detskaya-2"),
    ("!!!", "tv"), ("", "tv"), ("Spalnya", "spalnya"),
])
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_slugify_unique_and_short():
    assert slugify("Гостиная", taken={"gostinaya"}) == "gostinaya-2"
    assert slugify("Гостиная", taken={"gostinaya", "gostinaya-2"}) == "gostinaya-3"
    assert len(slugify("x" * 100)) <= 24


@pytest.mark.parametrize("value", ['say "hi"', "back\\slash", "line\nbreak", "tab\there", "bell\x07", "Пульт ✓"])
def test_toml_string_roundtrips(value):
    assert tomllib.loads(f"v = {toml_string(value)}")["v"] == value


# --- writing the config ------------------------------------------------------------------------------
def _tv(tv_id="living", name="Living room", host="192.168.1.50", mac=TV_A, wol=True):
    return TvConfig(id=tv_id, name=name, host=host, mac=mac, wake_on_lan=wol)


def test_add_tv_creates_a_new_config():
    add_tv_to_config(_tv())
    cfg = load_config()
    assert cfg.tvs["living"].host == "192.168.1.50" and cfg.tvs["living"].wake_on_lan
    assert "written by the setup wizard" in config.config_file().read_text(encoding="utf-8")


def test_add_tv_keeps_everything_already_there():
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = ('# my notes — keep me\n[tv.bedroom]\nname = "Bedroom"\nhost = "192.168.1.51"\n'
                '[ui]\nquick_apps = ["youtube"]   # mine\n[apps]\nmyapp = "123"')  # no newline at the end
    path.write_text(original, encoding="utf-8")
    add_tv_to_config(_tv(name='Tricky "name" \\ here'))
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original)
    cfg = load_config()
    assert set(cfg.tvs) == {"bedroom", "living"}
    assert cfg.tvs["living"].name == 'Tricky "name" \\ here'
    assert cfg.quick_apps == ["youtube"] and cfg.extra_apps == {"myapp": ["123"]}


def test_add_tv_refuses_a_bad_id_and_writes_nothing():
    with pytest.raises(ValueError):
        add_tv_to_config(_tv(tv_id="bad id]\n[evil"))
    assert not config.config_file().exists()


def test_add_tv_duplicate_id_restores_the_old_file():
    add_tv_to_config(_tv())
    before = config.config_file().read_bytes()
    with pytest.raises(tomllib.TOMLDecodeError):
        add_tv_to_config(_tv())  # same [tv.living] twice would break the file
    assert config.config_file().read_bytes() == before


# --- the wizard in Home ------------------------------------------------------------------------------
def _home(sightings, tvs=None):
    return Home(Config(tvs=tvs or {}), local_ip_fn=lambda: "192.168.1.20", adapters_fn=list,
                find_tvs_fn=lambda subnets: sightings)


def _sighting(host="192.168.1.50", mac=TV_A, year=2022, confirmed=True, note=""):
    return TvSighting(host, normalize_mac(mac), "QE65", "Samsung", year, confirmed, note)


@pytest.fixture
def paired(monkeypatch):
    """Pretend the TV says yes; record what was paired."""
    calls = []

    def fake_pair(self, timeout=45):
        calls.append((self.cfg.id, self.host))
        self.cfg.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.token_file.write_text("t", encoding="utf-8")

    monkeypatch.setattr(SamsungTv, "pair", fake_pair)
    return calls


def test_setup_needed_only_without_devices():
    assert _home([]).setup_needed
    assert not _home([], {"living": _tv()}).setup_needed


def test_scan_gives_candidate_ids():
    home = _home([_sighting(), _sighting("192.168.1.51", TV_B, 2017)])
    assert [t["cid"] for t in home.setup_scan()] == ["c1", "c2"]


def test_pair_adds_the_tv_after_the_tv_agrees(paired):
    home = _home([_sighting(), _sighting("192.168.1.51", TV_B, 2017)])
    home.setup_scan()
    assert home.setup_pair("c1", "  Гостиная  ") == {"id": "gostinaya", "already": False}
    assert home.setup_pair("c2", "Спальня") == {"id": "spalnya", "already": False}
    assert paired == [("gostinaya", "192.168.1.50"), ("spalnya", "192.168.1.51")]
    cfg = load_config()
    assert cfg.tvs["gostinaya"].name == "Гостиная" and cfg.tvs["gostinaya"].mac == TV_A
    assert cfg.tvs["gostinaya"].wake_on_lan and not cfg.tvs["spalnya"].wake_on_lan  # 2022 vs 2017
    assert set(home.tvs) == {"gostinaya", "spalnya"} and not home.setup_needed
    assert home.book.hosts["gostinaya"] == "192.168.1.50"


def test_pair_twice_is_already_configured(paired):
    home = _home([_sighting()])
    home.setup_scan()
    home.setup_pair("c1", "A")
    assert home.setup_pair("c1", "B") == {"id": "a", "already": True}
    assert len(paired) == 1


def test_pair_same_name_gets_unique_ids(paired):
    home = _home([_sighting(), _sighting("192.168.1.51", TV_B)])
    home.setup_scan()
    assert home.setup_pair("c1", "TV")["id"] == "tv"
    assert home.setup_pair("c2", "TV")["id"] == "tv-2"


def test_empty_name_falls_back_to_the_model(paired):
    home = _home([_sighting()])
    home.setup_scan()
    assert home.setup_pair("c1", "   ")["id"] == "qe65"


def test_pair_refuses_unknown_and_unconfirmed(paired):
    home = _home([_sighting(confirmed=False, note="the MAC it reports differs")])
    home.setup_scan()
    with pytest.raises(DeviceError, match="last search"):
        home.setup_pair("c9", "X")
    with pytest.raises(DeviceError, match="differs"):
        home.setup_pair("c1", "X")
    assert paired == [] and not config.config_file().exists()


def test_declined_on_the_tv_writes_nothing(monkeypatch):
    def declined(self, timeout=45):
        raise DeviceError("not allowed on the TV")

    monkeypatch.setattr(SamsungTv, "pair", declined)
    home = _home([_sighting()])
    home.setup_scan()
    with pytest.raises(DeviceError, match="not allowed"):
        home.setup_pair("c1", "X")
    assert not config.config_file().exists() and not home.tvs


def test_only_one_pairing_at_a_time(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_pair(self, timeout=45):
        started.set()
        release.wait(5)

    monkeypatch.setattr(SamsungTv, "pair", slow_pair)
    home = _home([_sighting(), _sighting("192.168.1.51", TV_B)])
    home.setup_scan()
    t = threading.Thread(target=lambda: home.setup_pair("c1", "A"))
    t.start()
    assert started.wait(5)
    with pytest.raises(DeviceError, match="right now"):
        home.setup_pair("c2", "B")
    release.set()
    t.join(5)


def test_samsung_pair_turns_library_errors_into_device_errors(monkeypatch):
    import samsungtvws

    class Refusing:
        def __init__(self, *a, **k):
            pass

        def open(self):
            raise RuntimeError("ms.channel.unauthorized")

        def close(self):
            pass

    monkeypatch.setattr(samsungtvws, "SamsungTVWS", Refusing)
    tv = SamsungTv(_tv())
    monkeypatch.setattr(tv, "online", lambda: True)
    with pytest.raises(DeviceError, match="not allowed"):
        tv.pair()


# --- the API ---------------------------------------------------------------------------------------------
def test_status_says_when_setup_is_needed():
    assert dispatch(_home([]), "GET", "/api/status", {})["setup"] is True


def test_api_scan_and_pair(paired):
    home = _home([_sighting()])
    tvs = dispatch(home, "POST", "/api/setup/scan", {})["tvs"]
    assert tvs[0]["cid"] == "c1" and tvs[0]["confirmed"]
    assert dispatch(home, "POST", "/api/setup/pair", {"cid": "c1", "name": "Кухня"})["id"] == "kuhnya"


@pytest.mark.parametrize("body,status", [
    ({}, 400), ({"cid": "192.168.1.50"}, 400), ({"cid": "c1", "name": "x" * 41}, 400),
    ({"cid": "c1", "name": 5}, 400), ({"cid": "c7", "name": "X"}, 409),
])
def test_api_pair_validation(body, status):
    home = _home([_sighting()])
    home.setup_scan()
    with pytest.raises(ApiError) as e:
        dispatch(home, "POST", "/api/setup/pair", body)
    assert e.value.status == status
