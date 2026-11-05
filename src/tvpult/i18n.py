"""English and Russian: small JSON dictionaries shipped in the package (locales/en.json, locales/ru.json).

Keys starting with "page." are for the remote page (it gets them embedded and picks its language from the
browser or the switch in its settings); the rest are for the command line and the desktop shortcut, in the
language of the system. TVPULT_LANG=en|ru overrides the system language.
"""

from __future__ import annotations

import json
import locale
import os
import sys
from collections.abc import Callable, Mapping
from functools import cache
from importlib import resources

LANGS = ("en", "ru")
DEFAULT = "en"
_lang: str | None = None


@cache
def strings(lang: str) -> dict[str, str]:
    if lang not in LANGS:
        raise ValueError(f"unknown language: {lang!r}")
    text = resources.files("tvpult").joinpath(f"locales/{lang}.json").read_text(encoding="utf-8")
    return json.loads(text)


def _pick(value: str | None) -> str | None:
    """'ru_RU.UTF-8', 'ru-RU', 'Russian_Russia' -> 'ru'; 'en_US' -> 'en'; anything else -> None."""
    v = (value or "").strip().lower()
    if v.startswith(("ru", "russian")):
        return "ru"
    if v.startswith(("en", "english")):
        return "en"
    return None


def _windows_ui_language() -> int | None:
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def detect(env: Mapping[str, str] = os.environ, platform: str = sys.platform,
           ui_language: Callable[[], int | None] = _windows_ui_language) -> str:
    """The language for the command line: TVPULT_LANG, else the system's (Russian or English, English otherwise)."""
    forced = _pick(env.get("TVPULT_LANG"))
    if forced:
        return forced
    if platform == "win32":
        lang_id = ui_language()
        if lang_id is not None:
            return "ru" if lang_id & 0x3FF == 0x19 else DEFAULT  # 0x19: LANG_RUSSIAN
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(var)
        if value and value not in ("C", "POSIX"):
            return _pick(value) or DEFAULT
    try:
        return _pick(locale.getlocale()[0]) or DEFAULT
    except ValueError:
        return DEFAULT


def lang() -> str:
    global _lang
    if _lang is None:
        _lang = detect()
    return _lang


def use_lang(value: str | None) -> str:
    """Set the language (None: detect again). Returns the one in use."""
    global _lang
    if value is not None and value not in LANGS:
        raise ValueError(f"unknown language: {value!r}")
    _lang = value
    return lang()


def t(key: str, **values: object) -> str:
    """The text for key in the current language ({name} placeholders filled in); English if missing."""
    text = strings(lang()).get(key) or strings(DEFAULT)[key]
    return text.format(**values) if values else text


def page_strings() -> dict[str, dict[str, str]]:
    """Both dictionaries, only the page's keys (without the "page." prefix), for embedding in the page."""
    return {lang_: {k.removeprefix("page."): v for k, v in strings(lang_).items() if k.startswith("page.")}
            for lang_ in LANGS}
