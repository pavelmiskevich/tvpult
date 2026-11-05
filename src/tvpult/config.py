"""Device configuration (devices.toml) and where tvpult keeps its files.

Files live in the user's directories, never next to the code (which may sit in site-packages):
  * config — devices.toml in the user config dir (Windows: %LOCALAPPDATA%\\tvpult, Linux: ~/.config/tvpult);
  * data — pairing tokens, remembered addresses, log, shortcut icon in the user data dir
    (Windows: %LOCALAPPDATA%\\tvpult, Linux: ~/.local/share/tvpult).
Overrides, strongest first: CLI flags (use_paths), then TVPULT_CONFIG / TVPULT_DATA_DIR.
"""

from __future__ import annotations

import os
import re
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
    return parse_config(path.read_text(encoding="utf-8"))


def parse_config(text: str) -> Config:
    raw = tomllib.loads(text)
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


# --- writing (setup wizard) --------------------------------------------------------------
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
                      "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"], strict=True))


def slugify(name: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A short ASCII id for a device name ('Гостиная' -> 'gostinaya'), unique among `taken`."""
    text = "".join(_TRANSLIT.get(c, c) for c in name.lower())
    base = "-".join(re.findall(r"[a-z0-9]+", text))[:24].strip("-") or "tv"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def toml_string(value: str) -> str:
    """A TOML basic string literal (quotes, backslashes and control characters escaped)."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def add_tv_to_config(tv: TvConfig, path: Path | None = None) -> None:
    """Append a [tv.<id>] section to devices.toml; everything already there stays untouched.

    The result is re-read; if it doesn't parse or doesn't contain the new TV, the old file comes back.
    """
    path = Path(path) if path else config_file()
    if not re.fullmatch(r"[a-z0-9-]{1,32}", tv.id):
        raise ValueError(f"bad device id: {tv.id!r}")
    before = path.read_text(encoding="utf-8") if path.exists() else None
    section = "\n".join([
        f"[tv.{tv.id}]",
        f"name = {toml_string(tv.name)}",
        f"host = {toml_string(tv.host)}",
        f"mac = {toml_string(tv.mac)}",
        f"wake_on_lan = {'true' if tv.wake_on_lan else 'false'}",
    ]) + "\n"
    if before is None:
        text = "# tvpult devices — written by the setup wizard; safe to edit by hand.\n\n" + section
    else:
        text = before + ("" if before.endswith("\n") or not before else "\n") + "\n" + section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        if load_config(path).tvs.get(tv.id) is None:
            raise ValueError("the new TV is missing after writing")
    except (tomllib.TOMLDecodeError, ValueError, KeyError):
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")
        raise


# --- editing (settings page) ---------------------------------------------------------------
# Sections in devices.toml for each device kind ("soundbar" is the kind the status reports).
SECTIONS = {"tv": "tv", "soundbar": "soundbar"}
APP_ALIAS = re.compile(r"[A-Za-z0-9_.-]{1,40}")
MAX_QUICK_APPS = 12


def clean_name(name: str) -> str:
    """A device name as stored: whitespace collapsed, at most 40 characters."""
    return " ".join(str(name).split())[:40]


def edit_config(change, path: Path | None = None) -> Config:
    """Apply change(doc) to devices.toml and save it; comments, order and layout stay as they were.

    The new text is checked before anything is written and replaces the file in one step (a crash
    mid-way leaves the old file). Returns the config as read back from the new text.
    """
    import tomlkit

    path = Path(path) if path else config_file()
    doc = tomlkit.parse(path.read_text(encoding="utf-8") if path.exists() else "")
    change(doc)
    text = tomlkit.dumps(doc)
    result = parse_config(text)  # raises if the result isn't valid: nothing written then
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return result


def _section(doc, kind: str, dev_id: str):
    table = doc.get(SECTIONS[kind], {})
    if dev_id not in table:
        raise KeyError(f"no {kind} '{dev_id}' in the config")
    return table[dev_id]


def update_device(kind: str, dev_id: str, *, name: str | None = None, wake_on_lan: bool | None = None,
                  path: Path | None = None) -> Config:
    """Rename a device and/or switch Wake-on-LAN (TVs) in devices.toml."""
    if kind not in SECTIONS:
        raise ValueError(f"unknown device kind: {kind!r}")
    if wake_on_lan is not None and kind != "tv":
        raise ValueError("only TVs have Wake-on-LAN")

    def change(doc):
        sec = _section(doc, kind, dev_id)
        if name is not None:
            if not clean_name(name):
                raise ValueError("the name can't be empty")
            sec["name"] = clean_name(name)
        if wake_on_lan is not None:
            sec["wake_on_lan"] = bool(wake_on_lan)

    return edit_config(change, path)


def remove_device(kind: str, dev_id: str, path: Path | None = None) -> Config:
    """Delete a device's section from devices.toml."""
    if kind not in SECTIONS:
        raise ValueError(f"unknown device kind: {kind!r}")

    def change(doc):
        _section(doc, kind, dev_id)  # KeyError if absent
        table = doc[SECTIONS[kind]]
        del table[dev_id]
        if not table:  # no devices of this kind left: no empty [tv] header either
            del doc[SECTIONS[kind]]

    return edit_config(change, path)


def set_quick_apps(aliases: list[str], path: Path | None = None) -> Config:
    """The quick-launch row: app aliases in the order they are shown ([ui] quick_apps)."""
    import tomlkit

    aliases = list(dict.fromkeys(aliases))
    if len(aliases) > MAX_QUICK_APPS or not all(isinstance(a, str) and APP_ALIAS.fullmatch(a) for a in aliases):
        raise ValueError(f"up to {MAX_QUICK_APPS} app names (letters, digits, . _ -)")

    def change(doc):
        if "ui" not in doc:
            doc["ui"] = tomlkit.table()
        doc["ui"]["quick_apps"] = aliases

    return edit_config(change, path)
