"""Allowed remote keys and the app catalog (apps.json: known Tizen app ids)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

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


# --- the app catalog (apps.json) ------------------------------------------------------------------------
# App ids differ between model years and regions, and 2022+ TVs don't list their apps, so each TV is probed
# with these candidates: the first id it knows wins, the name shown is the one the TV reports (or ours).
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")
_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._]{0,63}")
_REGION = re.compile(r"\*|[A-Z]{2}")
SCHEMA = 1
LANGS = ("en", "ru")


@dataclass(frozen=True)
class AppId:
    id: str
    seen: tuple[int, ...] = ()  # model years this id was confirmed on (Samsung platform code, e.g. 22_ -> 2022)


@dataclass(frozen=True)
class App:
    alias: str
    names: dict[str, str]  # "en" always; other languages optional
    regions: tuple[str, ...]  # "*" = everywhere, else ISO 3166 country codes ("RU")
    ids: tuple[AppId, ...]

    def name(self, lang: str = "en") -> str:
        return self.names.get(lang) or self.names["en"]


class CatalogError(ValueError):
    pass


def _check(cond: bool, where: str, what: str) -> None:
    if not cond:
        raise CatalogError(f"apps.json: {where}: {what}")


def parse_catalog(raw: Any) -> dict[str, App]:
    """Check apps.json against its schema and turn it into App entries (CatalogError says what is wrong)."""
    _check(isinstance(raw, dict) and set(raw) == {"schema", "apps"}, "top level", 'only "schema" and "apps"')
    _check(raw["schema"] == SCHEMA, "schema", f"must be {SCHEMA}")
    _check(isinstance(raw["apps"], dict) and raw["apps"], "apps", "a non-empty object")
    apps: dict[str, App] = {}
    seen_ids: dict[str, str] = {}
    for alias, a in raw["apps"].items():
        _check(isinstance(alias, str) and bool(_ALIAS.fullmatch(alias)), repr(alias),
               "alias: lowercase letters, digits, . _ - (up to 40)")
        _check(isinstance(a, dict) and set(a) == {"names", "regions", "ids"}, alias, '"names", "regions", "ids"')
        names, regions, ids = a["names"], a["regions"], a["ids"]
        _check(isinstance(names, dict) and isinstance(names.get("en"), str) and names["en"].strip() != "", alias,
               'names: an object with at least "en"')
        _check(set(names) <= set(LANGS), alias, f"names: only {', '.join(LANGS)}")
        _check(all(isinstance(v, str) and v.strip() and len(v) <= 40 for v in names.values()), alias,
               "names: non-empty text, up to 40 characters")
        _check(isinstance(regions, list) and regions and all(isinstance(r, str) and _REGION.fullmatch(r)
                                                             for r in regions), alias,
               'regions: a list of "*" or country codes like "RU"')
        _check(len(set(regions)) == len(regions) and ("*" not in regions or regions == ["*"]), alias,
               'regions: no duplicates; "*" alone')
        _check(isinstance(ids, list) and ids, alias, "ids: a non-empty list")
        entries = []
        for i in ids:
            _check(isinstance(i, dict) and "id" in i and set(i) <= {"id", "seen"}, alias,
                   'each id: {"id": "...", "seen": [years]}')
            _check(isinstance(i["id"], str) and bool(_APP_ID.fullmatch(i["id"])), alias, f"bad app id {i['id']!r}")
            _check(i["id"] not in seen_ids, alias, f"id {i['id']} is also under {seen_ids.get(i['id'])}")
            seen_ids[i["id"]] = alias
            years = i.get("seen", [])
            _check(isinstance(years, list) and all(type(y) is int and 2015 <= y <= 2040 for y in years)
                   and years == sorted(set(years)), alias, "seen: model years, ascending, e.g. [2017, 2022]")
            entries.append(AppId(i["id"], tuple(years)))
        apps[alias] = App(alias, dict(names), tuple(regions), tuple(entries))
    return apps


@cache
def catalog() -> dict[str, App]:
    """The shipped catalog (src/tvpult/apps.json)."""
    text = resources.files("tvpult").joinpath("apps.json").read_text(encoding="utf-8")
    return parse_catalog(json.loads(text))


def app_names() -> dict[str, dict[str, str]]:
    """alias -> {lang: name} for every language (missing ones in English), for the page."""
    return {a.alias: {lang: a.name(lang) for lang in LANGS} for a in catalog().values()}


def app_catalog(extra: dict[str, list[str]] | None = None, year: int | None = None) -> dict[str, list[str]]:
    """alias -> ids in the order to try them: the user's own first, then those seen on this model year."""
    cat = {}
    for app in catalog().values():
        ids = sorted(app.ids, key=lambda i: year not in i.seen) if year else list(app.ids)  # stable sort
        cat[app.alias] = [i.id for i in ids]
    for alias, ids in (extra or {}).items():
        cat[alias] = list(dict.fromkeys([*ids, *cat.get(alias, [])]))
    return cat
