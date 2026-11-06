"""tvpult — command line for the home TVs and soundbar."""

from __future__ import annotations

import argparse
import base64
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as paths
from .catalog import ALLOWED_KEYS, KEY_ALIASES, UnknownKey
from .config import init_config, load_config
from .devices import DeviceError, Home
from .i18n import t

DEFAULT_PORT = 8765
LOG_MAX_BYTES = 1_000_000
# Commands that work without any configured device.
NO_DEVICES_NEEDED = {"keys", "catalog", "shortcut", "init", "paths", "serve", "app", "find"}


def shortcut_name() -> str:
    """The desktop shortcut's name, in the system language: "TV Remote" / «Пульт ТВ»."""
    return t("shortcut.name")


def _print_sightings(tvs: list[dict]) -> None:
    """`find --all`: every Samsung TV seen, configured or new; unconfirmed ones with the reason."""
    if not tvs:
        print(t("cli.no_tvs"))
        return
    for tv in tvs:
        year = tv["year"] or "?"
        who = t("cli.configured_as", id=tv["configured"]) if tv["configured"] else t("cli.new")
        mac = ":".join(tv["mac"][i:i + 2] for i in range(0, 12, 2))
        mark = "" if tv["confirmed"] else "  ⚠ " + tv["note"]
        print(f"{tv['host']:<15} {tv['model'] or '?':<16} {year:<5} {mac}  {tv['name']}  [{who}]{mark}")


def _print_catalog() -> None:
    """`tvpult catalog`: every app tvpult looks for, e.g. `kinopoisk  Кинопоиск  RU  111399000037 (2017, 2022)`."""
    from . import i18n
    from .catalog import catalog

    for app in catalog().values():
        ids = "  ".join(i.id + (f" ({', '.join(map(str, i.seen))})" if i.seen else "") for i in app.ids)
        print(f"{app.alias:<10} {app.name(i18n.lang()):<14} {','.join(app.regions):<6} {ids}")


def log_file() -> Path:
    return paths.data_dir() / "tvpult.log"


def _trim_log() -> None:
    """Start the log afresh when it grows too big (missing or locked file: nothing to do)."""
    with contextlib.suppress(OSError):
        if log_file().stat().st_size > LOG_MAX_BYTES:
            log_file().unlink()


