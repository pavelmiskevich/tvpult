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

DEFAULT_PORT = 8765
LOG_MAX_BYTES = 1_000_000
SHORTCUT_NAME = "Пульт ТВ"
# Commands that work without any configured device.
NO_DEVICES_NEEDED = {"keys", "shortcut", "init", "paths", "serve", "app", "find"}


def _print_sightings(tvs: list[dict]) -> None:
    """`find --all`: every Samsung TV seen, configured or new; unconfirmed ones with the reason."""
    if not tvs:
        print("Телевизоров Samsung не найдено. Включены ли они? В той же ли сети ноутбук?")
        return
    for t in tvs:
        year = t["year"] or "?"
        who = f"настроен как {t['configured']}" if t["configured"] else "новый"
        mac = ":".join(t["mac"][i:i + 2] for i in range(0, 12, 2))
        mark = "" if t["confirmed"] else "  ⚠ " + t["note"]
        print(f"{t['host']:<15} {t['model'] or '?':<16} {year:<5} {mac}  {t['name']}  [{who}]{mark}")


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
                state = "не найден (выключен?); по прежнему адресу теперь другое устройство"
            elif not d["online"]:
                state = "выключен или не найден"
            elif d.get("power") != "on":
                state = f"режим ожидания ({d.get('power')})"
            else:
                state = "включён"
                if d.get("network"):
                    state += f", {d['network']}"
                if d.get("current_app"):
                    state += f", на экране: {d['current_app']}"
            pair = "" if d.get("paired") else "  [не сопряжён: tvpult pair " + d["id"] + "]"
            print(f"{d['id']:<6} {d['name']:<14} {d['host']:<15} {state}{pair}")
        else:
            if not d["online"]:
                print(f"{d['id']:<6} {d['name']:<14} {d['host']:<15} недоступен")
            else:
                mute = ", звук выкл" if d.get("muted") else ""
                app = f", играет: {d['app']}" if d.get("app") else ""
                print(f"{d['id']:<6} {d['name']:<14} {d['host']:<15} громкость {d.get('volume')}%{mute}{app}")


def _ready(home: Home, dev):
    """Before acting on a device: if it doesn't answer where we left it, search for it once."""
    if not home.ensure(dev):
        if getattr(dev, "wrong_device", False):
            raise DeviceError(f"{dev.cfg.name}: по адресу {dev.host} другое устройство, а наше не найдено")
        raise DeviceError(f"{dev.cfg.name} выключен или не найден в сети")
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
    raise DeviceError(f"рядом с {exe} нет pythonw — ярлык без окна консоли сделать не из чего")


