"""#30: a mistake in devices.toml is one line naming the place — in the terminal and on the page."""

import pytest

from tvpult import config
from tvpult.config import Config, ConfigError, add_tv_to_config, parse_config
from tvpult.devices import DeviceError, Home
from tvpult.i18n import use_lang
from tvpult.server import dispatch

TV = '[tv.living]\nname = "Living room"\nhost = "192.168.1.50"\n'
BAR = '[soundbar.bar]\nhost = "192.168.1.60"\nuuid = "6f1c2a3b-1d2e-4f50-8a6b-00000000ba01"\n'


def _error(text: str) -> ConfigError:
    with pytest.raises(ConfigError) as e:
        parse_config(text)
    return e.value


# --- every row of the issue's table, and the neighbours ---------------------------------------------------
@pytest.mark.parametrize(("text", "code", "where"), [
    ('[tv.living\nhost = "192.168.1.50"\n', "config_syntax", None),
    ('[tv.a]\nname = "A"\n', "config_missing", "[tv.a]"),
    ("tv = 5\n", "config_not_table", "[tv]"),
    ("[tv]\nliving = 5\n", "config_not_table", "[tv.living]"),
    ("[ui]\nquick_apps = 5\n", "config_not_list", "[ui] quick_apps"),
    ('[ui]\nquick_apps = ["youtube", 5]\n', "config_not_list", "[ui] quick_apps"),
    ('[tv."a b"]\nhost = "192.168.1.50"\n', "config_bad_id", '[tv.a b]'),
    ('[tv."гостиная"]\nhost = "192.168.1.50"\n', "config_bad_id", "[tv.гостиная]"),
    ('[tv.Living]\nhost = "192.168.1.50"\n', "config_bad_id", "[tv.Living]"),
    ('[tv.a]\nhost = "example.com"\n', "config_bad_host", "[tv.a] host"),
    ('[tv.a]\nhost = "8.8.8.8"\n', "config_bad_host", "[tv.a] host"),
    ('[tv.a]\nhost = "127.0.0.1"\n', "config_bad_host", "[tv.a] host"),
    ("[tv.a]\nhost = 5\n", "config_not_text", "[tv.a] host"),
    (TV + 'mac = "02:00:00:00:55"\n', "config_bad_mac", "[tv.living] mac"),
    (TV + 'lan_mac = "nope"\n', "config_bad_mac", "[tv.living] lan_mac"),
    (TV + 'wake_on_lan = "yes"\n', "config_not_bool", "[tv.living] wake_on_lan"),
    ('[tv.living]\nhost = "192.168.1.50"\nname = 5\n', "config_not_text", "[tv.living] name"),
    (TV + "assume_apps = 5\n", "config_not_table", "[tv.living] assume_apps"),
    (TV + "[tv.living.assume_apps]\nbrowser = 5\n", "config_not_text", "[tv.living.assume_apps] browser"),
    ('[soundbar.bar]\nhost = "192.168.1.60"\n', "config_missing", "[soundbar.bar]"),
    (BAR.replace("6f1c2a3b-1d2e-4f50-8a6b-00000000ba01", "nope"), "config_bad_uuid", "[soundbar.bar] uuid"),
    (BAR + "port = 0\n", "config_bad_port", "[soundbar.bar] port"),
    (BAR + "port = true\n", "config_bad_port", "[soundbar.bar] port"),
    ("[apps]\nokko = 5\n", "config_not_list", "[apps] okko"),
    ("[wifi]\naa = 5\n", "config_not_text", "[wifi] aa"),
    ('[network]\nscan_subnets = "192.168.5.0/24"\n', "config_not_list", "[network] scan_subnets"),
])
def test_each_mistake_names_its_place(text, code, where):
    e = _error(text)
    assert e.code == code
    if where:
        assert e.params["where"] == where and str(e).startswith(where)
    assert "\n" not in str(e)  # one line


def test_syntax_error_says_where_in_the_file():
    assert "line 1" in str(_error('[tv.living\nhost = "192.168.1.50"\n'))


