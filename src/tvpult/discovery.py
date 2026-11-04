"""Finding the devices when their addresses change.

Devices are recognised by what doesn't change:
  * a Samsung TV by its Wi-Fi MAC, which it reports itself (GET :8001/api/v2/ -> device.wifiMac);
  * a Cast soundbar by its Cast UUID (GET :8008/setup/eureka_info -> ssdp_udn) — Cast hides its MAC
    in that API — or, as a fallback, by its MAC in the laptop's ARP table.

Only private /24 networks are ever scanned: the laptop's own network, the networks of the last known
addresses, and any extra ones from devices.toml. Found addresses are kept in .data/addresses.json.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

TV_PORT = 8001
CAST_PORT = 8009
CAST_HTTP_PORT = 8008
MAX_SUBNETS = 4
_PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
_MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2}(?:[-:][0-9a-f]{2}){5})\b")
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


# --- identifiers ----------------------------------------------------------------
def normalize_mac(mac: str | None) -> str:
    """'02:00:00:00:55:01', '02-00-00-00-55-01', '020000005501' -> '020000005501'; anything else -> ''."""
    hexes = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    if len(hexes) != 12 or hexes == "000000000000":
        return ""
    return hexes


def normalize_uuid(value: str | None) -> str:
    return re.sub(r"[^0-9a-f]", "", (value or "").lower())


# --- where to look ----------------------------------------------------------------
def is_private_lan(ip: ipaddress.IPv4Address) -> bool:
    return any(ip in net for net in _PRIVATE) and not ip.is_loopback and not ip.is_link_local


def default_local_ip() -> str | None:
    """The laptop's address on the network that carries its default route. Sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP connect only picks a route; no packet leaves
            return s.getsockname()[0]
    except OSError:
        return None


MIN_PREFIX = 22  # never scan more than ~1k addresses in one network, whatever its real mask


def adapter_addresses() -> list[tuple[str, int]]:
    """(IPv4 address, prefix length) of every network adapter of this machine; [] if unknown."""
    return read_adapters()


def read_adapters(get_adapters: Callable[[], Iterable[Any]] | None = None) -> list[tuple[str, int]]:
    try:
        if get_adapters is None:
            import ifaddr

            get_adapters = ifaddr.get_adapters
        return [(ip.ip, ip.network_prefix) for a in get_adapters() for ip in a.ips if isinstance(ip.ip, str)]
    except Exception:  # no adapter info: callers fall back to /24
        log.debug("adapter list unavailable", exc_info=True)
        return []


def local_network(local_ip: str, adapters: Iterable[tuple[str, int]]) -> ipaddress.IPv4Network:
    """The real network of the adapter holding local_ip (mask from the adapter), at most /MIN_PREFIX wide.

    Falls back to /24 when the adapter isn't known.
    """
    for ip, prefix in adapters:
        if ip == local_ip and 0 < prefix <= 32:
            return ipaddress.IPv4Network(f"{ip}/{max(prefix, MIN_PREFIX)}", strict=False)
    return ipaddress.IPv4Network(f"{local_ip}/24", strict=False)


def candidate_subnets(local_ips: Iterable[str | None], known_hosts: Iterable[str],
                      extra: Iterable[str] = (),
                      adapters: Iterable[tuple[str, int]] | None = None) -> list[ipaddress.IPv4Network]:
    """Private networks to scan, most likely first, at most MAX_SUBNETS.

    * the laptop's own network(s) with their real mask (adapters), capped at /MIN_PREFIX — only the
      adapter that holds the given local address, so VPN / WSL / Hyper-V adapters are never scanned;
    * the /24 of each last known device address, unless already covered;
    * explicitly configured networks (private, /MIN_PREFIX or smaller).
    """
    adapters = list(adapters or [])
    nets: list[ipaddress.IPv4Network] = []

    def add(net: ipaddress.IPv4Network) -> None:
        if len(nets) < MAX_SUBNETS and not any(net.subnet_of(n) for n in nets):
            nets.append(net)

    def private(value: str | None) -> str | None:
        try:
            ip = ipaddress.IPv4Address(value or "")
        except ValueError:
            return None
        return str(ip) if is_private_lan(ip) else None

    for value in local_ips:
        if ip := private(value):
            add(local_network(ip, adapters))
    for value in known_hosts:
        if ip := private(value):
            add(ipaddress.IPv4Network(f"{ip}/24", strict=False))
    for value in extra:
        try:
            net = ipaddress.IPv4Network(value, strict=False)
        except ValueError:
            log.warning("scan_subnets: not a network: %r", value)
            continue
        if net.prefixlen < MIN_PREFIX or not is_private_lan(net.network_address):
            log.warning("scan_subnets: %s skipped (only private networks of /%d or smaller)", net, MIN_PREFIX)
            continue
        add(net)
    return nets


