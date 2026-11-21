"""#35: `tvpult shortcut` on Linux (.desktop) and macOS (an app in ~/Applications), and finding a browser there.

The platform is passed in, so every system CI runs on checks all three.
"""

import plistlib
import struct
import sys
from pathlib import Path, PurePosixPath

import pytest

from tvpult import cli, shortcuts
from tvpult.i18n import use_lang


def _entry(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def test_desktop_entry_runs_this_python_without_a_terminal():
    data = PurePosixPath("/home/ann/.local/share/tvpult")
    text = shortcuts.desktop_entry(
        "/home/ann/.venv/bin/python", ["-m", "tvpult", "app"], "TV Remote", "Remote", data / "tvpult.png", data
    )
    assert text.startswith("[Desktop Entry]\n") and text.endswith("\n")
    e = _entry(text)
    assert e["Type"] == "Application" and e["Terminal"] == "false"
    assert e["Exec"] == "/home/ann/.venv/bin/python -m tvpult app"
    assert e["Name"] == "TV Remote" and e["Icon"] == f"{data}/tvpult.png" and e["Path"] == str(data)


def test_desktop_entry_quotes_what_the_spec_reserves(tmp_path):
    text = shortcuts.desktop_entry(
        '/home/o"neil/my $dir/100%/py', ["-m", "tvpult", "app", "--lan"], "Пульт ТВ", "x", tmp_path / "i.png", tmp_path
    )
    e = _entry(text)
    # quoted for the space, `"` and `$` escaped inside, `%` doubled, then the backslashes escaped as a string value
    assert e["Exec"] == '"/home/o\\\\"neil/my \\\\$dir/100%%/py" -m tvpult app --lan'
    assert e["Name"] == "Пульт ТВ"


def test_linux_shortcut_goes_to_the_menu_and_the_desktop(tmp_path):
    apps, desktop = tmp_path / "apps", tmp_path / "Desktop"
    desktop.mkdir()
    made = shortcuts.make_linux_shortcut(
        "/usr/bin/python3", ["-m", "tvpult", "app"], "TV", "c", tmp_path, apps, desktop
    )
    assert made == [apps / "tvpult.desktop", desktop / "tvpult.desktop"]
    assert made[0].read_bytes() == made[1].read_bytes() and b"\r\n" not in made[0].read_bytes()
    assert (tmp_path / "tvpult.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    if sys.platform != "win32":
        assert made[1].stat().st_mode & 0o111  # a desktop launcher must be executable


def test_linux_shortcut_without_a_desktop_folder(tmp_path):
    made = shortcuts.make_linux_shortcut("/usr/bin/python3", ["app"], "TV", "c", tmp_path, tmp_path / "apps", None)
    assert made == [tmp_path / "apps" / "tvpult.desktop"]


def test_linux_dirs_follow_xdg(tmp_path):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / "Рабочий стол").mkdir()
    (home / ".config/user-dirs.dirs").write_text('# comment\nXDG_DESKTOP_DIR="$HOME/Рабочий стол"\n', encoding="utf-8")
    apps, desktop = shortcuts.linux_dirs({}, home)
    assert apps == home / ".local/share/applications" and desktop == home / "Рабочий стол"
    apps, desktop = shortcuts.linux_dirs({"XDG_DATA_HOME": str(tmp_path / "data")}, tmp_path / "nobody")
    assert apps == tmp_path / "data/applications" and desktop is None  # no desktop folder: menu only


def test_mac_app_bundle(tmp_path):
    app = shortcuts.make_mac_app(
        "/Users/o'neil/.venv/bin/python", ["-m", "tvpult", "app"], "Пульт ТВ", "0.1.0", tmp_path / "data", tmp_path
    )
    assert app == tmp_path / "Пульт ТВ.app"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "tvpult" and info["CFBundleName"] == "Пульт ТВ"
    assert info["LSUIElement"] is True and info["CFBundleVersion"] == "0.1.0"
    script = (app / "Contents/MacOS/tvpult").read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\n") and "\r" not in script
    assert "exec '/Users/o'\"'\"'neil/.venv/bin/python' -m tvpult app\n" in script
    icns = (app / "Contents/Resources/tvpult.icns").read_bytes()
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns) and icns[8:12] == b"ic07"
    assert icns[16:24] == b"\x89PNG\r\n\x1a\n"
    again = shortcuts.make_mac_app("/py", ["app"], "Пульт ТВ", "0.1.0", tmp_path / "data", tmp_path)
    assert again == app and "exec /py app" in (app / "Contents/MacOS/tvpult").read_text(encoding="utf-8")


def test_outside_windows_the_shortcut_runs_this_python():
    assert cli.shortcut_target("/opt/venv/bin/python", platform="linux") == ("/opt/venv/bin/python", "-m tvpult app")
    assert cli.shortcut_target("/opt/venv/bin/python", platform="darwin") == ("/opt/venv/bin/python", "-m tvpult app")


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def test_make_shortcut_on_linux(home_dir):
    use_lang("en")
    (home_dir / "Desktop").mkdir()
    made = cli._make_shortcut(lan=True, platform="linux")
    assert made == [
        str(home_dir / ".local/share/applications/tvpult.desktop"),
        str(home_dir / "Desktop/tvpult.desktop"),
    ]
    e = _entry(Path(made[0]).read_text(encoding="utf-8"))
    assert e["Exec"].endswith(" -m tvpult app --lan") and e["Name"] == "TV Remote"


def test_make_shortcut_on_mac(home_dir):
    use_lang("en")
    made = cli._make_shortcut(platform="darwin")
    assert made == [str(home_dir / "Applications/TV Remote.app")]
    assert (home_dir / "Applications/TV Remote.app/Contents/MacOS/tvpult").is_file()


def test_a_folder_that_cant_be_written_is_one_error(home_dir):
    (home_dir / ".local").write_text("not a folder", encoding="utf-8")
    with pytest.raises(cli.DeviceError) as e:
        cli._make_shortcut(platform="linux")
    assert e.value.code == "shortcut"


@pytest.mark.parametrize(("platform", "hint"), [("linux", "Allow Launching"), ("darwin", "Launchpad")])
def test_cli_prints_every_place_and_a_hint(monkeypatch, capsys, platform, hint):
    use_lang("en")
    monkeypatch.setattr(cli, "_make_shortcut", lambda lan=False: ["/a/tvpult.desktop", "/b/tvpult.desktop"])
    monkeypatch.setattr(sys, "platform", platform)
    assert cli.main(["shortcut"]) == 0
    out = capsys.readouterr().out
    assert "Shortcut created: /a/tvpult.desktop" in out and "Shortcut created: /b/tvpult.desktop" in out
    assert hint in out


def test_linux_browser_is_found_on_the_path(monkeypatch):
    found = {"chromium": "/usr/bin/chromium"}
    monkeypatch.setattr(cli.shutil, "which", lambda name: found.get(name))
    assert cli._browser("linux") == "/usr/bin/chromium"
    found.clear()
    assert cli._browser("linux") is None  # an ordinary browser tab then


def test_mac_browser_in_the_users_applications(home_dir, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    exe = home_dir / "Applications/Chromium.app/Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    if Path("/Applications/Google Chrome.app").exists():  # a real Mac with Chrome finds that first
        pytest.skip("Chrome is installed system-wide here")
    assert cli._browser("darwin") == str(exe)