# Devices can move to any private address (see discovery), so all private ranges bypass proxies.
_LAN_NO_PROXY = ["localhost", "127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


def _no_proxy_for_lan() -> None:
    """websocket-client honours *_proxy variables; LAN traffic must never go through a proxy."""
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    merged = ",".join(dict.fromkeys(filter(None, [*current.split(","), *_LAN_NO_PROXY])))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged


def _windowless_output() -> None:
    """Started without a console (desktop shortcut via pythonw): print and log into a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    _trim_log()
    stream = open(log_file(), "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — stays open for the process
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def _tailscale_self() -> tuple[str | None, str | None]:
    exe = shutil.which("tailscale")
    if not exe:
        return None, None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10, check=True)
        me = json.loads(out.stdout).get("Self", {})
        ips = [ip for ip in me.get("TailscaleIPs", []) if "." in ip]
        name = (me.get("DNSName") or "").rstrip(".") or None
        return (ips[0] if ips else None), name
    except (subprocess.SubprocessError, ValueError, OSError):
        return None, None


def _print_status(devs: list[dict]) -> None:
    for d in devs:
        if d["kind"] == "tv":
            if d.get("wrong_device"):
                state = t("cli.st.wrong_device")
            elif not d["online"]:
                state = t("cli.st.off")
            elif d.get("power") != "on":
                state = t("cli.st.standby", power=d.get("power"))
            else:
                state = t("cli.st.on")
                if d.get("wired") or d.get("network"):
                    state += ", " + (t("cli.st.wired") if d.get("wired") else d["network"])
                if d.get("current_app"):
                    state += ", " + t("cli.st.on_screen", app=d["current_app"])
            pair = "" if d.get("paired") else "  [" + t("cli.st.not_paired", id=d["id"]) + "]"
            print(f"{d['id']:<6} {d['name']:<14} {d['host']:<15} {state}{pair}")
        else:
            if not d["online"]:
                print(f"{d['id']:<6} {d['name']:<14} {d['host']:<15} {t('cli.st.unreachable')}")
            else:
                mute = ", " + t("cli.st.muted") if d.get("muted") else ""
                app = ", " + t("cli.st.playing", app=d["app"]) if d.get("app") else ""
                vol = t("cli.st.volume", volume=d.get("volume"))
                print(f"{d['id']:<6} {d['name']:<14} {d['host']:<15} {vol}{mute}{app}")


def _ready(home: Home, dev):
    """Before acting on a device: if it doesn't answer where we left it, search for it once."""
    if not home.ensure(dev):
        if getattr(dev, "wrong_device", False):
            raise DeviceError(t("cli.err.wrong_device", name=dev.cfg.name, host=dev.host))
        raise DeviceError(t("cli.err.offline", name=dev.cfg.name))
    return dev


def _our_server_running(port: int) -> bool:
    import requests

    s = requests.Session()
    s.trust_env = False
    try:
        r = s.get(f"http://127.0.0.1:{port}/", timeout=2)
        return r.ok and r.headers.get("Server", "").startswith("tvpult")
    except requests.RequestException:
        return False


def _browser() -> str | None:
    """Edge or Chrome, to open the remote as its own app window."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("msedge") or shutil.which("chrome")


def _open_remote(url: str) -> None:
    exe = _browser()
    if exe:
        subprocess.Popen([exe, f"--app={url}", "--window-size=440,940"], close_fds=True)
    else:
        import webbrowser

        webbrowser.open(url)


def shortcut_target(executable: str | None = None) -> tuple[str, str]:
    """What the desktop shortcut runs: the windowless Python of *this* environment with `-m tvpult app`.

    Works the same for a source checkout (`uv run`) and an installed tool (`uv tool install`), and
    doesn't depend on where the code lives.
    """
    exe = Path(executable or sys.executable)
    for name in ("pythonw.exe", "pythonw"):
        candidate = exe.with_name(name)
        if candidate.is_file():
            return str(candidate), "-m tvpult app"
    raise DeviceError(t("cli.err.no_pythonw", exe=exe))


def _make_shortcut(lan: bool = False) -> str:
    """Create the desktop shortcut ("TV Remote" / «Пульт ТВ», by the system language).

    It starts the remote in its own window and stops when that is closed.

    lan: the shortcut also opens the remote to phones on the home network (PIN sign-in).
    """
    from .icon import write_icon

    target, arguments = shortcut_target()
    if lan:
        arguments += " --lan"
    icon = paths.data_dir() / "tvpult.ico"
    write_icon(icon)
    script = shortcut_script(target, arguments, paths.data_dir(), icon)
    # -EncodedCommand (UTF-16LE base64): the Cyrillic name survives; a plain -Command argument
    # turned it into '?', which Windows refuses in file names.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                         capture_output=True, timeout=30)
    path = out.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    if out.returncode != 0 or not Path(path[0]).is_file():
        err = out.stderr.decode("utf-8", "replace").strip() or out.stdout.decode("utf-8", "replace").strip()
        raise DeviceError(t("cli.err.shortcut", error=err or t("cli.err.powershell_silent")))
    return path[0]


def _ps_quote(value: object) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def shortcut_script(target: str, arguments: str, workdir: Path, icon: Path) -> str:
    """PowerShell that creates the desktop shortcut and prints its path (UTF-8)."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "$d = [Environment]::GetFolderPath('Desktop')",
        f"$p = Join-Path $d {_ps_quote(shortcut_name() + '.lnk')}",
        # WScript.Shell saves through the ANSI code page, which may lack Cyrillic: save under an
        # ASCII name, then rename with PowerShell, which handles Unicode names.
        "$tmp = Join-Path $d 'tvpult-remote.lnk'",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($tmp)",
        f"$s.TargetPath = {_ps_quote(target)}",
        f"$s.Arguments = {_ps_quote(arguments)}",
        f"$s.WorkingDirectory = {_ps_quote(workdir)}",
        f"$s.IconLocation = {_ps_quote(f'{icon},0')}",
        f"$s.Description = {_ps_quote(t('shortcut.description'))}",
        "$s.Save()",
        "Move-Item -LiteralPath $tmp -Destination $p -Force",
        "Write-Output $p",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tvpult", description=t("cli.description"))
    p.add_argument("--config", default=None, help=t("cli.opt.config"))
    p.add_argument("--data-dir", default=None, help=t("cli.opt.data_dir"))
    p.add_argument("--addresses", default=None, help=argparse.SUPPRESS)  # address book path (tests)
    p.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # app mode without a window (tests)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help=t("cli.cmd.status"))
    s = sub.add_parser("find", help=t("cli.cmd.find"))
    s.add_argument("--all", action="store_true", help=t("cli.cmd.find_all"))
    s = sub.add_parser("pair", help=t("cli.cmd.pair"))
    s.add_argument("tv")
    s = sub.add_parser("on", help=t("cli.cmd.on"))
    s.add_argument("tv")
    s = sub.add_parser("off", help=t("cli.cmd.off"))
    s.add_argument("tv")
    s = sub.add_parser("key", help=t("cli.cmd.key"))
    s.add_argument("tv")
    s.add_argument("keys", nargs="+")
    s = sub.add_parser("apps", help=t("cli.cmd.apps"))
    s.add_argument("tv")
    s = sub.add_parser("text", help=t("cli.cmd.text"))
    s.add_argument("tv")
    s.add_argument("text")
    s.add_argument("--no-submit", action="store_true", help=t("cli.cmd.no_submit"))
    s = sub.add_parser("discover", help=t("cli.cmd.discover"))
    s.add_argument("tv")
    s.add_argument("--grep", help=t("cli.cmd.grep"))
    s = sub.add_parser("launch", help=t("cli.cmd.launch"))
    s.add_argument("tv")
    s.add_argument("app")
    s = sub.add_parser("vol", help=t("cli.cmd.vol"))
    s.add_argument("value")
    s.add_argument("--bar")
    sub.add_parser("keys", help=t("cli.cmd.keys"))
    sub.add_parser("catalog", help=t("cli.cmd.catalog"))
    for name in ("serve", "app"):
        s = sub.add_parser(name, help=t(f"cli.cmd.{name}"))
        s.add_argument("--port", type=int, default=DEFAULT_PORT)
        s.add_argument("--tailnet", action="store_true", help=t("cli.cmd.tailnet"))
        s.add_argument("--lan", action="store_true", help=t("cli.cmd.lan"))
    s = sub.add_parser("shortcut", help=t("cli.cmd.shortcut", name=shortcut_name()))
    s.add_argument("--lan", action="store_true", help=t("cli.cmd.shortcut_lan"))
    sub.add_parser("init", help=t("cli.cmd.init"))
    sub.add_parser("paths", help=t("cli.cmd.paths"))

    a = p.parse_args(argv)
    paths.use_paths(a.config, a.data_dir)
    _windowless_output()
    logging.basicConfig(level=logging.INFO if (a.verbose or a.cmd == "app") else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr, force=True)
    if a.cmd == "app":  # started from the shortcut: no console to read, so always keep a log file
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        _trim_log()
        fh = logging.FileHandler(log_file(), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.info("tvpult app starting (port %s)", a.port)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if a.cmd == "keys":
        print(t("cli.aliases"), ", ".join(sorted(KEY_ALIASES)))
        print(t("cli.keys"), ", ".join(sorted(ALLOWED_KEYS)))
        return 0
    if a.cmd == "catalog":
        _print_catalog()
        return 0
    if a.cmd == "shortcut":
        try:
            print(t("cli.shortcut_made"), _make_shortcut(lan=a.lan))
            return 0
        except DeviceError as e:
            print(t("cli.error", error=e), file=sys.stderr)
            return 1
    if a.cmd == "paths":
        missing = "" if paths.config_file().exists() else t("cli.paths.missing")
        print(t("cli.paths.config"), paths.config_file(), missing)
        print(t("cli.paths.data"), paths.data_dir())
        return 0
    if a.cmd == "init":
        if init_config():
            print(t("cli.init.created", path=paths.config_file()))
        else:
            print(t("cli.init.exists", path=paths.config_file()))
        return 0
    if a.cmd == "app" and _our_server_running(a.port):
        _open_remote(f"http://127.0.0.1:{a.port}/")  # already running: just show it
        return 0

    _no_proxy_for_lan()
    cfg = load_config()
    if cfg.empty and a.cmd not in NO_DEVICES_NEEDED:
        print(t("cli.not_configured", path=paths.config_file()), file=sys.stderr)
        return 1
    home = Home(cfg, keep_alive=a.cmd in ("serve", "app"),
                book_path=Path(a.addresses) if a.addresses else None)
    try:
        if a.cmd == "status":
            _print_status(home.status(rediscover="wait"))
        elif a.cmd == "find":
            print(t("cli.find.networks"), ", ".join(str(n) for n in home.subnets()) or "—")
            if a.all or cfg.empty:
                _print_sightings(home.scan_tvs())
                return 0
            found = home.relocate(force=True)
            for d in home.devices():
                where = found.get(d.cfg.id)
                if where:
                    how = t(f"cli.find.by.{where.how}") if where.how in ("mac", "uuid", "arp") else where.how
                    line = f"{d.host:<15} " + t("cli.find.found", how=how)
                else:
                    line = t("cli.find.missing")
                print(f"{d.cfg.id:<6} {d.cfg.name:<14} {line}")
        elif a.cmd == "pair":
            tv = _ready(home, home.tv(a.tv))
            print(t("cli.pair.look"))
            tv.pair()
            print(t("cli.pair.done"))
        elif a.cmd == "on":
            print(home.tv(a.tv).power_on())
        elif a.cmd == "off":
            print(_ready(home, home.tv(a.tv)).power_off())
        elif a.cmd == "key":
            print(t("cli.pressed"), " ".join(_ready(home, home.tv(a.tv)).send_keys(a.keys)))
        elif a.cmd == "apps":
            for app in _ready(home, home.tv(a.tv)).installed_apps(refresh=True):
                print(f"{app['alias']:<10} {app['id']:<16} {app['name']}")
        elif a.cmd == "text":
            _ready(home, home.tv(a.tv)).send_text(a.text, submit=not a.no_submit)
            print(t("cli.text_sent"))
        elif a.cmd == "discover":
            apps = _ready(home, home.tv(a.tv)).discover_apps()
            if apps is None:
                print(t("cli.discover.none"), file=sys.stderr)
                return 1
            pat = (a.grep or "").lower()
            for app in sorted(apps, key=lambda x: x["name"].lower()):
                if pat and pat not in app["name"].lower():
                    continue
                print(f"{app['id']:<36} {app['name']}")
        elif a.cmd == "launch":
            print(t("cli.opened"), _ready(home, home.tv(a.tv)).launch(a.app))
        elif a.cmd == "vol":
            bar = _ready(home, home.bar(a.bar))
            v = a.value.lower()
            if v in ("mute", "unmute"):
                print(t("cli.vol.muted") if bar.set_muted(v == "mute") else t("cli.vol.unmuted"))
            elif v[0] in "+-":
                print(t("cli.vol.level", volume=bar.change_volume(int(v))))
            else:
                print(t("cli.vol.level", volume=bar.set_volume(int(v))))
        elif a.cmd in ("serve", "app"):
            from .server import Presence, serve

            binds, names = ["127.0.0.1"], set()
            if a.tailnet:
                ip, name = _tailscale_self()
                if not ip:
                    print(t("cli.no_tailscale"), file=sys.stderr)
                    return 2
                binds.append(ip)
                if name:
                    names.add(name)
                    print(t("cli.tailnet_also", url=f"http://{name}:{a.port}/"))
            lan, auth = None, None
            if a.lan:  # phones on the home network, with PIN sign-in
                from . import discovery
                from .auth import LanAuth

                lan = discovery.default_local_ip()
                if not lan or not discovery.is_private_lan(ipaddress.IPv4Address(lan)):
                    print(t("cli.no_lan_ip"), file=sys.stderr)
                    return 2
                auth = LanAuth(paths.data_dir() / "sessions.json")
            url = f"http://127.0.0.1:{a.port}/"
            if a.cmd == "app":
                print(t("cli.app_open"))
                serve(home, binds, a.port, names, Presence(idle_exit=True),
                      on_ready=None if a.no_browser else (lambda: _open_remote(url)), lan=lan, auth=auth)
            else:
                print(t("cli.serve_open"))
                serve(home, binds, a.port, names, lan=lan, auth=auth)
        return 0
    except UnknownKey as e:
        print(t("cli.unknown_key", key=e), file=sys.stderr)
        return 2
    except (DeviceError, ValueError) as e:
        print(t("cli.error", error=e), file=sys.stderr)
        return 1
    finally:
        home.close()


if __name__ == "__main__":
    raise SystemExit(main())