# --- scanning -----------------------------------------------------------------------
HINT_TIMEOUT = 1.5  # a Wi-Fi device idle for a while answers its first connection slowly


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(subnets: Iterable[ipaddress.IPv4Network], ports: Iterable[int],
         connect: Callable[[str, int], bool] = port_open, workers: int = 128) -> list[tuple[str, int]]:
    """Every (host, port) in the subnets that accepts a TCP connection."""
    targets = [(str(h), p) for net in subnets for h in net.hosts() for p in ports]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as ex:
        hits = ex.map(lambda t: connect(*t), targets)
        return [t for t, ok in zip(targets, hits, strict=True) if ok]


def _http() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # LAN only, never through a proxy
    return s


def model_year(platform_model: str | None) -> int | None:
    """Samsung's platform code starts with the model year: '22_PONTUSM_QTV' -> 2022, '17_KANTS_FHD' -> 2017."""
    m = re.match(r"^(\d{2})_", platform_model or "")
    return 2000 + int(m.group(1)) if m else None


def identify_tv(host: str, http: requests.Session | None = None) -> dict[str, Any] | None:
    """What the Samsung TV at host says about itself (mac, model, name, year, wired), or None."""
    try:
        r = (http or _http()).get(f"http://{host}:{TV_PORT}/api/v2/", timeout=2)
        dev = r.json().get("device", {}) if r.ok else {}
    except (requests.RequestException, ValueError, AttributeError):
        return None
    mac = normalize_mac(dev.get("wifiMac"))
    if not mac:
        return None
    return {"mac": mac, "model": str(dev.get("modelName", "")), "name": str(dev.get("name", "")),
            "year": model_year(dev.get("model")), "wired": dev.get("networkType") == "wired"}


@dataclass
class TvSighting:
    """A Samsung TV seen on the network during a full scan."""

    host: str
    mac: str  # the Wi-Fi MAC the TV reports
    model: str
    name: str
    year: int | None
    confirmed: bool  # ARP agrees with the reported MAC — only then may it be paired or used
    note: str = ""


def find_tvs(subnets: list[ipaddress.IPv4Network], *,
             scan_fn: Callable[..., list[tuple[str, int]]] | None = None,
             identify_tv_fn: Callable[[str], dict[str, Any] | None] | None = None,
             arp_fn: Callable[[], dict[str, str]] | None = None) -> list[TvSighting]:
    """Every Samsung TV answering in the subnets, configured or not, sorted by address.

    A TV whose reported MAC the ARP table doesn't confirm is still listed — with confirmed=False and
    the reason — so the user sees it instead of wondering; it is never paired or used that way.
    """
    # Looked up at call time (module globals), so tests can replace them.
    scan_fn = scan_fn or scan
    identify_tv_fn = identify_tv_fn or identify_tv
    arp_fn = arp_fn or read_arp
    hosts = [h for h, p in scan_fn(subnets, (TV_PORT,)) if p == TV_PORT]
    if not hosts:
        return []
    arp_by_ip = {ip: mac for mac, ip in arp_fn().items()}
    seen: list[TvSighting] = []
    for host in hosts:
        ident = identify_tv_fn(host)
        if not ident:
            continue  # something else listens on 8001
        arp_mac = arp_by_ip.get(host, "")
        confirmed = arp_mac == ident["mac"]
        note = ""
        if not confirmed:
            if ident.get("wired") and arp_mac:
                note = "connected by cable: the network shows its Ethernet MAC — add it as lan_mac to confirm"
            elif not arp_mac:
                note = "the network didn't confirm its MAC (no ARP entry)"
            else:
                note = "the MAC it reports differs from the one the network sees — not trusted"
        seen.append(TvSighting(host, ident["mac"], ident["model"], ident["name"], ident.get("year"),
                               confirmed, note))
    return sorted(seen, key=lambda t: ipaddress.IPv4Address(t.host))


