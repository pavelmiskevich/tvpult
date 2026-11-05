"""Talking to the actual devices: Samsung TVs (REST + WebSocket) and a Cast soundbar.

Each device has a current address (`host`) that starts from devices.toml, is overridden by the
address book (.data/addresses.json) and is changed by discovery when the device moves. Before a
device is used, its identity is checked (TV: Wi-Fi MAC; soundbar: Cast UUID), so commands never go
to whatever else happens to answer at an old address.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from . import config as paths
from . import discovery
from .catalog import app_catalog, normalize_key
from .config import Config, SoundbarConfig, TvConfig
from .wol import send_wol, subnet_broadcast

log = logging.getLogger(__name__)

REMOTE_NAME = "tvpult"
APP_CACHE_SECONDS = 600
IDENTITY_CACHE_SECONDS = 60
RESCAN_SECONDS = 60
SEARCH_SECONDS = 10  # a search asked for by hand (settings page)


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DeviceError(RuntimeError):
    pass


class _Identity:
    """Remembers that the device at a host was checked and is the right one."""

    def __init__(self):
        self.host: str | None = None
        self.at = 0.0
        self.wrong_host: str | None = None

    def fresh(self, host: str) -> bool:
        return self.host == host and time.time() - self.at < IDENTITY_CACHE_SECONDS

    def ok(self, host: str) -> None:
        self.host, self.at, self.wrong_host = host, time.time(), None

    def wrong(self, host: str) -> None:
        self.host, self.at, self.wrong_host = None, 0.0, host

    def reset(self) -> None:
        self.host, self.at, self.wrong_host = None, 0.0, None


class SamsungTv:
    def __init__(self, cfg: TvConfig, extra_apps: dict[str, list[str]] | None = None,
                 wifi_names: dict[str, str] | None = None, keep_alive: bool = False,
                 host: str | None = None):
        self.cfg = cfg
        self.host = host or cfg.host
        self._wifi_names = wifi_names or {}
        # Only the long-running server pre-opens the remote channel. One-shot CLI
        # commands must not: every new connection can make the TV ask "Allow?" again.
        self.keep_alive = keep_alive
        self._catalog = app_catalog(extra_apps)
        self._http = requests.Session()
        self._http.trust_env = False  # never route LAN calls through a proxy
        self._lock = threading.Lock()
        self._ws = None
        self._apps: list[dict[str, str]] | None = None
        self._apps_at = 0.0
        self._identity = _Identity()
        self._arp_fn = discovery.read_arp

    # --- address ----------------------------------------------------------------
    def set_host(self, host: str) -> None:
        """Move to a new address: drop the old connection and everything learnt there."""
        with self._lock:
            if host == self.host:
                return
            if self._ws is not None:
                with contextlib.suppress(Exception):  # already broken: closing may fail, that's fine
                    self._ws.close()
            self._ws = None
            self.host = host
            self._apps, self._apps_at = None, 0.0
            self._identity.reset()

    @property
    def wrong_device(self) -> bool:
        """True when the last check found a different device at our address."""
        return self._identity.wrong_host == self.host

    # --- REST (no pairing needed) ---------------------------------------------
    def _rest(self, path: str, timeout: float = 3.0) -> dict[str, Any] | None:
        r = self._http.get(f"http://{self.host}:8001/api/v2/{path}", timeout=timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def info(self) -> dict[str, Any]:
        data = self._rest("") or {}
        return data.get("device", {})

    def _check_identity(self, dev: dict[str, Any]) -> bool:
        want = discovery.normalize_mac(self.cfg.mac)
        if not want:  # no MAC configured: nothing to check against
            self._identity.ok(self.host)
            return True
        claimed = discovery.normalize_mac(dev.get("wifiMac"))
        if claimed != want:
            log.warning("%s: %s is a different device (MAC %s)", self.cfg.id, self.host, dev.get("wifiMac"))
            self._identity.wrong(self.host)
            return False
        # The claim is the device's own answer; the ARP table must agree before the pairing token or
        # any keys/text go there (a LAN device could otherwise pose as our TV).
        seen = discovery.arp_mac(self.host, self._arp_fn)
        if not seen or seen not in {want, discovery.normalize_mac(self.cfg.lan_mac)}:
            log.warning("%s: %s claims our MAC but ARP disagrees — not trusted", self.cfg.id, self.host)
            self._identity.wrong(self.host)
            return False
        self._identity.ok(self.host)
        return True

    def online(self) -> bool:
        """Reachable *and* it is our TV."""
        if not _port_open(self.host, 8001):
            return False
        if self._identity.fresh(self.host):
            return True
        try:
            return self._check_identity(self.info())
        except (requests.RequestException, ValueError):
            return False

    def installed_apps(self, refresh: bool = False) -> list[dict[str, str]]:
        if not refresh and self._apps is not None and time.time() - self._apps_at < APP_CACHE_SECONDS:
            return self._apps
        found = []
        for alias, ids in self._catalog.items():
            for app_id in ids:
                try:
                    st = self._rest(f"applications/{app_id}")
                except requests.RequestException:
                    continue
                if st:
                    found.append({"alias": alias, "id": app_id, "name": st.get("name") or alias})
                    break
        have = {a["alias"] for a in found}
        for alias, app_id in self.cfg.assume_apps.items():
            if alias not in have:
                found.append({"alias": alias, "id": app_id, "name": alias})
        self._apps, self._apps_at = found, time.time()
        return found

    def discover_apps(self, timeout: float = 20) -> list[dict[str, str]] | None:
        """Ask the TV for its installed apps (works on older sets; 2022+ never answer).

        Returns None when the TV doesn't reply in time.
        """
        if not self.online():
            raise DeviceError(f"{self.cfg.name} is off or unreachable")
        result: dict[str, Any] = {}

        def worker():
            try:
                result["apps"] = self._remote().app_list()
            except Exception as e:  # reported below
                result["error"] = e

        with self._lock:
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                self._ws = None  # abandon the stuck socket; a fresh one is opened next time
                return None
        if "error" in result:
            raise DeviceError(f"{self.cfg.name}: {result['error']}")
        return [{"id": str(a.get("appId")), "name": str(a.get("name"))} for a in result.get("apps") or []]

    def current_app(self) -> str | None:
        for app in self.installed_apps():
            try:
                st = self._rest(f"applications/{app['id']}")
            except requests.RequestException:
                continue
            if st and st.get("visible"):
                return app["name"]
        return None

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.cfg.id, "kind": "tv", "name": self.cfg.name, "host": self.host,
            "online": False, "power": "off", "paired": self.cfg.token_file.exists(),
            "wake_on_lan": self.cfg.wake_on_lan, "has_mac": bool(self.cfg.mac),
        }
        if not _port_open(self.host, 8001):
            return out
        try:
            dev = self.info()
            if not self._check_identity(dev):
                out["wrong_device"] = True
                return out
            out["online"] = True
            out["model"] = dev.get("modelName")
            out["network"] = self.network_label(dev)
            out["wired"] = dev.get("networkType") == "wired"  # the page and the CLI say "cable" in their language
            # 2017 sets don't report PowerState; if they answer at all, they're on.
            out["power"] = dev.get("PowerState", "on")
            if out["power"] == "on":
                self.warm_up()
                out["apps"] = self.installed_apps()
                out["current_app"] = self.current_app()
        except (requests.RequestException, ValueError) as e:
            out["error"] = str(e)
        return out

    def network_label(self, dev: dict[str, Any]) -> str:
        """'Wi-Fi Home', or the raw access-point MAC when it has no label; '' for a cable (see "wired")."""
        if dev.get("networkType") == "wired":
            return ""
        bssid = str(dev.get("ssid") or "").lower()
        if not bssid:
            return "Wi-Fi"
        return "Wi-Fi " + self._wifi_names.get(bssid, bssid)

    # --- WebSocket remote (paired) --------------------------------------------
    def _remote(self):
        from samsungtvws import SamsungTVWS

        if self._ws is None:
            paths.data_dir().mkdir(parents=True, exist_ok=True)
            # key_press_delay defaults to 1 s *after every key* in samsungtvws — that
            # was the remote's lag. We space multi-key sequences ourselves instead.
            self._ws = SamsungTVWS(
                host=self.host, port=8002, token_file=str(self.cfg.token_file),
                name=REMOTE_NAME, timeout=8, key_press_delay=0,
            )
        return self._ws

    def warm_up(self) -> None:
        """Open the remote channel in the background so the first press is instant.

        Only for paired TVs: on an unpaired one this would pop up the Allow prompt.
        """
        if not self.keep_alive or self._ws is not None or not self.cfg.token_file.exists():
            return

        def go():
            if not self._lock.acquire(blocking=False):
                return
            try:
                if self._ws is None:
                    self._remote().open()
            except Exception as e:
                log.info("warm-up %s failed: %s", self.cfg.id, e)
                self._ws = None
            finally:
                self._lock.release()

        threading.Thread(target=go, daemon=True).start()

    def _with_remote(self, fn):
        with self._lock:
            for attempt in (1, 2):
                try:
                    return fn(self._remote())
                except Exception as e:  # dropped socket: reconnect once
                    log.warning("remote %s failed (attempt %d): %s", self.cfg.id, attempt, e)
                    if self._ws is not None:
                        with contextlib.suppress(Exception):
                            self._ws.close()
                    self._ws = None
                    if attempt == 2:
                        raise DeviceError(f"{self.cfg.name}: {e}") from e

    def _require_online(self) -> None:
        if not self.online():
            if self.wrong_device:
                raise DeviceError(f"{self.cfg.name}: at {self.host} there is another device — try `tvpult find`")
            raise DeviceError(f"{self.cfg.name} is off or unreachable")

    def pair(self, timeout: float = 45) -> None:
        """Open the remote channel; the TV shows an Allow prompt the first time."""
        from samsungtvws import SamsungTVWS

        self._require_online()
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        tv = SamsungTVWS(host=self.host, port=8002, token_file=str(self.cfg.token_file),
                         name=REMOTE_NAME, timeout=timeout)
        try:
            tv.open()
        except Exception as e:  # declined, timed out, connection dropped — all mean "not paired"
            raise DeviceError(f"{self.cfg.name}: not allowed on the TV ({e})") from e
        finally:
            with contextlib.suppress(Exception):
                tv.close()
        if not self.cfg.token_file.exists():
            raise DeviceError("the TV did not issue a token (prompt declined or timed out)")

    def send_keys(self, keys: list[str]) -> list[str]:
        norm = [normalize_key(k) for k in keys]
        self._require_online()

        def go(tv):
            for i, k in enumerate(norm):
                if i:
                    time.sleep(0.15)  # spacing only between keys of one sequence
                tv.send_key(k)
            return norm

        return self._with_remote(go)

    def send_text(self, text: str, submit: bool = False) -> int:
        """Type into the text field currently open on the TV (search box, password...)."""
        if not text or len(text) > 256:
            raise DeviceError("text must be 1-256 characters")
        self._require_online()

        def go(tv):
            tv.send_text(text)
            if submit:
                tv.end_text()
            return len(text)

        return self._with_remote(go)

    def resolve_app(self, app: str) -> str:
        for a in self.installed_apps():
            if app.lower() in (a["alias"].lower(), a["name"].lower()):
                return a["id"]
        if app in self._catalog:
            raise DeviceError(f"{app} is not installed on {self.cfg.name}")
        if app.isdigit() or "." in app:
            return app
        raise DeviceError(f"unknown app: {app}")

    def launch(self, app: str) -> str:
        self._require_online()
        app_id = self.resolve_app(app)
        # Built-in apps (org.tizen.*) start as native apps, store apps as deep links.
        app_type = "DEEP_LINK" if app_id.isdigit() else "NATIVE_LAUNCH"
        self._with_remote(lambda tv: tv.run_app(app_id, app_type=app_type))
        return app_id

    def wol_targets(self) -> tuple[str, ...]:
        """Broadcast addresses for Wake-on-LAN: the TV's last network, the laptop's, and the generic one."""
        targets = [subnet_broadcast(self.host)]
        local = discovery.default_local_ip()
        if local:
            targets.append(subnet_broadcast(local))
        targets.append("255.255.255.255")
        return tuple(dict.fromkeys(targets))

    def power_on(self) -> str:
        if self.online():
            state = self.info().get("PowerState", "on")
            if state == "on":
                return "already on"
            self.send_keys(["KEY_POWER"])  # 2022 sets in network standby
            return "woken from standby"
        if not self.cfg.wake_on_lan or not self.cfg.mac:
            raise DeviceError(f"{self.cfg.name} cannot be powered on over the network — use the remote")
        send_wol(self.cfg.mac, self.wol_targets())
        return "wake-on-lan sent"

    def power_off(self) -> str:
        if not self.online() or self.info().get("PowerState", "on") != "on":
            return "already off"
        self.send_keys(["KEY_POWER"])
        return "turned off"

    def close(self) -> None:
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                finally:
                    self._ws = None


class CastSoundbar:
    def __init__(self, cfg: SoundbarConfig, host: str | None = None):
        self.cfg = cfg
        self.host = host or cfg.host
        self._lock = threading.Lock()
        self._cc = None
        self._identity = _Identity()
        self._arp_fn = discovery.read_arp

    def set_host(self, host: str) -> None:
        with self._lock:
            if host == self.host:
                return
            if self._cc is not None:
                with contextlib.suppress(Exception):
                    self._cc.disconnect(timeout=2)
            self._cc = None
            self.host = host
            self._identity.reset()

    @property
    def wrong_device(self) -> bool:
        return self._identity.wrong_host == self.host

    def online(self) -> bool:
        """Reachable *and* it is our soundbar (Cast UUID matches)."""
        if not _port_open(self.host, self.cfg.port):
            return False
        if self._identity.fresh(self.host):
            return True
        found = discovery.identify_cast(self.host)
        if found is not None and found != discovery.normalize_uuid(self.cfg.uuid):
            log.warning("%s: %s is a different Cast device", self.cfg.id, self.host)
            self._identity.wrong(self.host)
            return False
        if self.cfg.mac:  # the UUID is self-reported: the ARP table must confirm our MAC there
            if not discovery.mac_confirmed(self.host, self.cfg.mac, self._arp_fn):
                log.warning("%s: %s is not our soundbar (ARP disagrees)", self.cfg.id, self.host)
                self._identity.wrong(self.host)
                return False
        elif found is None:  # no MAC to check and the info endpoint is silent: can't tell
            return True
        self._identity.ok(self.host)
        return True

    def _cast(self):
        import pychromecast

        if self._cc is None:
            cc = pychromecast.get_chromecast_from_host(
                (self.host, self.cfg.port, uuid.UUID(self.cfg.uuid), None, self.cfg.name),
                tries=1, timeout=8,
            )
            cc.wait(timeout=8)
            self._cc = cc
        return self._cc

    def _with_cast(self, fn):
        with self._lock:
            for attempt in (1, 2):
                try:
                    return fn(self._cast())
                except Exception as e:
                    log.warning("cast %s failed (attempt %d): %s", self.cfg.id, attempt, e)
                    if self._cc is not None:
                        with contextlib.suppress(Exception):
                            self._cc.disconnect(timeout=2)
                    self._cc = None
                    if attempt == 2:
                        raise DeviceError(f"{self.cfg.name}: {e}") from e

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.cfg.id, "kind": "soundbar", "name": self.cfg.name,
                               "host": self.host, "online": self.online()}
        if self.wrong_device:
            out["wrong_device"] = True
        if not out["online"]:
            return out
        try:
            st = self._with_cast(lambda cc: cc.status)
            out["volume"] = round((st.volume_level or 0) * 100)
            out["muted"] = bool(st.volume_muted)
            out["app"] = st.display_name
        except DeviceError as e:
            out["error"] = str(e)
        return out

    def _require_online(self) -> None:
        if not self.online():
            raise DeviceError(f"{self.cfg.name} is off or unreachable")

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, int(level)))
        self._require_online()

        def go(cc):
            cc.set_volume(level / 100)
            return level

        return self._with_cast(go)

    def change_volume(self, delta: int) -> int:
        self._require_online()
        current = self._with_cast(lambda cc: round((cc.status.volume_level or 0) * 100))
        return self.set_volume(current + delta)

    def set_muted(self, muted: bool | None = None) -> bool:
        self._require_online()

        def go(cc):
            target = (not cc.status.volume_muted) if muted is None else muted
            cc.set_volume_muted(target)
            return target

        return self._with_cast(go)

    def close(self) -> None:
        with self._lock:
            if self._cc is not None:
                try:
                    self._cc.disconnect(timeout=2)
                finally:
                    self._cc = None


@contextlib.contextmanager
def _try_lock(lock: threading.Lock, busy: str):
    """Hold the lock, or refuse right away (DeviceError) if someone else holds it."""
    if not lock.acquire(blocking=False):
        raise DeviceError(busy)
    try:
        yield
    finally:
        lock.release()


PAIRING_BUSY = "another TV is being connected right now"


class Home:
    """All devices from the config, at their current addresses."""

    def __init__(self, config: Config, keep_alive: bool = False, book_path: Path | None = None,
                 locate_fn=None, local_ip_fn=None, adapters_fn=None, find_tvs_fn=None):
        self.config = config
        self.book = discovery.AddressBook.load(book_path or paths.data_dir() / "addresses.json")
        known = self.book.hosts
        self.tvs = {k: SamsungTv(v, config.extra_apps, config.wifi_names, keep_alive, known.get(k))
                    for k, v in config.tvs.items()}
        self.bars = {k: CastSoundbar(v, known.get(k)) for k, v in config.soundbars.items()}
        self._locate = locate_fn or discovery.locate
        self._local_ip = local_ip_fn or discovery.default_local_ip
        self._adapters = adapters_fn or discovery.adapter_addresses
        self._find_tvs = find_tvs_fn or discovery.find_tvs
        self._relocate_lock = threading.Lock()
        self._keep_alive = keep_alive
        self._candidates: dict[str, dict[str, Any]] = {}
        self._setup_lock = threading.Lock()  # one pairing at a time (wizard and re-pairing)
        self._config_lock = threading.Lock()  # one writer of devices.toml at a time

    def devices(self) -> list[SamsungTv | CastSoundbar]:
        return [*self.tvs.values(), *self.bars.values()]

    # --- setup wizard -------------------------------------------------------------
    @property
    def setup_needed(self) -> bool:
        return self.config.empty

    def setup_scan(self) -> list[dict[str, Any]]:
        """Find every TV for the wizard; each gets a short candidate id (`cid`) for setup_pair.

        Pairing later accepts only these ids — never an address from the page.
        """
        found = self.scan_tvs()
        candidates = {f"c{i}": t for i, t in enumerate(found, 1)}
        self._candidates = candidates
        return [{**t, "cid": cid} for cid, t in candidates.items()]

    def setup_pair(self, cid: str, name: str, timeout: float = 45) -> dict[str, Any]:
        """Pair a candidate from the last scan, then add it to devices.toml (only after the TV said yes)."""
        t = self._candidates.get(cid)
        if t is None:
            raise DeviceError("this TV is not in the last search — search again")
        if t["configured"]:
            return {"id": t["configured"], "already": True}
        if not t["confirmed"]:
            raise DeviceError(t["note"] or "the network did not confirm this TV's MAC")
        if not self._setup_lock.acquire(blocking=False):
            raise DeviceError("another TV is being connected right now")
        try:
            name = " ".join(name.split())[:40] or t["model"] or "TV"
            tv_id = paths.slugify(name, taken={*self.tvs, *self.bars})
            mac = ":".join(t["mac"][i:i + 2] for i in range(0, 12, 2))
            cfg = TvConfig(id=tv_id, name=name, host=t["host"], mac=mac,
                           # an assumption: our 2017 set didn't wake over Wi-Fi; editable in settings
                           wake_on_lan=(t["year"] or 0) >= 2018)
            tv = SamsungTv(cfg, self.config.extra_apps, self.config.wifi_names, self._keep_alive)
            tv.pair(timeout)  # the TV asks "Allow?"; DeviceError if declined / timed out
            with self._config_lock:
                paths.add_tv_to_config(cfg)
            self.config.tvs[tv_id] = cfg
            self.tvs[tv_id] = tv
            t["configured"] = tv_id
            self.book.hosts[tv_id] = cfg.host
            with contextlib.suppress(OSError):
                self.book.save()
            return {"id": tv_id, "already": False}
        finally:
            self._setup_lock.release()

    # --- settings page ------------------------------------------------------------------
    def _device(self, kind: str, dev_id: str) -> SamsungTv | CastSoundbar:
        if kind == "tv":
            return self.tv(dev_id)
        if kind == "soundbar":
            return self.bar(dev_id)
        raise DeviceError(f"unknown device kind: {kind}")

    def _write_config(self, fn, *args, **kwargs) -> None:
        """Change devices.toml; a config error becomes a DeviceError and the file stays as it was."""
        with self._config_lock:
            try:
                fn(*args, **kwargs)
            except (KeyError, ValueError, OSError) as e:
                raise DeviceError(f"devices.toml not changed: {e}") from e

    def update_device(self, kind: str, dev_id: str, name: str | None = None,
                      wake_on_lan: bool | None = None) -> dict[str, Any]:
        """Rename a device and/or switch a TV's Wake-on-LAN; saved to devices.toml."""
        dev = self._device(kind, dev_id)
        if wake_on_lan and not dev.cfg.mac:
            raise DeviceError(f"{dev.cfg.name}: no MAC address known, Wake-on-LAN can't work")
        self._write_config(paths.update_device, kind, dev_id, name=name, wake_on_lan=wake_on_lan)
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = paths.clean_name(name)
        if wake_on_lan is not None:
            changes["wake_on_lan"] = bool(wake_on_lan)
        dev.cfg = dataclasses.replace(dev.cfg, **changes)
        (self.config.tvs if kind == "tv" else self.config.soundbars)[dev_id] = dev.cfg
        return {"id": dev_id, **changes}

    def remove_device(self, kind: str, dev_id: str) -> dict[str, Any]:
        """Forget a device: its section in devices.toml, its pairing token and its remembered address."""
        dev = self._device(kind, dev_id)
        self._write_config(paths.remove_device, kind, dev_id)
        with contextlib.suppress(Exception):
            dev.close()
        if kind == "tv":
            self.tvs.pop(dev_id, None)
            self.config.tvs.pop(dev_id, None)
            dev.cfg.token_file.unlink(missing_ok=True)
        else:
            self.bars.pop(dev_id, None)
            self.config.soundbars.pop(dev_id, None)
        self._candidates = {}  # the last search's "already connected" marks are stale now
        if self.book.hosts.pop(dev_id, None) is not None:
            with contextlib.suppress(OSError):
                self.book.save()
        return {"removed": dev_id}

    def unpair(self, tv_id: str) -> dict[str, Any]:
        """Forget the TV's pairing token. The TV keeps tvpult in its own device list until removed there."""
        tv = self.tv(tv_id)
        with _try_lock(self._setup_lock, PAIRING_BUSY):
            try:
                tv.close()
            finally:
                had = tv.cfg.token_file.exists()
                tv.cfg.token_file.unlink(missing_ok=True)
        return {"id": tv_id, "unpaired": had}

    def repair(self, tv_id: str, timeout: float = 45) -> dict[str, Any]:
        """Pair a configured TV again (the TV may ask "Allow?"). If that fails, the old token comes back."""
        tv = self.tv(tv_id)
        with _try_lock(self._setup_lock, PAIRING_BUSY):
            token = tv.cfg.token_file
            old = token.read_bytes() if token.exists() else None
            tv.close()
            token.unlink(missing_ok=True)
            try:
                tv.pair(timeout)
            except Exception:
                if old is not None and not token.exists():
                    token.write_bytes(old)
                raise
        return {"id": tv_id, "paired": True}

    def set_quick_apps(self, aliases: list[str]) -> list[str]:
        """The quick-launch row (app aliases, in order); saved to devices.toml."""
        self._write_config(paths.set_quick_apps, aliases)
        self.config.quick_apps[:] = list(dict.fromkeys(aliases))
        return list(self.config.quick_apps)

    def search(self) -> dict[str, str] | None:
        """"Search again" from the settings: look for every device now.

        At most once per SEARCH_SECONDS: None means "searched a moment ago, not again yet".
        """
        if time.time() - self.book.scanned_at < SEARCH_SECONDS:
            return None
        return {dev_id: f.host for dev_id, f in self.relocate(force=True).items()}

    def tv(self, tv_id: str) -> SamsungTv:
        try:
            return self.tvs[tv_id]
        except KeyError:
            raise DeviceError(f"no TV '{tv_id}' (have: {', '.join(self.tvs)})") from None

    def bar(self, bar_id: str | None = None) -> CastSoundbar:
        if bar_id is None and len(self.bars) == 1:
            return next(iter(self.bars.values()))
        try:
            return self.bars[bar_id or ""]
        except KeyError:
            raise DeviceError(f"no soundbar '{bar_id}' (have: {', '.join(self.bars)})") from None

    # --- discovery --------------------------------------------------------------
    def _wanted(self) -> list[discovery.Wanted]:
        return ([discovery.Wanted(t.cfg.id, "tv", mac=t.cfg.mac, lan_mac=t.cfg.lan_mac) for t in self.tvs.values()]
                + [discovery.Wanted(b.cfg.id, "cast", mac=b.cfg.mac, uuid=b.cfg.uuid) for b in self.bars.values()])

    def subnets(self):
        return discovery.candidate_subnets([self._local_ip()], [d.host for d in self.devices()],
                                           self.config.scan_subnets, self._adapters())

    def scan_tvs(self) -> list[dict[str, Any]]:
        """Every Samsung TV on the network, configured or not (for `find --all` and the setup wizard).

        Each entry says which configured TV it is (`configured`: id or None).
        """
        by_mac = {discovery.normalize_mac(t.cfg.mac): t.cfg.id for t in self.tvs.values() if t.cfg.mac}
        out = []
        for s in self._find_tvs(self.subnets()):
            out.append({"host": s.host, "mac": s.mac, "model": s.model, "name": s.name, "year": s.year,
                        "confirmed": s.confirmed, "note": s.note, "configured": by_mac.get(s.mac)})
        return out

    def relocate(self, force: bool = False) -> dict[str, discovery.Found]:
        """Search the network for the devices and move them to where they are now.

        Rate-limited (once per RESCAN_SECONDS, shared between processes through the address book)
        unless force. Only one search runs at a time. Returns what was found.
        """
        if not self._relocate_lock.acquire(blocking=False):
            return {}
        try:
            if not force and time.time() - self.book.scanned_at < RESCAN_SECONDS:
                return {}
            hints = {d.cfg.id: d.host for d in self.devices()}
            found = self._locate(self._wanted(), self.subnets(), hints=hints)
            by_id = {d.cfg.id: d for d in self.devices()}
            for dev_id, where in found.items():
                dev = by_id.get(dev_id)
                if dev is None:
                    continue
                if dev.host != where.host:
                    log.warning("%s moved: %s -> %s (by %s)", dev_id, dev.host, where.host, where.how)
                    dev.set_host(where.host)
                self.book.hosts[dev_id] = where.host
            self.book.mark_scanned()
            try:
                self.book.save()
            except OSError as e:
                log.warning("address book not saved: %s", e)
            return found
        finally:
            self._relocate_lock.release()

    def relocate_in_background(self) -> None:
        threading.Thread(target=self.relocate, daemon=True).start()

    def ensure(self, dev: SamsungTv | CastSoundbar) -> bool:
        """Make sure dev is reachable; if not, search once. True if reachable now."""
        if dev.online():
            return True
        self.relocate()
        return dev.online()

    # --- status -------------------------------------------------------------------
    def status(self, rediscover: str = "none") -> list[dict[str, Any]]:
        """rediscover: 'none' | 'background' (server) | 'wait' (CLI) — what to do about missing devices."""
        devs = self.devices()
        with ThreadPoolExecutor(max_workers=len(devs) or 1) as ex:
            result = list(ex.map(lambda d: d.status(), devs))
        missing = any(not r.get("online") for r in result)
        if missing and rediscover == "background":
            self.relocate_in_background()
        elif missing and rediscover == "wait" and self.relocate():
            with ThreadPoolExecutor(max_workers=len(devs) or 1) as ex:
                result = list(ex.map(lambda d: d.status(), devs))
        return result

    def close(self) -> None:
        for d in self.devices():
            d.close()
