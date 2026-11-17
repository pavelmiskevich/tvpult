"""#6: English and Russian — the dictionaries match, every key used exists, the language is picked right."""

import json
import re
import string
from pathlib import Path

import pytest

from tvpult import i18n
from tvpult.i18n import detect, page_strings, strings, t, use_lang

SRC = Path(__file__).resolve().parent.parent / "src" / "tvpult"
PAGE = (SRC / "static" / "index.html").read_text(encoding="utf-8")


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


# --- the dictionaries -------------------------------------------------------------------------------
def test_both_dictionaries_have_the_same_keys():
    assert set(strings("en")) == set(strings("ru"))


def test_same_placeholders_and_nothing_empty():
    for key, en in strings("en").items():
        ru = strings("ru")[key]
        assert en.strip() and ru.strip(), key
        assert _placeholders(en) == _placeholders(ru), key


def test_dictionaries_are_plain_json_objects_of_strings():
    for lang in i18n.LANGS:
        raw = json.loads((SRC / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        assert raw and all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items())


def test_every_key_the_page_uses_exists():
    keys = set(page_strings()["en"])
    used = set(re.findall(r'data-i18n(?:-aria|-ph|-alt)?="([^"]+)"', PAGE))
    used |= set(re.findall(r'\bt\("([a-z_.]*[a-z_])"', PAGE)) - {"key"}  # "key": the example in a comment
    used |= set(re.findall(r'"(power\.[a-z_]+)"', PAGE))
    assert used and used <= keys, used - keys
    assert {"kind.tv", "kind.soundbar"} <= keys  # built at run time: t("kind." + d.kind)
    # say("cls", "key"): messages kept per device are stored as keys
    said = set(re.findall(r'say\("\w+", "([a-z_.]+)"', PAGE))
    assert said and said <= keys, said - keys


def test_no_russian_left_in_the_page_script():
    script = PAGE[PAGE.index("<script>"):]
    assert not re.search(r"[А-Яа-яЁё]", script)


def test_every_key_the_code_uses_exists():
    keys = set(strings("en"))
    for f in SRC.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        used = set(re.findall(r'\bt\("([a-z_.]+)"', text)) | set(re.findall(r"\bt\('([a-z_.]+)'", text))
        assert used <= keys, (f.name, used - keys)
    for name in ("serve", "app"):  # built as f"cli.cmd.{name}"
        assert f"cli.cmd.{name}" in keys
    for how in ("mac", "uuid", "arp"):
        assert f"cli.find.by.{how}" in keys


def test_the_page_gets_both_dictionaries_embedded():
    from tvpult.server import I18N_SLOT, _page

    assert I18N_SLOT in PAGE.encode("utf-8")
    html = _page().decode("utf-8")
    script = html[html.index("const I18N = "):]
    data = json.loads(script[len("const I18N = "):script.index(";\n")])
    assert set(data) == {"en", "ru"} and data["ru"]["settings"] == "Настройки" and data["en"]["settings"] == "Settings"
    assert "</" not in script[:script.index(";\n")]  # nothing in the dictionaries can close the <script>


# --- lookups -----------------------------------------------------------------------------------------------
def test_t_fills_placeholders_in_both_languages():
    use_lang("en")
    assert t("cli.init.exists", path="x.toml") == "x.toml already exists — leaving it alone"
    assert t("shortcut.name") == "TV Remote"
    use_lang("ru")
    assert t("cli.init.exists", path="x.toml") == "Уже есть x.toml — не трогаю"
    assert t("shortcut.name") == "Пульт ТВ"


def test_values_are_not_read_as_templates():
    use_lang("en")
    assert t("cli.error", error="{path} {0}") == "Error: {path} {0}"


def test_unknown_language_is_refused():
    with pytest.raises(ValueError):
        use_lang("de")


# --- which language ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("env,expected", [
    ({"TVPULT_LANG": "en", "LANG": "ru_RU.UTF-8"}, "en"),
    ({"TVPULT_LANG": "ru"}, "ru"),
    ({"LANG": "ru_RU.UTF-8"}, "ru"),
    ({"LC_ALL": "en_US.UTF-8", "LANG": "ru_RU.UTF-8"}, "en"),
    ({"LANG": "de_DE.UTF-8"}, "en"),
    ({"TVPULT_LANG": "fr", "LANG": "ru_RU"}, "ru"),  # an unknown override is ignored
])
def test_detect_on_linux_and_mac(env, expected):
    assert detect(env, platform="linux") == expected


@pytest.mark.parametrize("lang_id,expected", [(0x0419, "ru"), (0x0409, "en"), (0x0407, "en")])
def test_detect_on_windows_uses_the_display_language(lang_id, expected):
    assert detect({}, platform="win32", ui_language=lambda: lang_id) == expected


def test_detect_on_windows_respects_the_override():
    assert detect({"TVPULT_LANG": "en"}, platform="win32", ui_language=lambda: 0x0419) == "en"


def test_cli_speaks_english(capsys):
    from tvpult.cli import main

    use_lang("en")
    assert main(["paths"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Settings:") and "tvpult init" in out


def test_status_line_says_cable_in_the_cli_language(capsys):
    from tvpult.cli import _print_status

    use_lang("en")
    _print_status([{"kind": "tv", "id": "tv", "name": "TV", "host": "192.168.1.50", "online": True, "power": "on",
                    "paired": True, "wired": True, "network": ""}])
    assert "on, cable" in capsys.readouterr().out


def test_serve_on_the_lan_prints_the_pin_line(tmp_path, capsys):
    """serve() starts and stops, printing the phone line in the CLI language (a loop variable once hid t())."""
    from tvpult.auth import LanAuth
    from tvpult.server import Presence, serve

    class QuietHome:
        def status(self, rediscover="none"):
            return []

        def close(self):
            pass

    use_lang("en")
    auth = LanAuth(tmp_path / "sessions.json", pin="123456")
    serve(QuietHome(), [], 0, presence=Presence(idle_exit=True, startup=0), lan="127.0.0.1", auth=auth)
    assert "from a phone on the home network, PIN: 123456" in capsys.readouterr().out