def test_the_message_is_translated():
    e = _error('[tv.a]\nhost = "example.com"\n')
    assert str(e) == '[tv.a] host: "example.com" is not an IPv4 address on the home network (like 192.168.1.50)'
    use_lang("ru")
    assert e.localized() == "[tv.a] host: «example.com» — не IPv4-адрес домашней сети (например, 192.168.1.50)"


def test_a_good_file_still_loads():
    cfg = parse_config(TV + 'mac = "8c:79:f5:0a:1b:2c"\nwake_on_lan = true\n' + BAR + "port = 8009\n"
                       + '[apps]\nokko = "1"\nkino = ["2", "3"]\n[ui]\nquick_apps = ["okko"]\n'
                       + '[wifi]\n"AA:BB:CC:DD:EE:FF" = "home"\n[network]\nscan_subnets = ["192.168.5.0/24"]\n')
    assert cfg.tvs["living"].wake_on_lan and cfg.soundbars["bar"].port == 8009
    assert cfg.extra_apps == {"okko": ["1"], "kino": ["2", "3"]} and cfg.wifi_names == {"aa:bb:cc:dd:ee:ff": "home"}
    assert parse_config('[tv.my_tv-2]\nhost = "10.0.0.5"\n').tvs["my_tv-2"].name == "my_tv-2"


def test_a_file_that_is_not_utf8(tmp_path):
    p = tmp_path / "d.toml"
    p.write_bytes(b'[tv.a]\nname = "\xff"\n')
    with pytest.raises(ConfigError) as e:
        config.load_config(p)
    assert e.value.code == "config_syntax"


# --- the wizard writes in one step and never a wrong file ----------------------------------------------------
def test_wizard_writes_nothing_when_the_result_is_wrong():
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[ui]\nquick_apps = 5\n', encoding="utf-8")  # already wrong: appending can't fix it
    with pytest.raises(ConfigError):
        add_tv_to_config(config.TvConfig(id="new", name="New", host="192.168.1.70"))
    assert path.read_text(encoding="utf-8") == '[ui]\nquick_apps = 5\n'
    assert not list(path.parent.glob("*.tmp"))


# --- the command line -------------------------------------------------------------------------------------
def _broken_file(text='[tv.a]\nhost = "example.com"\n'):
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_cli_prints_one_line_and_the_file(capsys):
    from tvpult.cli import main

    path = _broken_file()
    use_lang("en")
    assert main(["status"]) == 1
    err = capsys.readouterr().err
    assert str(path) in err and '[tv.a] host: "example.com" is not' in err and "Traceback" not in err


@pytest.mark.parametrize("text", ['[tv.a\n', "tv = 5\n", "[ui]\nquick_apps = 5\n", '[tv.a]\nname = "x"\n'])
def test_cli_never_shows_a_traceback(text, capsys):
    from tvpult.cli import main

    _broken_file(text)
    assert main(["key", "a", "ok"]) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_app_mode_starts_anyway_and_the_page_shows_the_mistake(monkeypatch):
    from tvpult import cli, server

    _broken_file()
    started = {}
    monkeypatch.setattr(cli, "_our_server_running", lambda port: False)
    monkeypatch.setattr(server, "serve", lambda home, *a, **k: started.setdefault("home", home))
    try:
        assert cli.main(["--no-browser", "app", "--port", "8799"]) == 0
    finally:  # app mode logs to a file in the (temporary) data folder: let go of it
        import logging

        for h in [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]:
            logging.getLogger().removeHandler(h)
            h.close()
    home = started["home"]
    assert home.config_error is not None and not home.setup_needed  # no wizard over a broken file
    status = dispatch(home, "GET", "/api/status", {})
    assert status["config_error"]["code"] == "config_bad_host"
    assert status["config_error"]["params"]["where"] == "[tv.a] host"
    assert status["config_error"]["file"] == str(config.config_file())


def test_nothing_is_written_while_the_file_is_broken():
    home = Home(Config(), local_ip_fn=lambda: None, adapters_fn=list)
    home.config_error = ConfigError(code="config_not_table", where="[tv]")
    with pytest.raises(DeviceError) as e:
        home.set_quick_apps(["youtube"])
    assert e.value.code == "config_broken"


def test_the_page_has_the_mistake_screen():
    page = (config.PACKAGE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="cfg"' in page and "s.config_error" in page and "errText(e.code, e.params, e.error)" in page
