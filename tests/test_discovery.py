"""Auto-discovery: identifiers, where to look, scanning, matching, address book, relocation, retries."""

import ipaddress
import json
import struct

import pytest

from tvpult import discovery
from tvpult.config import Config, SoundbarConfig, TvConfig
from tvpult.devices import CastSoundbar, DeviceError, Home, SamsungTv
from tvpult.discovery import AddressBook, Found, Wanted, candidate_subnets, locate, normalize_mac, parse_arp
from tvpult.server import ApiError, Presence, dispatch

TV_A_MAC = "02:00:00:00:55:01"
TV_B_MAC = "02:00:00:00:32:01"
BAR_UUID = "6f1c2a3b-1d2e-4f50-8a6b-00000000ba01"
BAR_MAC = "02:00:00:00:BA:01"
N = ipaddress.IPv4Network



# --- identifiers -----------------------------------------------------------------
@pytest.mark.parametrize("raw", ["02:00:00:00:55:01", "02-00-00-00-55-01", "020000005501", " 02:00:00:00:55:01 "])
def test_normalize_mac_formats(raw):
    assert normalize_mac(raw) == "020000005501"


@pytest.mark.parametrize("raw", ["", None, "00:00:00:00:00:00", "02:00:00", "zz:zz:zz:zz:zz:zz", "02000000550111"])
def test_normalize_mac_rejects_junk(raw):
    assert normalize_mac(raw) == ""


def test_normalize_uuid():
    assert discovery.normalize_uuid("6F1C2A3B-1D2E-4F50-8A6B-00000000BA01") == "6f1c2a3b1d2e4f508a6b00000000ba01"


# --- where to look ------------------------------------------------------------------
def test_candidate_subnets_local_first_then_known_hosts():
    nets = candidate_subnets(["192.168.1.20"], ["192.168.1.50", "192.168.2.40"])
    assert nets == [N("192.168.1.0/24"), N("192.168.2.0/24")]


def test_candidate_subnets_never_public_loopback_linklocal_or_cgnat():
    nets = candidate_subnets(["8.8.8.8", "127.0.0.1", "169.254.3.4", "100.118.198.62", None, "junk"], [])
    assert nets == []


def test_candidate_subnets_extra_only_private_and_small():
    nets = candidate_subnets([], [], ["192.168.5.0/24", "10.0.0.0/8", "1.2.3.0/24", "nonsense", "192.168.6.0/25"])
    assert nets == [N("192.168.5.0/24"), N("192.168.6.0/25")]


def test_candidate_subnets_capped():
    hosts = [f"192.168.{i}.1" for i in range(10)]
    assert len(candidate_subnets([], hosts)) == discovery.MAX_SUBNETS


def test_default_local_ip_is_private_or_none():
    ip = discovery.default_local_ip()
    assert ip is None or ipaddress.IPv4Address(ip)


# --- scanning -----------------------------------------------------------------------------
def test_scan_only_probes_given_subnets_and_ports():
    probed = []

    def fake_connect(host, port):
        probed.append((host, port))
        return (host, port) in {("192.168.2.50", 8001), ("192.168.2.60", 8009)}

    hits = discovery.scan([N("192.168.2.0/24")], (8001, 8009), connect=fake_connect, workers=8)
    assert sorted(hits) == [("192.168.2.50", 8001), ("192.168.2.60", 8009)]
    assert len(probed) == 254 * 2
    assert all(h.startswith("192.168.2.") and p in (8001, 8009) for h, p in probed)
    assert ("192.168.2.0", 8001) not in probed and ("192.168.2.255", 8001) not in probed


def test_scan_nothing_to_do():
    assert discovery.scan([], (8001,)) == []


