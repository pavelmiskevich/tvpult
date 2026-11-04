"""Device configuration (devices.toml) and where tvpult keeps its files.

Files live in the user's directories, never next to the code (which may sit in site-packages):
  * config — devices.toml in the user config dir (Windows: %LOCALAPPDATA%\\tvpult, Linux: ~/.config/tvpult);
  * data — pairing tokens, remembered addresses, log, shortcut icon in the user data dir
    (Windows: %LOCALAPPDATA%\\tvpult, Linux: ~/.local/share/tvpult).
Overrides, strongest first: CLI flags (use_paths), then TVPULT_CONFIG / TVPULT_DATA_DIR.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_path, user_data_path

APP = "tvpult"
PACKAGE_DIR = Path(__file__).resolve().parent
EXAMPLE_CONFIG = PACKAGE_DIR / "devices.example.toml"  # shipped inside the package


@dataclass(frozen=True)
class Paths:
    config_file: Path
    data_dir: Path


def default_paths(env: Mapping[str, str] = os.environ) -> Paths:
    config = env.get("TVPULT_CONFIG")
    data = env.get("TVPULT_DATA_DIR")
    return Paths(
        config_file=Path(config) if config else user_config_path(APP, appauthor=False, roaming=False) / "devices.toml",
        data_dir=Path(data) if data else user_data_path(APP, appauthor=False, roaming=False),
    )


_paths = default_paths()


def use_paths(config_file: Path | str | None = None, data_dir: Path | str | None = None) -> Paths:
    """Override where the config and the data are (CLI flags, tests)."""
    global _paths
    _paths = Paths(Path(config_file) if config_file else _paths.config_file,
                   Path(data_dir) if data_dir else _paths.data_dir)
    return _paths


def config_file() -> Path:
    return _paths.config_file


def data_dir() -> Path:
    return _paths.data_dir


def init_config(path: Path | None = None) -> bool:
    """Create the config from the shipped example. False if it already exists (never overwritten)."""
    path = path or config_file()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG, path)
    return True


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
        return data_dir() / f"token-{self.id}.txt"


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

    @property
    def empty(self) -> bool:
        return not self.tvs and not self.soundbars


def load_config(path: Path | None = None) -> Config:
    """Read the config; a missing file is an empty config (first run), not an error."""
    path = Path(path) if path else config_file()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
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