def identify_cast(host: str, http: requests.Session | None = None) -> str | None:
    """Normalised Cast UUID of the device at host, or None."""
    try:
        r = (http or _http()).get(
            f"http://{host}:{CAST_HTTP_PORT}/setup/eureka_info?params=name,device_info", timeout=2)
        data = r.json() if r.ok else {}
    except (requests.RequestException, ValueError):
        return None
    return normalize_uuid(data.get("ssdp_udn")) or None


def parse_arp(text: str) -> dict[str, str]:
    """MAC -> IP from `arp -a` output (Windows 'aa-bb-…' and Unix 'aa:bb:…' formats)."""
    table: dict[str, str] = {}
    for line in text.splitlines():
        ip_m, mac_m = _IP_RE.search(line), _MAC_RE.search(line)
        if not ip_m or not mac_m:
            continue
        mac = normalize_mac(mac_m.group(1))
        if mac and mac != "ffffffffffff" and not mac.startswith("01005e"):  # skip broadcast/multicast
            table.setdefault(mac, ip_m.group(1))
    return table


def read_arp() -> dict[str, str]:
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10)
        return parse_arp(out.stdout)
    except (OSError, subprocess.SubprocessError):
        return {}


def arp_mac(ip: str, arp_fn: Callable[[], dict[str, str]] = read_arp) -> str:
    """The MAC the laptop's ARP table holds for ip ('' if none).

    This comes from the network layer, not from the device's own answer, so a device on the LAN
    can't simply claim to be our TV by putting the TV's MAC into its HTTP reply.
    """
    for mac, addr in arp_fn().items():
        if addr == ip:
            return mac
    return ""


def mac_confirmed(ip: str, claimed_mac: str, arp_fn: Callable[[], dict[str, str]] = read_arp) -> bool:
    """True only if the ARP table agrees that ip has claimed_mac."""
    want = normalize_mac(claimed_mac)
    return bool(want) and arp_mac(ip, arp_fn) == want


# --- matching -------------------------------------------------------------------------
@dataclass(frozen=True)
class Wanted:
    """A device to look for."""
    id: str
    kind: str  # "tv" | "cast"
    mac: str = ""
    uuid: str = ""
    lan_mac: str = ""  # a cabled TV shows this MAC in ARP while still reporting `mac` itself

    def arp_macs(self) -> set[str]:
        """MACs the ARP table may legitimately show for this device."""
        return {m for m in (normalize_mac(self.mac), normalize_mac(self.lan_mac)) if m}


@dataclass
class Found:
    host: str
    how: str  # "mac" | "uuid" | "arp"


