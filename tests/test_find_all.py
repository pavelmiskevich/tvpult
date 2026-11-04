"""#2: find every Samsung TV on the network, with the real network mask and the ARP rule."""

import ipaddress
from types import SimpleNamespace

from tvpult import discovery
from tvpult.config import Config, TvConfig
from tvpult.devices import Home
from tvpult.discovery import TvSighting, candidate_subnets, find_tvs, local_network, model_year, normalize_mac

N = ipaddress.IPv4Network
TV_A = "02:00:00:00:55:01"
TV_B = "02:00:00:00:32:01"
OTHER = "02:00:00:00:77:01"


# --- real network mask ------------------------------------------------------------------
def test_local_network_uses_the_adapter_mask():
    adapters = [("100.64.0.5", 32), ("192.168.1.20", 23), ("172.29.48.1", 20)]
    assert local_network("192.168.1.20", adapters) == N("192.168.0.0/23")


def test_local_network_caps_huge_networks():
    assert local_network("10.1.2.3", [("10.1.2.3", 8)]) == N("10.1.0.0/22")


def test_local_network_falls_back_to_24():
    assert local_network("192.168.5.7", []) == N("192.168.5.0/24")
    assert local_network("192.168.5.7", [("192.168.5.7", 0)]) == N("192.168.5.0/24")


def test_only_the_default_route_adapter_is_scanned():
    # WSL / Hyper-V / VPN adapters are on the machine but never become scan targets
    adapters = [("192.168.1.20", 24), ("172.29.48.1", 20), ("172.22.144.1", 20), ("100.118.0.1", 32)]
    assert candidate_subnets(["192.168.1.20"], [], adapters=adapters) == [N("192.168.1.0/24")]


def test_known_host_inside_the_local_network_is_not_scanned_twice():
    nets = candidate_subnets(["192.168.0.20"], ["192.168.1.50", "192.168.7.9"], adapters=[("192.168.0.20", 23)])
    assert nets == [N("192.168.0.0/23"), N("192.168.7.0/24")]


def test_extra_networks_up_to_the_cap():
    nets = candidate_subnets([], [], ["192.168.4.0/22", "192.168.8.0/21", "8.8.8.0/24"])
    assert nets == [N("192.168.4.0/22")]


def test_read_adapters_keeps_ipv4_only():
    adapters = [SimpleNamespace(ips=[SimpleNamespace(ip="192.168.1.20", network_prefix=24),
                                     SimpleNamespace(ip=("fe80::1", 0, 3), network_prefix=64)])]
    assert discovery.read_adapters(lambda: adapters) == [("192.168.1.20", 24)]


def test_read_adapters_never_raises():
    def broken():
        raise OSError("no adapters for you")

    assert discovery.read_adapters(broken) == []


# --- what a TV says about itself ----------------------------------------------------------
def test_model_year():
    assert model_year("22_PONTUSM_QTV") == 2022
    assert model_year("17_KANTS_FHD") == 2017
    assert model_year("") is None and model_year(None) is None and model_year("PONTUS") is None


# --- the full scan --------------------------------------------------------------------------
def _find(tvs: dict[str, dict], arp: dict[str, str], extra_ports=()):
    ports = [(h, 8001) for h in tvs] + list(extra_ports)
    return find_tvs([N("192.168.1.0/24")],
                    scan_fn=lambda subnets, p: ports,
                    identify_tv_fn=lambda h: tvs.get(h),
                    arp_fn=lambda: {normalize_mac(m): ip for ip, m in arp.items()})


def _ident(mac, model="QE65", year=2022, wired=False, name="TV"):
    return {"mac": normalize_mac(mac), "model": model, "name": name, "year": year, "wired": wired}


def test_finds_every_tv_sorted_and_confirmed():
    tvs = {"192.168.1.51": _ident(TV_B, "UE43", 2017), "192.168.1.50": _ident(TV_A)}
    found = _find(tvs, {"192.168.1.50": TV_A, "192.168.1.51": TV_B})
    assert [t.host for t in found] == ["192.168.1.50", "192.168.1.51"]
    assert all(t.confirmed for t in found)
    assert (found[0].year, found[1].year) == (2022, 2017)


def test_sorted_numerically_not_as_text():
    tvs = {"192.168.1.100": _ident(TV_A), "192.168.1.9": _ident(TV_B)}
    found = _find(tvs, {"192.168.1.100": TV_A, "192.168.1.9": TV_B})
    assert [t.host for t in found] == ["192.168.1.9", "192.168.1.100"]


