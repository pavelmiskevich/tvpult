"""Device configuration loaded from devices.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_DIR / "devices.toml"
DATA_DIR = PROJECT_DIR / ".data"


@dataclass(frozen=True)
class TvConfig:
    id: str
    name: str
    host: str
    mac: str = ""
    wake_on_lan: bool = False
    # MAC of the TV's Ethernet port, if it is ever cabled: the ARP table then shows this one,
    # while the TV still reports its Wi-Fi MAC (`mac`) about itself.
    lan_mac: str = ""
    # Apps known to be on this TV that its REST API can't see (2017 sets: org.tizen.*).
    assume_apps: dict[str, str] = field(default_factory=dict)

    @property
    def token_file(self) -> Path:
        return DATA_DIR / f"token-{self.id}.txt"


@dataclass(frozen=True)
class SoundbarConfig:
    id: str
    name: str
    host: str
    uuid: str
    port: int = 8009
    mac: str = ""  # optional: lets discovery fall back to the ARP table


@dataclass(frozen=True)
class Config:
    tvs: dict[str, TvConfig] = field(default_factory=dict)
    soundbars: dict[str, SoundbarConfig] = field(default_factory=dict)
    extra_apps: dict[str, list[str]] = field(default_factory=dict)
    quick_apps: list[str] = field(default_factory=list)
    wifi_names: dict[str, str] = field(default_factory=dict)  # BSSID (lowercase) -> label
    scan_subnets: list[str] = field(default_factory=list)  # extra private /24 networks to search


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    tvs = {
        tv_id: TvConfig(
            id=tv_id,
            name=str(v.get("name", tv_id)),
            host=str(v["host"]),
            mac=str(v.get("mac", "")),
            wake_on_lan=bool(v.get("wake_on_lan", False)),
            lan_mac=str(v.get("lan_mac", "")),
            assume_apps={str(a): str(i) for a, i in v.get("assume_apps", {}).items()},
        )
        for tv_id, v in raw.get("tv", {}).items()
    }
    bars = {
        bar_id: SoundbarConfig(
            id=bar_id,
            name=str(v.get("name", bar_id)),
            host=str(v["host"]),
            uuid=str(v["uuid"]),
            port=int(v.get("port", 8009)),
            mac=str(v.get("mac", "")),
        )
        for bar_id, v in raw.get("soundbar", {}).items()
    }
    apps = {
        str(k): [str(i) for i in (v if isinstance(v, list) else [v])]
        for k, v in raw.get("apps", {}).items()
    }
    quick = [str(a) for a in raw.get("ui", {}).get("quick_apps", [])]
    wifi = {str(k).lower(): str(v) for k, v in raw.get("wifi", {}).items()}
    subnets = [str(s) for s in raw.get("network", {}).get("scan_subnets", [])]
    return Config(tvs=tvs, soundbars=bars, extra_apps=apps, quick_apps=quick, wifi_names=wifi,
                  scan_subnets=subnets)
