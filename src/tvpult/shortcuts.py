"""The shortcut on Linux and macOS (Windows: `cli._make_shortcut`, a .lnk made by PowerShell).

Linux (and other freedesktop systems): a `.desktop` file in the applications menu, and a copy on the desktop
when there is one. macOS: a small app bundle in ~/Applications whose executable is a shell script.
Both run `python -m tvpult app` of the environment tvpult runs in, without a terminal.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from xml.sax.saxutils import escape

from .icon import icns_bytes, png_bytes

BUNDLE_ID = "io.github.pavelmiskevich.tvpult"
DESKTOP_FILE = "tvpult.desktop"

# Desktop Entry spec, "The Exec key": an argument with any of these must be quoted.
_EXEC_RESERVED = re.compile(r"""[\s"'\\><~|&;$*?#()`]""")


def _exec_arg(arg: str) -> str:
    if arg and not _EXEC_RESERVED.search(arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def _desktop_value(value: str) -> str:
    """A Desktop Entry string value: backslash and control characters escaped."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def desktop_entry(python: str, args: list[str], name: str, comment: str, icon: Path, workdir: Path) -> str:
    """The text of the .desktop file."""
    command = " ".join(_exec_arg(a) for a in [python, *args]).replace("%", "%%")
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            f"Name={_desktop_value(name)}",
            f"Comment={_desktop_value(comment)}",
            f"Exec={_desktop_value(command)}",
            f"Path={_desktop_value(str(workdir))}",
            f"Icon={_desktop_value(str(icon))}",
            "Terminal=false",
            "Categories=AudioVideo;Utility;",
            "",
        ]
    )


def linux_dirs(env: dict[str, str] | None = None, home: Path | None = None) -> tuple[Path, Path | None]:
    """(applications menu folder, desktop folder or None), the XDG way."""
    env = os.environ if env is None else env
    home = home or Path.home()
    data = Path(env.get("XDG_DATA_HOME") or home / ".local/share")
    desktop: Path | None = home / "Desktop"
    user_dirs = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        for line in user_dirs.read_text(encoding="utf-8").splitlines():
            m = re.fullmatch(r'\s*XDG_DESKTOP_DIR="(.*)"\s*', line)
            if m:
                desktop = Path(m.group(1).replace("$HOME", str(home)))
    except (OSError, UnicodeDecodeError):
        pass
    if desktop is None or desktop == home or not desktop.is_dir():
        desktop = None
    return data / "applications", desktop


def make_linux_shortcut(
    python: str, args: list[str], name: str, comment: str, data_dir: Path, apps_dir: Path, desktop_dir: Path | None
) -> list[Path]:
    """Write the icon and the .desktop file(s); returns where they went."""
    icon = data_dir / "tvpult.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(png_bytes(128))
    text = desktop_entry(python, args, name, comment, icon, data_dir)
    made = []
    for folder in [apps_dir, desktop_dir]:
        if folder is None:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / DESKTOP_FILE
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)  # a launcher on the desktop has to be executable to start
        made.append(path)
    return made


def info_plist(name: str, version: str) -> str:
    def s(value: str) -> str:
        return f"<string>{escape(value)}</string>"

    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            "<dict>",
            f"  <key>CFBundleName</key>{s(name)}",
            f"  <key>CFBundleDisplayName</key>{s(name)}",
            f"  <key>CFBundleIdentifier</key>{s(BUNDLE_ID)}",
            f"  <key>CFBundleVersion</key>{s(version)}",
            f"  <key>CFBundleShortVersionString</key>{s(version)}",
            f"  <key>CFBundlePackageType</key>{s('APPL')}",
            f"  <key>CFBundleExecutable</key>{s('tvpult')}",
            f"  <key>CFBundleIconFile</key>{s('tvpult')}",
            # A server with no window of its own: the remote opens in the browser. No Dock icon for it.
            "  <key>LSUIElement</key><true/>",
            "</dict>",
            "</plist>",
            "",
        ]
    )


def launcher_script(python: str, args: list[str], workdir: Path) -> str:
    """The bundle's executable."""
    command = " ".join(shlex.quote(a) for a in [python, *args])
    return f"#!/bin/sh\ncd {shlex.quote(str(workdir))} || exit 1\nexec {command}\n"


def make_mac_app(python: str, args: list[str], name: str, version: str, data_dir: Path, apps_dir: Path) -> Path:
    """Write `<name>.app` into apps_dir (~/Applications); returns the bundle's path."""
    app = apps_dir / f"{name}.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(info_plist(name, version), encoding="utf-8", newline="\n")
    (contents / "Resources/tvpult.icns").write_bytes(icns_bytes())
    exe = contents / "MacOS/tvpult"
    exe.write_text(launcher_script(python, args, data_dir), encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    data_dir.mkdir(parents=True, exist_ok=True)
    return app