def test_non_tv_on_port_8001_is_ignored():
    found = _find({"192.168.1.50": _ident(TV_A)}, {"192.168.1.50": TV_A}, extra_ports=[("192.168.1.99", 8001)])
    assert [t.host for t in found] == ["192.168.1.50"]


def test_impostor_is_listed_unconfirmed_with_reason():
    found = _find({"192.168.1.66": _ident(TV_A)}, {"192.168.1.66": OTHER})
    assert len(found) == 1 and not found[0].confirmed and "differs" in found[0].note


def test_no_arp_entry_is_unconfirmed():
    found = _find({"192.168.1.50": _ident(TV_A)}, {})
    assert not found[0].confirmed and "ARP" in found[0].note


def test_cabled_tv_gets_a_helpful_note():
    found = _find({"192.168.1.50": _ident(TV_A, wired=True)}, {"192.168.1.50": OTHER})
    assert not found[0].confirmed and "lan_mac" in found[0].note


def test_nothing_answering_means_no_arp_read():
    called = []
    assert find_tvs([N("192.168.1.0/24")], scan_fn=lambda s, p: [], identify_tv_fn=lambda h: None,
                    arp_fn=lambda: called.append(1) or {}) == []
    assert called == []


def test_find_tvs_scans_only_the_tv_port():
    asked = []
    find_tvs([N("192.168.1.0/24")], scan_fn=lambda s, p: asked.append(tuple(p)) or [],
             identify_tv_fn=lambda h: None, arp_fn=dict)
    assert asked == [(8001,)]


# --- Home and the CLI ---------------------------------------------------------------------------
def test_scan_tvs_marks_configured_and_new():
    sightings = [TvSighting("192.168.1.50", normalize_mac(TV_A), "QE65", "Samsung", 2022, True),
                 TvSighting("192.168.1.51", normalize_mac(TV_B), "UE43", "Samsung", 2017, True)]
    cfg = Config(tvs={"living": TvConfig(id="living", name="L", host="192.168.1.50", mac=TV_A)})
    home = Home(cfg, local_ip_fn=lambda: "192.168.1.20", adapters_fn=lambda: [("192.168.1.20", 24)],
                find_tvs_fn=lambda subnets: sightings)
    out = home.scan_tvs()
    assert [(t["host"], t["configured"]) for t in out] == [("192.168.1.50", "living"), ("192.168.1.51", None)]


def test_scan_uses_the_real_network():
    seen = []
    home = Home(Config(), local_ip_fn=lambda: "192.168.0.20", adapters_fn=lambda: [("192.168.0.20", 23)],
                find_tvs_fn=lambda subnets: seen.append(subnets) or [])
    home.scan_tvs()
    assert seen == [[N("192.168.0.0/23")]]


def _fake_home_class(sightings):
    class FakeHome(Home):
        def __init__(self, cfg, **kw):
            super().__init__(cfg, local_ip_fn=lambda: "192.168.1.20", adapters_fn=list,
                             find_tvs_fn=lambda s: sightings)

    return FakeHome


def test_cli_find_without_config_lists_all_tvs(monkeypatch, capsys):
    from tvpult import cli

    sightings = [TvSighting("192.168.1.50", normalize_mac(TV_A), "QE65", "Samsung TV", 2022, True),
                 TvSighting("192.168.1.66", normalize_mac(OTHER), "", "odd", None, False, "the MAC it reports differs")]
    monkeypatch.setattr(cli, "Home", _fake_home_class(sightings))
    assert cli.main(["find"]) == 0  # no config at all: lists every TV instead of failing
    out = capsys.readouterr().out
    assert "192.168.1.50" in out and "2022" in out and "новый" in out
    assert "192.168.1.66" in out and "⚠" in out
    assert "02:00:00:00:55:01" in out


def test_cli_find_all_with_config(monkeypatch, capsys):
    from tvpult import cli, config

    config.config_file().parent.mkdir(parents=True, exist_ok=True)
    config.config_file().write_text(
        '[tv.living]\nname = "L"\nhost = "192.168.1.50"\nmac = "02:00:00:00:55:01"\n', encoding="utf-8")
    sightings = [TvSighting("192.168.1.50", normalize_mac(TV_A), "QE65", "Samsung", 2022, True)]
    monkeypatch.setattr(cli, "Home", _fake_home_class(sightings))
    assert cli.main(["find", "--all"]) == 0
    assert "настроен как living" in capsys.readouterr().out


def test_cli_find_nothing_found_explains(monkeypatch, capsys):
    from tvpult import cli

    monkeypatch.setattr(cli, "Home", _fake_home_class([]))
    assert cli.main(["find"]) == 0
    assert "не найдено" in capsys.readouterr().out