def _make_shortcut(lan: bool = False) -> str:
    """Create «Пульт ТВ» on the desktop: starts the remote in its own window, stops when closed.

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
        raise DeviceError("ярлык не создан: " + (err or "PowerShell ничего не ответил"))
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
        f"$p = Join-Path $d {_ps_quote(SHORTCUT_NAME + '.lnk')}",
        # WScript.Shell saves through the ANSI code page, which may lack Cyrillic: save under an
        # ASCII name, then rename with PowerShell, which handles Unicode names.
        "$tmp = Join-Path $d 'tvpult-remote.lnk'",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($tmp)",
        f"$s.TargetPath = {_ps_quote(target)}",
        f"$s.Arguments = {_ps_quote(arguments)}",
        f"$s.WorkingDirectory = {_ps_quote(workdir)}",
        f"$s.IconLocation = {_ps_quote(f'{icon},0')}",
        f"$s.Description = {_ps_quote('Пульт для телевизоров и саундбара')}",
        "$s.Save()",
        "Move-Item -LiteralPath $tmp -Destination $p -Force",
        "Write-Output $p",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tvpult", description="Управление телевизорами и саундбаром дома")
    p.add_argument("--config", default=None, help="файл devices.toml (по умолчанию — в папке пользователя)")
    p.add_argument("--data-dir", default=None, help="папка для ключей сопряжения, адресов, журнала")
    p.add_argument("--addresses", default=None, help=argparse.SUPPRESS)  # address book path (tests)
    p.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # app mode without a window (tests)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="что включено и что на экране")
    s = sub.add_parser("find", help="найти устройства в сети (по MAC/UUID) и запомнить адреса")
    s.add_argument("--all", action="store_true", help="показать все Samsung TV в сети, а не только настроенные")
    s = sub.add_parser("pair", help="сопрячь ТВ с ноутбуком (на ТВ нажать «Разрешить»)")
    s.add_argument("tv")
    s = sub.add_parser("on", help="включить ТВ")
    s.add_argument("tv")
    s = sub.add_parser("off", help="выключить ТВ")
    s.add_argument("tv")
    s = sub.add_parser("key", help="нажать кнопки пульта: tvpult key living home right ok")
    s.add_argument("tv")
    s.add_argument("keys", nargs="+")
    s = sub.add_parser("apps", help="какие приложения из списка есть на ТВ")
    s.add_argument("tv")
    s = sub.add_parser("text", help="ввести текст в открытое на ТВ поле: tvpult text living \"поиск\"")
    s.add_argument("tv")
    s.add_argument("text")
    s.add_argument("--no-submit", action="store_true", help="не нажимать «Готово»")
    s = sub.add_parser("discover", help="спросить у ТВ полный список приложений с id")
    s.add_argument("tv")
    s.add_argument("--grep", help="показать только совпадающие по названию")
    s = sub.add_parser("launch", help="открыть приложение: tvpult launch living youtube")
    s.add_argument("tv")
    s.add_argument("app")
    s = sub.add_parser("vol", help="громкость саундбара: 25, +5, -5, mute, unmute")
    s.add_argument("value")
    s.add_argument("--bar")
    sub.add_parser("keys", help="список разрешённых кнопок")
    for name, help_ in (("serve", "запустить страничку-пульт (до Ctrl+C)"),
                        ("app", "пульт отдельным окном; закрыл окно — всё остановилось (для ярлыка)")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--port", type=int, default=DEFAULT_PORT)
        s.add_argument("--tailnet", action="store_true", help="открыть ещё и в сети Tailscale")
        s.add_argument("--lan", action="store_true", help="открыть для телефонов в домашней сети (вход по PIN)")
    s = sub.add_parser("shortcut", help=f"создать ярлык «{SHORTCUT_NAME}» на рабочем столе")
    s.add_argument("--lan", action="store_true", help="ярлык откроет пульт и для телефонов в домашней сети (PIN)")
    sub.add_parser("init", help="создать devices.toml из примера в папке пользователя")
    sub.add_parser("paths", help="где лежат настройки и данные")

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
        print("Псевдонимы:", ", ".join(sorted(KEY_ALIASES)))
        print("Кнопки:", ", ".join(sorted(ALLOWED_KEYS)))
        return 0
    if a.cmd == "shortcut":
        try:
            print("Ярлык создан:", _make_shortcut(lan=a.lan))
            return 0
        except DeviceError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
    if a.cmd == "paths":
        print("Настройки:", paths.config_file(), "" if paths.config_file().exists() else "(ещё нет — tvpult init)")
        print("Данные:   ", paths.data_dir())
        return 0
    if a.cmd == "init":
        if init_config():
            print("Создан", paths.config_file(), "— впиши туда MAC своих ТВ (Настройки > Об этом ТВ)")
        else:
            print("Уже есть", paths.config_file(), "— не трогаю")
        return 0
    if a.cmd == "app" and _our_server_running(a.port):
        _open_remote(f"http://127.0.0.1:{a.port}/")  # already running: just show it
        return 0

    _no_proxy_for_lan()
    cfg = load_config()
    if cfg.empty and a.cmd not in NO_DEVICES_NEEDED:
        print(f"Устройства не настроены: {paths.config_file()} не найден или пуст. Начни с `tvpult init`.",
              file=sys.stderr)
        return 1
    home = Home(cfg, keep_alive=a.cmd in ("serve", "app"),
                book_path=Path(a.addresses) if a.addresses else None)
    try:
        if a.cmd == "status":
            _print_status(home.status(rediscover="wait"))
        elif a.cmd == "find":
            print("Ищу устройства в сетях:", ", ".join(str(n) for n in home.subnets()) or "—")
            if a.all or cfg.empty:
                _print_sightings(home.scan_tvs())
                return 0
            found = home.relocate(force=True)
            for d in home.devices():
                where = found.get(d.cfg.id)
                how = {"mac": "по MAC", "uuid": "по UUID", "arp": "по MAC из ARP"}.get(where.how) if where else None
                line = f"{d.host:<15} найден {how}" if where else "не найден (выключен?)"
                print(f"{d.cfg.id:<6} {d.cfg.name:<14} {line}")
        elif a.cmd == "pair":
            tv = _ready(home, home.tv(a.tv))
            print("Смотри на экран ТВ и нажми «Разрешить» (около 30 секунд)…")
            tv.pair()
            print("Готово: ТВ сопряжён.")
        elif a.cmd == "on":
            print(home.tv(a.tv).power_on())
        elif a.cmd == "off":
            print(_ready(home, home.tv(a.tv)).power_off())
        elif a.cmd == "key":
            print("нажато:", " ".join(_ready(home, home.tv(a.tv)).send_keys(a.keys)))
        elif a.cmd == "apps":
            for app in _ready(home, home.tv(a.tv)).installed_apps(refresh=True):
                print(f"{app['alias']:<10} {app['id']:<16} {app['name']}")
        elif a.cmd == "text":
            _ready(home, home.tv(a.tv)).send_text(a.text, submit=not a.no_submit)
            print("текст отправлен")
        elif a.cmd == "discover":
            apps = _ready(home, home.tv(a.tv)).discover_apps()
            if apps is None:
                print("ТВ не прислал список (модели 2022+ его не отдают). Используй tvpult apps.", file=sys.stderr)
                return 1
            pat = (a.grep or "").lower()
            for app in sorted(apps, key=lambda x: x["name"].lower()):
                if pat and pat not in app["name"].lower():
                    continue
                print(f"{app['id']:<36} {app['name']}")
        elif a.cmd == "launch":
            print("открыто:", _ready(home, home.tv(a.tv)).launch(a.app))
        elif a.cmd == "vol":
            bar = _ready(home, home.bar(a.bar))
            v = a.value.lower()
            if v in ("mute", "unmute"):
                print("звук выключен" if bar.set_muted(v == "mute") else "звук включён")
            elif v[0] in "+-":
                print(f"громкость {bar.change_volume(int(v))}%")
            else:
                print(f"громкость {bar.set_volume(int(v))}%")
        elif a.cmd in ("serve", "app"):
            from .server import Presence, serve

            binds, names = ["127.0.0.1"], set()
            if a.tailnet:
                ip, name = _tailscale_self()
                if not ip:
                    print("Tailscale не найден или не подключён.", file=sys.stderr)
                    return 2
                binds.append(ip)
                if name:
                    names.add(name)
                    print(f"  в Tailscale также: http://{name}:{a.port}/")
            lan, auth = None, None
            if a.lan:  # phones on the home network, with PIN sign-in
                from . import discovery
                from .auth import LanAuth

                lan = discovery.default_local_ip()
                if not lan or not discovery.is_private_lan(ipaddress.IPv4Address(lan)):
                    print("Не нашёл адрес ноутбука в домашней сети.", file=sys.stderr)
                    return 2
                auth = LanAuth(paths.data_dir() / "sessions.json")
            url = f"http://127.0.0.1:{a.port}/"
            if a.cmd == "app":
                print("Пульт открыт; закрой окно пульта — всё остановится:")
                serve(home, binds, a.port, names, Presence(idle_exit=True),
                      on_ready=None if a.no_browser else (lambda: _open_remote(url)), lan=lan, auth=auth)
            else:
                print("Пульт открыт (Ctrl+C — остановить):")
                serve(home, binds, a.port, names, lan=lan, auth=auth)
        return 0
    except UnknownKey as e:
        print(f"Нет такой кнопки: {e}. Список: tvpult keys", file=sys.stderr)
        return 2
    except (DeviceError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        home.close()


if __name__ == "__main__":
    raise SystemExit(main())
