"""Where tvpult keeps its files: user directories, overrides, first run, the desktop shortcut target."""

from pathlib import Path

import pytest

from tvpult import config
from tvpult.config import EXAMPLE_CONFIG, Config, Paths, default_paths, init_config, load_config


def test_example_config_ships_inside_the_package():
    assert EXAMPLE_CONFIG.is_file()
    assert EXAMPLE_CONFIG.parent == Path(config.__file__).resolve().parent


def test_default_paths_use_user_directories(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "user_config_path", lambda app, appauthor, roaming: tmp_path / "cfg" / app)
    monkeypatch.setattr(config, "user_data_path", lambda app, appauthor, roaming: tmp_path / "data" / app)
    p = default_paths(env={})
    assert p == Paths(tmp_path / "cfg" / "tvpult" / "devices.toml", tmp_path / "data" / "tvpult")


def test_default_paths_never_next_to_the_code():
    p = default_paths(env={})
    code = Path(config.__file__).resolve().parents[2]
    assert code not in p.config_file.parents and code not in p.data_dir.parents


def test_env_overrides(tmp_path):
    p = default_paths(env={"TVPULT_CONFIG": str(tmp_path / "x.toml"), "TVPULT_DATA_DIR": str(tmp_path / "d")})
    assert p == Paths(tmp_path / "x.toml", tmp_path / "d")


def test_use_paths_partial_override(tmp_path):
    before = config.config_file()
    config.use_paths(data_dir=tmp_path / "elsewhere")
    assert config.data_dir() == tmp_path / "elsewhere" and config.config_file() == before


def test_token_and_address_book_follow_the_data_dir(tmp_path):
    from tvpult.config import TvConfig
    from tvpult.devices import Home

    config.use_paths(data_dir=tmp_path / "d")
    assert TvConfig(id="living", name="L", host="192.168.1.50").token_file == tmp_path / "d" / "token-living.txt"
    assert Home(Config()).book.path == tmp_path / "d" / "addresses.json"


def test_missing_config_is_an_empty_first_run():
    assert not config.config_file().exists()
    cfg = load_config()
    assert cfg == Config() and cfg.empty


def test_init_creates_from_example_and_never_overwrites():
    assert init_config() is True
    created = config.config_file()
    assert created.read_bytes() == EXAMPLE_CONFIG.read_bytes()
    created.write_text("# mine", encoding="utf-8")
    assert init_config() is False
    assert created.read_text(encoding="utf-8") == "# mine"


def test_cli_paths_and_init(capsys):
    from tvpult.cli import main

    assert main(["paths"]) == 0
    out = capsys.readouterr().out
    assert str(config.config_file()) in out and str(config.data_dir()) in out
    assert main(["init"]) == 0 and config.config_file().exists()


def test_cli_device_command_without_config_explains_what_to_do(capsys):
    from tvpult.cli import main

    assert main(["status"]) == 1
    assert "tvpult init" in capsys.readouterr().err


def test_cli_flags_override_paths(tmp_path, capsys):
    from tvpult.cli import main

    main(["--config", str(tmp_path / "c.toml"), "--data-dir", str(tmp_path / "dd"), "paths"])
    assert config.config_file() == tmp_path / "c.toml" and config.data_dir() == tmp_path / "dd"


def test_shortcut_runs_windowless_python_of_this_environment(tmp_path):
    from tvpult.cli import shortcut_target

    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert shortcut_target(str(tmp_path / "python.exe")) == (str(tmp_path / "pythonw.exe"), "-m tvpult app")


def test_shortcut_without_pythonw_is_an_error(tmp_path):
    from tvpult.cli import shortcut_target
    from tvpult.devices import DeviceError

    (tmp_path / "python.exe").write_bytes(b"")
    with pytest.raises(DeviceError):
        shortcut_target(str(tmp_path / "python.exe"))


def test_module_entry_point_exists():
    import importlib.util

    assert importlib.util.find_spec("tvpult.__main__") is not None


def test_version_flag(capsys):
    import pytest

    from tvpult.cli import main

    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.startswith("tvpult ")