# --- ARP ----------------------------------------------------------------------------------------
WINDOWS_ARP = """
Interface: 192.168.1.20 --- 0x9
  Internet Address      Physical Address      Type
  192.168.1.1           02-00-00-00-a9-20     dynamic
  192.168.1.60          02-00-00-00-ba-01     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""
LINUX_ARP = """
? (192.168.2.60) at 02:00:00:00:ba:01 [ether] on wlan0
router.lan (192.168.2.1) at 02:00:00:00:a9:20 [ether] on wlan0
? (192.168.2.77) at <incomplete> on wlan0
"""


def test_parse_arp_windows():
    t = parse_arp(WINDOWS_ARP)
    assert t["02000000ba01"] == "192.168.1.60"
    assert "ffffffffffff" not in t and "01005e000016" not in t


def test_parse_arp_unix():
    t = parse_arp(LINUX_ARP)
    assert t == {"02000000ba01": "192.168.2.60", "02000000a920": "192.168.2.1"}


# --- matching ------------------------------------------------------------------------------------
WANTED = [Wanted("tv_a", "tv", mac=TV_A_MAC), Wanted("tv_b", "tv", mac=TV_B_MAC),
          Wanted("bar", "cast", mac=BAR_MAC, uuid=BAR_UUID)]


def _honest_arp(tvs, casts):
    """An ARP table that agrees with what the devices say about themselves (mac -> ip)."""
    arp = {normalize_mac(mac): host for host, mac in tvs.items()}
    arp.update({normalize_mac(BAR_MAC): host for host, u in casts.items() if u == BAR_UUID})
    return arp


def _locate(open_ports, tvs, casts, arp=None, hints=None, slow=()):
    table = _honest_arp(tvs, casts) if arp is None else arp
    return locate(WANTED, [N("192.168.2.0/24")], hints=hints,
                  scan_fn=lambda subnets, ports: open_ports,
                  slow_connect=lambda h, p: (h, p) in slow,
                  identify_tv_fn=lambda h: {"mac": normalize_mac(tvs[h])} if h in tvs else None,
                  identify_cast_fn=lambda h: discovery.normalize_uuid(casts.get(h)) or None,
                  arp_fn=lambda: table)


def test_locate_matches_tvs_by_mac_even_when_addresses_swapped():
    found = _locate([("192.168.2.50", 8001), ("192.168.2.51", 8001)],
                    {"192.168.2.50": TV_B_MAC, "192.168.2.51": TV_A_MAC}, {})
    assert found["tv_a"] == Found("192.168.2.51", "mac")
    assert found["tv_b"] == Found("192.168.2.50", "mac")


def test_locate_ignores_foreign_tv_and_unanswering_hosts():
    found = _locate([("192.168.2.30", 8001), ("192.168.2.31", 8001)],
                    {"192.168.2.30": "11:22:33:44:55:66"}, {})
    assert found == {}


def test_locate_cast_by_uuid():
    found = _locate([("192.168.2.60", 8009)], {}, {"192.168.2.60": BAR_UUID})
    assert found == {"bar": Found("192.168.2.60", "uuid")}


def test_locate_cast_falls_back_to_arp_only_for_a_cast_host():
    arp = {normalize_mac(BAR_MAC): "192.168.2.60"}
    # uuid endpoint silent, but ARP says our MAC is there and 8009 is open there -> accept
    assert _locate([("192.168.2.60", 8009)], {}, {}, arp) == {"bar": Found("192.168.2.60", "arp")}
    # ARP points at a host without Cast -> reject
    assert _locate([("192.168.2.11", 8009)], {}, {}, arp) == {}


def test_locate_other_cast_device_is_not_our_bar():
    found = _locate([("192.168.2.60", 8009)], {}, {"192.168.2.60": "ffffffff-0000-0000-0000-000000000000"})
    assert found == {}


def test_locate_retries_last_address_slowly_when_fast_scan_missed_it():
    # the fast scan missed the sleepy living-room TV at its old address; the slow retry finds it
    found = _locate([], {"192.168.1.50": TV_A_MAC}, {}, hints={"tv_a": "192.168.1.50"},
                    slow={("192.168.1.50", 8001)})
    assert found == {"tv_a": Found("192.168.1.50", "mac")}


def test_locate_slow_retry_still_checks_identity():
    found = _locate([], {"192.168.1.50": TV_B_MAC}, {}, hints={"tv_a": "192.168.1.50"},
                    slow={("192.168.1.50", 8001)})
    assert found == {"tv_b": Found("192.168.1.50", "mac")}  # it's the bedroom TV now — not the living-room TV


def test_locate_slow_retry_runs_alongside_scan_without_duplicates():
    identified = []

    def ident(h):
        identified.append(h)
        return {"mac": normalize_mac(TV_A_MAC)}

    found = locate(WANTED, [], hints={"tv_a": "192.168.2.51"},
                   scan_fn=lambda s, p: [("192.168.2.51", 8001)],
                   slow_connect=lambda h, p: True,  # the slow retry also sees it
                   identify_tv_fn=ident, identify_cast_fn=lambda h: None,
                   arp_fn=lambda: {normalize_mac(TV_A_MAC): "192.168.2.51"})
    assert found == {"tv_a": Found("192.168.2.51", "mac")}
    assert identified == ["192.168.2.51"]  # asked once, not twice


def test_locate_slow_retry_overlaps_scan_in_time():
    import threading

    started = threading.Event()

    def slow_scan(s, p):
        assert started.wait(2), "slow retry did not start while the scan was running"
        return []

    locate(WANTED, [], hints={"tv_a": "192.168.2.51"}, scan_fn=slow_scan,
           slow_connect=lambda h, p: started.set() or False,
           identify_tv_fn=lambda h: None, identify_cast_fn=lambda h: None, arp_fn=dict)


# --- address book ------------------------------------------------------------------------------
def test_address_book_roundtrip(tmp_path):
    book = AddressBook(tmp_path / "a.json", {"tv_a": "192.168.2.51"})
    book.mark_scanned()
    book.save()
    again = AddressBook.load(tmp_path / "a.json")
    assert again.hosts == {"tv_a": "192.168.2.51"} and again.scanned_at == pytest.approx(book.scanned_at)


@pytest.mark.parametrize("content", ["not json", '{"hosts": {"tv_a": "evil.example"}}', '["list"]', ""])
def test_address_book_rejects_corrupt_or_non_ip(tmp_path, content):
    path = tmp_path / "a.json"
    path.write_text(content, encoding="utf-8")
    assert AddressBook.load(path).hosts == {}


def test_address_book_missing_file(tmp_path):
    assert AddressBook.load(tmp_path / "nope.json").hosts == {}


# --- identity checks on devices ------------------------------------------------------------------
def _tv(mac=TV_A_MAC, host="192.168.1.50", arp_mac=TV_A_MAC, lan_mac=""):
    tv = SamsungTv(TvConfig(id="tv_a", name="Living room", host=host, mac=mac, lan_mac=lan_mac))
    tv._arp_fn = lambda: {normalize_mac(arp_mac): host} if arp_mac else {}
    return tv


def test_tv_identity_ok(monkeypatch):
    tv = _tv()
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    calls = []
    tv.info = lambda: calls.append(1) or {"wifiMac": TV_A_MAC}
    assert tv.online() and tv.online()
    assert len(calls) == 1  # identity cached
    assert not tv.wrong_device


def test_tv_wrong_device_at_address(monkeypatch):
    tv = _tv()
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_B_MAC}  # the bedroom TV now has the living-room TV's old address
    assert not tv.online()
    assert tv.wrong_device
    with pytest.raises(DeviceError, match="another device"):
        tv.send_keys(["ok"])


def test_tv_without_configured_mac_is_not_checked(monkeypatch):
    tv = _tv(mac="")
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_B_MAC}
    assert tv.online()


def test_tv_status_marks_wrong_device(monkeypatch):
    tv = _tv()
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_B_MAC, "PowerState": "on"}
    st = tv.status()
    assert st["online"] is False and st["wrong_device"] is True


def test_set_host_resets_connection_and_caches(monkeypatch):
    tv = _tv()
    closed = []
    tv._ws = type("WS", (), {"close": lambda self: closed.append(1)})()
    tv._apps, tv._identity.host = [{"alias": "x"}], "192.168.1.50"
    tv.set_host("192.168.2.51")
    assert tv.host == "192.168.2.51" and tv._ws is None and closed == [1]
    assert tv._apps is None and tv._identity.host is None


def test_bar_identity(monkeypatch):
    bar = CastSoundbar(SoundbarConfig(id="bar", name="JBL", host="192.168.1.60", uuid=BAR_UUID))
    bar._arp_fn = dict
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    monkeypatch.setattr(discovery, "identify_cast", lambda h, http=None: "0" * 32)
    assert not bar.online() and bar.wrong_device
    monkeypatch.setattr(discovery, "identify_cast", lambda h, http=None: discovery.normalize_uuid(BAR_UUID))
    bar._identity.reset()
    assert bar.online() and not bar.wrong_device


def test_wol_targets_include_tv_and_laptop_networks(monkeypatch):
    tv = _tv(host="192.168.1.50")
    monkeypatch.setattr(discovery, "default_local_ip", lambda: "192.168.2.25")
    assert tv.wol_targets() == ("192.168.1.255", "192.168.2.255", "255.255.255.255")


# --- relocation in Home --------------------------------------------------------------------------
def _config():
    return Config(
        tvs={"tv_a": TvConfig(id="tv_a", name="Living room", host="192.168.1.50", mac=TV_A_MAC),
             "tv_b": TvConfig(id="tv_b", name="Bedroom", host="192.168.1.51", mac=TV_B_MAC)},
        soundbars={"bar": SoundbarConfig(id="bar", name="JBL", host="192.168.1.60", uuid=BAR_UUID, mac=BAR_MAC)},
    )


def test_home_uses_remembered_addresses(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"hosts": {"tv_a": "192.168.2.51"}}), encoding="utf-8")
    home = Home(_config(), book_path=tmp_path / "a.json")
    assert home.tv("tv_a").host == "192.168.2.51"
    assert home.tv("tv_b").host == "192.168.1.51"  # not remembered: from devices.toml


def test_relocate_moves_devices_saves_and_rate_limits(tmp_path):
    calls = []

    def fake_locate(wanted, subnets, hints=None):
        calls.append((list(wanted), list(subnets), hints))
        return {"tv_a": Found("192.168.2.51", "mac"), "bar": Found("192.168.2.60", "uuid")}

    home = Home(_config(), book_path=tmp_path / "a.json", locate_fn=fake_locate,
                local_ip_fn=lambda: "192.168.2.25")
    found = home.relocate()
    assert set(found) == {"tv_a", "bar"}
    assert home.tv("tv_a").host == "192.168.2.51" and home.bar().host == "192.168.2.60"
    assert home.tv("tv_b").host == "192.168.1.51"  # not found (off): left where it was
    wanted, subnets, hints = calls[0]
    assert hints == {"tv_a": "192.168.1.50", "tv_b": "192.168.1.51", "bar": "192.168.1.60"}
    assert {w.id for w in wanted} == {"tv_a", "tv_b", "bar"}
    assert subnets[0] == N("192.168.2.0/24")  # the laptop's network first
    saved = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    assert saved["hosts"] == {"tv_a": "192.168.2.51", "bar": "192.168.2.60"}

    assert home.relocate() == {} and len(calls) == 1  # rate-limited
    home.relocate(force=True)
    assert len(calls) == 2


def test_relocate_rate_limit_shared_through_address_book(tmp_path):
    calls = []

    def fake(w, s, hints=None):
        calls.append(1)
        return {}

    Home(_config(), book_path=tmp_path / "a.json", locate_fn=fake).relocate()
    Home(_config(), book_path=tmp_path / "a.json", locate_fn=fake).relocate()  # another process, a moment later
    assert len(calls) == 1


def test_relocate_unchanged_host_keeps_connection(tmp_path):
    home = Home(_config(), book_path=tmp_path / "a.json",
                locate_fn=lambda w, s, hints=None: {"tv_a": Found("192.168.1.50", "mac")})
    tv = home.tv("tv_a")
    sentinel = object()
    tv._ws = sentinel
    home.relocate()
    assert tv._ws is sentinel


def test_ensure_searches_once_when_unreachable(tmp_path, monkeypatch):
    home = Home(_config(), book_path=tmp_path / "a.json",
                locate_fn=lambda w, s, hints=None: {"tv_a": Found("192.168.2.51", "mac")})
    tv = home.tv("tv_a")
    monkeypatch.setattr(SamsungTv, "online", lambda self: self.host == "192.168.2.51")
    assert home.ensure(tv) and tv.host == "192.168.2.51"


# --- server retries after relocation ---------------------------------------------------------------
class MovingTv:
    """Fails until it has been 'found' at its new address."""

    def __init__(self):
        self.moved = False
        self.sent = []

    def online(self):
        return self.moved

    def send_keys(self, keys):
        if not self.moved:
            raise DeviceError("off or unreachable")
        self.sent.append(keys)
        return keys

    def power_on(self):
        raise DeviceError("cannot")


class MovingHome:
    def __init__(self, finds=True):
        self.t = MovingTv()
        self.finds = finds
        self.relocations = 0

    def tv(self, i):
        if i != "tv_a":
            raise DeviceError("no TV")
        return self.t

    def relocate(self, force=False):
        self.relocations += 1
        if self.finds:
            self.t.moved = True
            return {"tv_a": Found("192.168.2.51", "mac")}
        return {}


def test_dispatch_retries_after_device_found_elsewhere():
    home = MovingHome()
    assert dispatch(home, "POST", "/api/tv/tv_a/key", {"keys": ["ok"]}) == {"sent": ["ok"]}
    assert home.relocations == 1


def test_dispatch_reports_when_not_found():
    home = MovingHome(finds=False)
    with pytest.raises(ApiError) as e:
        dispatch(home, "POST", "/api/tv/tv_a/key", {"keys": ["ok"]})
    assert e.value.status == 409 and home.relocations == 1


def test_dispatch_power_never_searches():
    home = MovingHome()
    with pytest.raises(ApiError):
        dispatch(home, "POST", "/api/tv/tv_a/power", {"on": True})
    assert home.relocations == 0


def test_dispatch_unknown_device_never_searches():
    home = MovingHome()
    with pytest.raises(ApiError) as e:
        dispatch(home, "POST", "/api/tv/tv99/key", {"keys": ["ok"]})
    assert e.value.status == 409 and home.relocations == 0


# --- presence (desktop-shortcut mode) -----------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


CID = "0123456789abcdef0123456789abcdef"


def test_presence_disabled_never_exits():
    c = Clock()
    p = Presence(idle_exit=False, clock=c)
    c.t += 10_000
    assert not p.should_exit()


def test_presence_exits_after_last_page_closes_with_grace():
    c = Clock()
    p = Presence(idle_exit=True, grace=8, clock=c)
    p.hello(CID)
    assert not p.should_exit()
    assert p.bye(CID)
    c.t += 5
    assert not p.should_exit()  # a reload says bye then hello within the grace
    c.t += 4
    assert p.should_exit()


def test_presence_reload_cancels_exit():
    c = Clock()
    p = Presence(idle_exit=True, grace=8, clock=c)
    p.hello(CID)
    p.bye(CID)
    c.t += 1
    p.hello("f" * 32)
    c.t += 60
    assert not p.should_exit()


def test_presence_stale_tab_is_dropped():
    c = Clock()
    p = Presence(idle_exit=True, grace=8, stale=180, clock=c)
    p.hello(CID)
    c.t += 181
    assert not p.should_exit()  # just noticed it's gone: grace starts now
    c.t += 9
    assert p.should_exit()


def test_presence_page_never_opened():
    c = Clock()
    p = Presence(idle_exit=True, startup=120, clock=c)
    c.t += 100
    assert not p.should_exit()
    c.t += 30
    assert p.should_exit()


def test_presence_bye_only_for_known_ids():
    p = Presence(idle_exit=True)
    assert not p.bye(CID)  # never said hello: a foreign page can't stop the server
    assert not p.valid("../etc") and not p.valid("ABC") and not p.valid(None) and p.valid(CID)


# --- icon -------------------------------------------------------------------------------------------------
def test_icon_is_valid_ico(tmp_path):
    from tvpult.icon import write_icon

    data = write_icon(tmp_path / "t.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    w, h, _, _, planes, bpp, size, offset = struct.unpack("<BBBBHHII", data[6:22])
    assert (reserved, kind, count) == (0, 1, 1)
    assert (w, h, planes, bpp) == (64, 64, 1, 32)
    assert offset == 22 and offset + size == len(data)
    hdr = struct.unpack("<IiiHHIIiiII", data[22:62])
    assert hdr[0] == 40 and hdr[1] == 64 and hdr[2] == 128 and hdr[4] == 32
    assert any(data[62 + i * 4 + 3] for i in range(64 * 64))  # something is drawn


# --- desktop shortcut script -----------------------------------------------------------------------------
def test_shortcut_script_quotes_and_cyrillic():
    from pathlib import Path

    from tvpult.cli import SHORTCUT_NAME, shortcut_script

    s = shortcut_script(r"C:\Users\O'Neil\uvw.exe", Path(r"C:\Proj\TV"), Path(r"C:\Proj\TV\.data\tvpult.ico"))
    assert "$ErrorActionPreference = 'Stop'" in s  # a failed Save() must not look like success
    assert f"'{SHORTCUT_NAME}.lnk'" in s and "Пульт" in SHORTCUT_NAME
    assert r"'C:\Users\O''Neil\uvw.exe'" in s  # apostrophe doubled inside a PowerShell literal
    assert r'run --quiet --project "C:\Proj\TV" tvpult app' in s
    assert s.strip().endswith("Write-Output $p")


# --- spoofing: a LAN device claiming to be our TV / soundbar ---------------------------------------------------
IMPOSTOR_MAC = "de:ad:be:ef:00:01"


def test_locate_rejects_impostor_claiming_tv_mac():
    # a compromised plug at .66 answers the TV API with the living-room TV's Wi-Fi MAC; ARP knows its real MAC
    found = _locate([("192.168.2.66", 8001)], {"192.168.2.66": TV_A_MAC}, {},
                    arp={normalize_mac(IMPOSTOR_MAC): "192.168.2.66"})
    assert found == {}


def test_locate_rejects_tv_claim_without_arp_entry():
    found = _locate([("192.168.2.66", 8001)], {"192.168.2.66": TV_A_MAC}, {}, arp={})
    assert found == {}


def test_locate_picks_real_tv_over_impostor():
    found = _locate([("192.168.2.66", 8001), ("192.168.2.51", 8001)],
                    {"192.168.2.66": TV_A_MAC, "192.168.2.51": TV_A_MAC}, {},
                    arp={normalize_mac(IMPOSTOR_MAC): "192.168.2.66", normalize_mac(TV_A_MAC): "192.168.2.51"})
    assert found == {"tv_a": Found("192.168.2.51", "mac")}


def test_locate_rejects_impostor_claiming_cast_uuid():
    found = _locate([("192.168.2.66", 8009)], {}, {"192.168.2.66": BAR_UUID},
                    arp={normalize_mac(IMPOSTOR_MAC): "192.168.2.66"})
    assert found == {}


def test_locate_accepts_cabled_tv_by_lan_mac():
    lan = "aa:bb:cc:00:11:22"
    wanted = [Wanted("tv_a", "tv", mac=TV_A_MAC, lan_mac=lan)]
    found = locate(wanted, [], scan_fn=lambda s, p: [("192.168.2.51", 8001)],
                   slow_connect=lambda h, p: False,
                   identify_tv_fn=lambda h: {"mac": normalize_mac(TV_A_MAC)},  # still reports Wi-Fi MAC
                   identify_cast_fn=lambda h: None,
                   arp_fn=lambda: {normalize_mac(lan): "192.168.2.51"})
    assert found == {"tv_a": Found("192.168.2.51", "mac")}


def test_tv_identity_rejects_impostor_and_never_opens_remote(monkeypatch):
    tv = _tv(arp_mac=IMPOSTOR_MAC)
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_A_MAC}  # the claim is right, the ARP table isn't
    opened = []
    tv._remote = lambda: opened.append(1)

    class NoNetwork:  # pair() builds its own client: it must never reach a real TV from a test
        def __init__(self, *a, **k):
            opened.append(1)
            raise AssertionError("tried to connect")

    import samsungtvws
    monkeypatch.setattr(samsungtvws, "SamsungTVWS", NoNetwork)
    assert not tv.online() and tv.wrong_device
    with pytest.raises(DeviceError):
        tv.send_keys(["ok"])
    with pytest.raises(DeviceError):
        tv.send_text("my wifi password")
    with pytest.raises(DeviceError):
        tv.pair()
    assert opened == []  # the pairing token never went anywhere


def test_tv_identity_rejects_missing_arp_entry(monkeypatch):
    tv = _tv(arp_mac="")
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_A_MAC}
    assert not tv.online()


def test_tv_identity_accepts_cabled_tv(monkeypatch):
    lan = "aa:bb:cc:00:11:22"
    tv = _tv(arp_mac=lan, lan_mac=lan)
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_A_MAC}
    assert tv.online()


def test_status_does_not_warm_up_impostor(monkeypatch):
    tv = _tv(arp_mac=IMPOSTOR_MAC)
    tv.keep_alive = True
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    tv.info = lambda: {"wifiMac": TV_A_MAC, "PowerState": "on"}
    warmed = []
    tv.warm_up = lambda: warmed.append(1)
    st = tv.status()
    assert st["online"] is False and st.get("wrong_device") and warmed == []


def test_bar_with_mac_requires_arp(monkeypatch):
    bar = CastSoundbar(SoundbarConfig(id="bar", name="JBL", host="192.168.1.60", uuid=BAR_UUID, mac=BAR_MAC))
    monkeypatch.setattr("tvpult.devices._port_open", lambda h, p, timeout=1.0: True)
    monkeypatch.setattr(discovery, "identify_cast", lambda h, http=None: discovery.normalize_uuid(BAR_UUID))
    bar._arp_fn = lambda: {normalize_mac(IMPOSTOR_MAC): "192.168.1.60"}
    assert not bar.online() and bar.wrong_device
    bar._identity.reset()
    bar._arp_fn = lambda: {normalize_mac(BAR_MAC): "192.168.1.60"}
    assert bar.online()


def test_arp_mac_lookup():
    table = {normalize_mac(TV_A_MAC): "192.168.1.50"}
    assert discovery.arp_mac("192.168.1.50", lambda: table) == normalize_mac(TV_A_MAC)
    assert discovery.arp_mac("192.168.1.99", lambda: table) == ""
    assert discovery.mac_confirmed("192.168.1.50", TV_A_MAC, lambda: table)
    assert not discovery.mac_confirmed("192.168.1.50", "", lambda: table)