def locate(wanted: Iterable[Wanted], subnets: list[ipaddress.IPv4Network], *,
           hints: dict[str, str] | None = None,
           scan_fn: Callable[..., list[tuple[str, int]]] = scan,
           slow_connect: Callable[[str, int], bool] = lambda h, p: port_open(h, p, HINT_TIMEOUT),
           identify_tv_fn: Callable[[str], dict[str, str] | None] = identify_tv,
           identify_cast_fn: Callable[[str], str | None] = identify_cast,
           arp_fn: Callable[[], dict[str, str]] = read_arp) -> dict[str, Found]:
    """Scan the subnets once and return device id -> where it is now.

    hints: device id -> last known address. A device the fast scan missed is tried once more at
    its last address with a longer timeout (sleepy Wi-Fi), so one slow answer doesn't lose it.
    """
    wanted = list(wanted)
    tvs_by_mac = {normalize_mac(w.mac): w.id for w in wanted if w.kind == "tv" and normalize_mac(w.mac)}
    casts_by_uuid = {normalize_uuid(w.uuid): w.id for w in wanted if w.kind == "cast" and normalize_uuid(w.uuid)}
    retry = list(dict.fromkeys(
        (host, TV_PORT if w.kind == "tv" else CAST_PORT)
        for w in wanted if (host := (hints or {}).get(w.id))
    ))
    # The slow retries at the last addresses run alongside the fast scan, not after it.
    with ThreadPoolExecutor(max_workers=len(retry) + 1) as ex:
        scanning = ex.submit(scan_fn, subnets, (TV_PORT, CAST_PORT))
        slow = [(t, ex.submit(slow_connect, *t)) for t in retry]
        open_ports = list(scanning.result())
        open_ports += [t for t, fut in slow if fut.result() and t not in open_ports]
    tv_hosts = [h for h, p in open_ports if p == TV_PORT]
    cast_hosts = [h for h, p in open_ports if p == CAST_PORT]
    found: dict[str, Found] = {}
    # The scan itself filled the ARP table. A device's own claim (MAC in its API answer, Cast UUID)
    # is accepted only when the ARP table confirms the MAC at that address — otherwise any device on
    # the LAN could pose as our TV and receive the pairing token and everything typed into it.
    arp = arp_fn() if (tv_hosts or cast_hosts) else {}
    arp_by_ip = {ip: mac for mac, ip in arp.items()}
    macs_by_id = {w.id: normalize_mac(w.mac) for w in wanted}
    arp_ok = {w.id: w.arp_macs() for w in wanted}

    for host in tv_hosts:
        ident = identify_tv_fn(host)
        dev_id = tvs_by_mac.get(ident["mac"]) if ident else None
        if not dev_id or dev_id in found:
            continue
        if arp_by_ip.get(host) not in arp_ok[dev_id]:
            log.warning("%s claims to be %s, but ARP says %s — ignored", host, dev_id, arp_by_ip.get(host) or "nothing")
            continue
        found[dev_id] = Found(host, "mac")
    for host in cast_hosts:
        dev_id = casts_by_uuid.get(identify_cast_fn(host) or "")
        if not dev_id or dev_id in found:
            continue
        want_mac = macs_by_id.get(dev_id)
        if want_mac and arp_by_ip.get(host) != want_mac:
            log.warning("%s claims to be %s, but ARP says %s — ignored", host, dev_id, arp_by_ip.get(host) or "nothing")
            continue
        found[dev_id] = Found(host, "uuid")

    # Fallback for Cast devices with a configured MAC whose info endpoint stayed silent: the ARP
    # table alone, accepted only if that address really is a Cast device.
    for w in wanted:
        if w.kind == "cast" and w.id not in found and macs_by_id.get(w.id):
            host = arp.get(macs_by_id[w.id])
            if host and host in cast_hosts:
                found[w.id] = Found(host, "arp")
    return found


# --- memory of addresses ------------------------------------------------------------
@dataclass
class AddressBook:
    path: Path
    hosts: dict[str, str] = field(default_factory=dict)
    scanned_at: float = 0.0

    @classmethod
    def load(cls, path: Path) -> AddressBook:
        try:
            raw: Any = json.loads(Path(path).read_text(encoding="utf-8"))
            hosts = {str(k): str(v) for k, v in dict(raw.get("hosts", {})).items()}
            for v in hosts.values():
                ipaddress.IPv4Address(v)  # reject anything that isn't a plain IPv4 address
            return cls(Path(path), hosts, float(raw.get("scanned_at", 0.0)))
        except (OSError, ValueError, TypeError, AttributeError):
            return cls(Path(path))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hosts": self.hosts, "scanned_at": self.scanned_at}, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def mark_scanned(self) -> None:
        self.scanned_at = time.time()
