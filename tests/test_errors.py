"""#25: errors carry a code; the page and the command line word them in their language."""

import re
from pathlib import Path

import pytest

from tvpult.devices import DeviceError, Home
from tvpult.errors import TvpultError, localized
from tvpult.i18n import strings, use_lang
from tvpult.server import ApiError, dispatch

SRC = Path(__file__).resolve().parent.parent / "src" / "tvpult"
CODE = {f.name: f.read_text(encoding="utf-8") for f in SRC.glob("*.py")}


def _codes_raised() -> set[str]:
    found = set()
    for text in CODE.values():
        found |= set(re.findall(r'\bcode="([a-z_]+)"', text))
        found |= set(re.findall(r'ApiError\(HTTPStatus\.[A-Z_]+, "([a-z_]+)"', text))
        found |= set(re.findall(r'_err\(HTTPStatus\.[A-Z_]+, "([a-z_]+)"\)', text))
        found |= set(re.findall(r'SignInError\(\d+, "([a-z_]+)"\)', text))
        found |= set(re.findall(r'"(tv_[a-z_]+)"', text))  # discovery's notes
    found |= set(re.findall(r'PAIRING_BUSY = "([a-z_]+)"', CODE["devices.py"]))
    return found


def test_every_code_raised_has_words_in_both_languages():
    codes = _codes_raised()
    assert len(codes) > 30
    for lang in ("en", "ru"):
        missing = {c for c in codes if f"err.{c}" not in strings(lang)}
        assert not missing, (lang, missing)


def test_no_free_english_text_left_in_raised_errors():
    for name, text in CODE.items():
        assert not re.search(r'DeviceError\(f?"', text), name  # DeviceError(code=...) only
        assert not re.search(r'ApiError\(HTTPStatus\.[A-Z_]+, f?"[^"]* ', text), name  # codes have no spaces
        if name != "errors.py":  # (its docstring describes the format)
            assert '{"error": "' not in text, name  # every refusal goes through a code


def test_the_page_words_errors_by_code():
    page = (SRC / "static" / "index.html").read_text(encoding="utf-8")
    assert "errText(data.code" in page and "tv.note_code" in page
    assert '"wrong PIN"' not in page and 'startsWith("too many")' not in page


# --- the error itself ------------------------------------------------------------------------------------
def test_text_is_english_and_localized_follows_the_language():
    e = DeviceError(code="offline", name="Гостиная")
    assert str(e) == "Гостиная is off or unreachable"
    use_lang("ru")
    assert e.localized() == "Гостиная: выключен или недоступен"
    use_lang("en")
    assert localized(e) == "Гостиная is off or unreachable"


def test_free_text_and_unknown_codes_still_work():
    assert str(DeviceError("something odd")) == "something odd" and localized(DeviceError("x")) == "x"
    assert str(TvpultError(code="no_such_code")) == "no_such_code"
    assert localized(ValueError("plain")) == "plain"


def test_a_missing_parameter_shows_the_template_instead_of_failing():
    assert str(DeviceError(code="offline")) == "{name} is off or unreachable"


def test_parameters_are_not_read_as_templates():
    use_lang("ru")
    assert DeviceError(code="device_failed", name="{x}", detail="{0}").localized() == "{x}: {0}"


# --- the API -----------------------------------------------------------------------------------------------
def test_api_answers_with_code_and_params():
    with pytest.raises(ApiError) as e:
        dispatch(Home.__new__(Home), "POST", "/api/settings/quick", {"apps": "x"})
    assert e.value.status == 400
    assert e.value.payload() == {
        "error": "apps: a list of up to 12 app names",
        "code": "bad_apps",
        "params": {"max": "12"},
    }


def test_a_device_error_keeps_its_code_through_the_api():
    from tvpult.config import Config

    with pytest.raises(ApiError) as e:
        dispatch(Home(Config()), "POST", "/api/tv/nope/key", {"keys": ["ok"]})
    assert e.value.status == 409 and e.value.code == "no_tv" and e.value.params == {"id": "nope", "have": ""}


def test_sign_in_errors_have_codes(tmp_path):
    from tvpult.auth import LanAuth, SignInError

    auth = LanAuth(tmp_path / "s.json", pin="123456")
    with pytest.raises(SignInError) as e:
        auth.sign_in("192.168.1.30", "000000")
    assert e.value.status == 401 and e.value.code == "wrong_pin" and str(e.value) == "wrong PIN"


# --- the command line ---------------------------------------------------------------------------------------
def test_cli_prints_errors_in_its_language(capsys):
    from tvpult import config
    from tvpult.cli import main

    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[tv.living]\nname = "Гостиная"\nhost = "192.168.1.50"\n', encoding="utf-8")
    use_lang("ru")
    assert main(["key", "nope", "ok"]) == 1
    assert "Ошибка: нет телевизора «nope» (есть: living)" in capsys.readouterr().err
    use_lang("en")
    assert main(["key", "nope", "ok"]) == 1
    assert "Error: no TV 'nope' (have: living)" in capsys.readouterr().err


def test_find_all_notes_in_the_cli_language(capsys):
    from tvpult.cli import _print_sightings

    tv = {
        "host": "192.168.1.50",
        "mac": "020000005501",
        "model": "QE65",
        "name": "TV",
        "year": 2022,
        "confirmed": False,
        "note": "the network didn't confirm its MAC (no ARP entry)",
        "note_code": "tv_no_arp",
        "configured": None,
    }
    use_lang("ru")
    _print_sightings([tv])
    assert "сеть не подтвердила его MAC" in capsys.readouterr().out
