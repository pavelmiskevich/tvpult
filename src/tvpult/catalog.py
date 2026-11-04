"""Allowed remote keys and known Tizen app ids."""

from __future__ import annotations

# Only these keys may be sent. Service-menu keys (KEY_FACTORY, KEY_3SPEED, ...) are
# deliberately absent: the service menu can change settings that have no easy undo.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "KEY_POWER", "KEY_VOLUP", "KEY_VOLDOWN", "KEY_MUTE",
        "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_ENTER",
        "KEY_RETURN", "KEY_EXIT", "KEY_HOME", "KEY_MENU", "KEY_SOURCE",
        "KEY_INFO", "KEY_GUIDE", "KEY_CHUP", "KEY_CHDOWN", "KEY_PRECH",
        "KEY_PLAY", "KEY_PAUSE", "KEY_STOP", "KEY_FF", "KEY_REWIND",
        "KEY_RED", "KEY_GREEN", "KEY_YELLOW", "KEY_CYAN",
        *(f"KEY_{d}" for d in range(10)),
    }
)

# Short aliases accepted by the CLI ("tvpult key living up enter").
KEY_ALIASES: dict[str, str] = {
    "power": "KEY_POWER", "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT",
    "right": "KEY_RIGHT", "ok": "KEY_ENTER", "enter": "KEY_ENTER", "back": "KEY_RETURN",
    "home": "KEY_HOME", "menu": "KEY_MENU", "source": "KEY_SOURCE", "vol+": "KEY_VOLUP",
    "vol-": "KEY_VOLDOWN", "mute": "KEY_MUTE", "ch+": "KEY_CHUP", "ch-": "KEY_CHDOWN",
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP", "info": "KEY_INFO",
    "exit": "KEY_EXIT",
}

# Candidate apps: alias -> possible ids (they differ between models and years).
# Each TV is probed; the first id it knows wins, ids it answers 404 for are hidden,
# and the name shown is the one the TV itself reports.
KNOWN_APPS: dict[str, list[str]] = {
    "youtube": ["111299001912"],
    "browser": ["org.tizen.browser", "3201907018784", "3202010022079"],
    "netflix": ["3201907018807", "11101200001"],
    "prime": ["3201910019365", "3201512006785"],
    "disney": ["3201901017640", "3202009021709", "3202204027038"],
    "spotify": ["3201606009684"],
    "appletv": ["3201807016597"],
    # Russian services (ids read from a 2017 TV's own app list, confirmed on a 2022 one)
    "kinopoisk": ["111399000037"],
    "premier": ["3201806016479"],
    "okko": ["3201506002941"],
    "ivi": ["111199000746"],
    "wink": ["3201710014965"],
    "start": ["3201611011182"],
    "kion": ["3201908018954"],
    "rutube": ["3201508004641"],
    "vkvideo": ["3202112026024"],
    "moretv": ["111299002103"],
}


class UnknownKey(ValueError):
    pass


def normalize_key(key: str) -> str:
    """Map an alias or KEY_* name to an allowed key, or raise UnknownKey."""
    k = key.strip()
    k = KEY_ALIASES.get(k.lower(), k.upper())
    if not k.startswith("KEY_"):
        k = "KEY_" + k
    if k not in ALLOWED_KEYS:
        raise UnknownKey(key)
    return k


def app_catalog(extra: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Built-in candidates plus the user's own; the user's ids are tried first."""
    cat = {k: list(v) for k, v in KNOWN_APPS.items()}
    for alias, ids in (extra or {}).items():
        cat[alias] = list(dict.fromkeys([*ids, *cat.get(alias, [])]))
    return cat
