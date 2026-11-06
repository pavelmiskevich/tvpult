"""Small HTTP server: the remote page plus a JSON API.

Security model: the server listens on localhost; optionally on the laptop's Tailscale address (the tailnet
authenticates who can reach it) and, with --lan, on the home-network address — where every API call needs a
PIN session (see auth.py). Everywhere, two checks stop a random web page in the browser from driving the TVs:
  * the Host header must be one of our own names (blocks DNS rebinding);
  * every POST must carry the custom header X-Tvpult: 1 (a cross-site page can't
    add it without a CORS preflight, which this server never approves).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from .auth import LanAuth, SignInError, cookie_token, device_label, set_cookie
from .catalog import UnknownKey, app_names
from .config import APP_ALIAS, MAX_QUICK_APPS, SECTIONS, clean_name
from .devices import DeviceError, Home
from .i18n import page_strings, t

log = logging.getLogger(__name__)

_ROUTE = re.compile(r"^/api/(tv|bar)/([A-Za-z0-9_-]+)/([a-z]+)$")
MAX_BODY = 16 * 1024
_DEV_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def dispatch(home: Home, method: str, path: str, body: dict[str, Any]) -> Any:
    """Route one API call. Pure apart from the device calls, so it is easy to test."""
    if method == "GET" and path == "/api/status":
        quick = getattr(getattr(home, "config", None), "quick_apps", [])
        return {"devices": home.status(rediscover="background"), "quick": quick,
                "setup": bool(getattr(home, "setup_needed", False))}
    if method != "POST":
        raise ApiError(HTTPStatus.NOT_FOUND, "not found")
    if path == "/api/setup/scan":
        return {"tvs": home.setup_scan()}
    if path == "/api/setup/pair":
        cid, name = body.get("cid"), body.get("name", "")
        if not isinstance(cid, str) or not re.fullmatch(r"c\d{1,4}", cid):
            raise ApiError(HTTPStatus.BAD_REQUEST, "cid required")
        if not isinstance(name, str) or len(name) > 40:
            raise ApiError(HTTPStatus.BAD_REQUEST, "name must be text, up to 40 characters")
        try:
            return home.setup_pair(cid, name)
        except DeviceError as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e)) from e
    if path.startswith("/api/settings/"):
        try:
            return _settings(home, path.removeprefix("/api/settings/"), body)
        except DeviceError as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e)) from e
    m = _ROUTE.match(path)
    if not m:
        raise ApiError(HTTPStatus.NOT_FOUND, "not found")
    kind, dev_id, action = m.groups()
    try:
        return _act(home, kind, dev_id, action, body)
    except DeviceError as first:
        if _found_elsewhere(home, kind, dev_id, action):
            try:
                return _act(home, kind, dev_id, action, body)
            except DeviceError as e:
                raise ApiError(HTTPStatus.CONFLICT, str(e)) from e
        raise ApiError(HTTPStatus.CONFLICT, str(first)) from first


def _settings(home: Home, action: str, body: dict[str, Any]) -> Any:
    """The settings page: everything that used to need editing devices.toml by hand."""
    if action == "quick":
        apps = body.get("apps")
        if (not isinstance(apps, list) or len(apps) > MAX_QUICK_APPS
                or not all(isinstance(a, str) and APP_ALIAS.fullmatch(a) for a in apps)):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"apps: a list of up to {MAX_QUICK_APPS} app names")
        return {"quick": home.set_quick_apps(apps)}
    if action == "search":
        found = home.search()
        return {"found": found or {}, "recent": found is None}
    if action not in ("device", "remove", "unpair", "pair"):
        raise ApiError(HTTPStatus.NOT_FOUND, "not found")
    kind, dev_id = body.get("kind"), body.get("id")
    if kind not in SECTIONS or not isinstance(dev_id, str) or not _DEV_ID.fullmatch(dev_id):
        raise ApiError(HTTPStatus.BAD_REQUEST, "kind (tv or soundbar) and id required")
    if action == "remove":
        return home.remove_device(kind, dev_id)
    if action == "device":
        name, wol = body.get("name"), body.get("wake_on_lan")
        if name is not None and (not isinstance(name, str) or not clean_name(name) or len(name) > 40):
            raise ApiError(HTTPStatus.BAD_REQUEST, "name must be text, 1-40 characters")
        if wol is not None and (not isinstance(wol, bool) or kind != "tv"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "wake_on_lan must be true or false, TVs only")
        if name is None and wol is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "nothing to change")
        return home.update_device(kind, dev_id, name=name, wake_on_lan=wol)
    if kind != "tv":
        raise ApiError(HTTPStatus.BAD_REQUEST, "only TVs are paired")
    return home.unpair(dev_id) if action == "unpair" else home.repair(dev_id)


def _found_elsewhere(home: Home, kind: str, dev_id: str, action: str) -> bool:
    """After a failed action: is the device unreachable here, and did a search find it somewhere?

    Not for power: an off TV is unreachable by nature, and searching would only delay Wake-on-LAN.
    """
    relocate = getattr(home, "relocate", None)
    if relocate is None or (kind, action) == ("tv", "power"):
        return False
    try:
        dev = home.tv(dev_id) if kind == "tv" else home.bar(dev_id)
    except DeviceError:
        return False  # unknown device id: nothing to search for
    if dev.online():
        return False  # it's there; the failure was something else
    return dev_id in relocate()


def _act(home: Home, kind: str, dev_id: str, action: str, body: dict[str, Any]) -> Any:
    """Run one action. DeviceError propagates (dispatch decides about retrying)."""
    try:
        if kind == "tv":
            tv = home.tv(dev_id)
            if action == "key":
                keys = body.get("keys") or ([body["key"]] if "key" in body else [])
                if not keys or not all(isinstance(k, str) for k in keys):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "keys required")
                return {"sent": tv.send_keys(keys)}
            if action == "launch":
                app = body.get("app")
                if not isinstance(app, str) or not app:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "app required")
                return {"launched": tv.launch(app)}
            if action == "text":
                text = body.get("text")
                if not isinstance(text, str) or not text:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "text required")
                return {"typed": tv.send_text(text, submit=bool(body.get("submit")))}
            if action == "power":
                on = body.get("on")
                if not isinstance(on, bool):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "on must be true or false")
                return {"result": tv.power_on() if on else tv.power_off()}
        else:
            bar = home.bar(dev_id)
            if action == "volume":
                if isinstance(body.get("level"), int):
                    return {"volume": bar.set_volume(body["level"])}
                if isinstance(body.get("delta"), int):
                    return {"volume": bar.change_volume(body["delta"])}
                raise ApiError(HTTPStatus.BAD_REQUEST, "level or delta required")
            if action == "mute":
                muted = body.get("muted")
                if muted is not None and not isinstance(muted, bool):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "muted must be bool or null")
                return {"muted": bar.set_muted(muted)}
    except UnknownKey as e:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"key not allowed: {e}") from e
    raise ApiError(HTTPStatus.NOT_FOUND, "not found")


class Presence:
    """Who has the remote page open — so the desktop-shortcut mode can stop when the last one closes.

    Pages say hello every ~20 s (browsers slow timers in hidden tabs to about once a minute, hence the
    generous `stale`) and say bye when closed. Client ids are random 128-bit values generated by the
    page; bye is accepted only for an id that said hello, so a foreign site can't stop the server.
    """

    _ID = re.compile(r"^[0-9a-f]{32}$")

    def __init__(self, idle_exit: bool = False, grace: float = 8.0, stale: float = 180.0,
                 startup: float = 120.0, clock=None):
        import time as _time

        self._clock = clock or _time.monotonic
        self.idle_exit = idle_exit
        self.grace, self.stale, self.startup = grace, stale, startup
        self._clients: dict[str, float] = {}
        self._started = self._clock()
        self._empty_since: float | None = None
        self._lock = threading.Lock()

    def valid(self, client: Any) -> bool:
        return isinstance(client, str) and bool(self._ID.match(client))

    def hello(self, client: str) -> None:
        with self._lock:
            new = client not in self._clients
            self._clients[client] = self._clock()
            self._empty_since = None
            if new:
                log.info("remote page opened (%d open)", len(self._clients))

    def bye(self, client: str) -> bool:
        with self._lock:
            if self._clients.pop(client, None) is None:
                return False
            if not self._clients:
                self._empty_since = self._clock()
            log.info("remote page closed (%d open)", len(self._clients))
            return True

    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def should_exit(self) -> bool:
        if not self.idle_exit:
            return False
        now = self._clock()
        with self._lock:
            for cid, seen in list(self._clients.items()):
                if now - seen > self.stale:  # tab gone without a bye (crash, sleep, killed browser)
                    del self._clients[cid]
                    if not self._clients:
                        self._empty_since = now
            if self._clients:
                return False
            if self._empty_since is None:  # nobody ever opened the page
                return now - self._started > self.startup
            return now - self._empty_since >= self.grace  # grace: a reload says bye then hello


def host_allowed(host_header: str | None, allowed: set[str]) -> bool:
    if not host_header:
        return False
    hostname = urlsplit("//" + host_header).hostname or ""
    return hostname.lower() in allowed


def origin_ok(origin: str | None, host_header: str | None) -> bool:
    if origin is None:
        return True
    return urlsplit(origin).netloc.lower() == (host_header or "").lower()


I18N_SLOT = b"/*TVPULT_I18N*/null"
APPS_SLOT = b"/*TVPULT_APPS*/null"


def _embed(value: Any) -> bytes:
    # "<" escaped so no text in the data can close the <script> element.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").encode("utf-8")


def _page() -> bytes:
    """The page with both dictionaries and the app names embedded: works offline and before sign-in (PIN)."""
    page = resources.files("tvpult").joinpath("static/index.html").read_bytes()
    return page.replace(I18N_SLOT, _embed(page_strings()), 1).replace(APPS_SLOT, _embed(app_names()), 1)


def qr_data_uri(text: str) -> str:
    """The URL as a QR code, an SVG data: URI (drawn locally, nothing fetched)."""
    import segno

    return segno.make(text, error="m").svg_data_uri(scale=5, border=2, dark="#111318", light="#ffffff")


def make_handler(home: Home, allowed_hosts: set[str], presence: Presence | None = None,
                 role: str = "local", auth: LanAuth | None = None, lan_url: str | None = None):
    """role: 'local' (this computer: everything, including the PIN), 'tailnet' (Tailscale vouches for the
    caller), or 'lan' (home network: every API call needs a PIN session)."""
    presence = presence or Presence()
    if role == "lan" and auth is None:
        raise ValueError("the LAN listener needs PIN sign-in")

    class Handler(BaseHTTPRequestHandler):
        server_version = "tvpult"

        def log_message(self, fmt, *args):  # every request (a status poll each 5 s) only at debug level
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, obj: Any, headers: dict[str, str] | None = None) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", headers)

        def _guard(self) -> bool:
            if not host_allowed(self.headers.get("Host"), allowed_hosts):
                self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
                return False
            return True

        def _signed_in(self) -> bool:
            """On the LAN listener every API call needs a session; elsewhere nothing extra is needed."""
            if role != "lan":
                return True
            if auth.valid(cookie_token(self.headers.get("Cookie"))):
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "pin_required"})
            return False

        def _lan_info(self) -> dict[str, Any]:
            if auth is None or not lan_url:
                return {"enabled": False}
            return {"enabled": True, "url": lan_url, "pin": auth.pin, "qr": qr_data_uri(lan_url),
                    "sessions": auth.sessions()}

        def do_GET(self):
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):  # the page holds no secrets: served to everyone allowed in
                self._send(HTTPStatus.OK, _page(), "text/html; charset=utf-8")
                return
            if path == "/api/lan":  # the PIN and the sessions: this computer only
                if role != "local":
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, self._lan_info())
                return
            if not self._signed_in():
                return
            self._handle("GET", path, {})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
                return
            # Read the body before any refusal, so the client gets the answer
            # instead of a reset connection.
            raw = self.rfile.read(length)
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path == "/api/bye":
                # Sent by navigator.sendBeacon on page close, which can't add custom headers.
                # Safe without X-Tvpult: it only works for a random client id that said hello.
                if not self._signed_in():
                    return
                try:
                    client = json.loads(raw or b"{}").get("client")
                except (ValueError, AttributeError):
                    client = None
                ok = presence.valid(client) and presence.bye(client)
                self._json(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, {"bye": bool(ok)})
                return
            if self.headers.get("X-Tvpult") != "1" or not origin_ok(
                self.headers.get("Origin"), self.headers.get("Host")
            ):
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            if path == "/api/login":
                if role != "lan":
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                try:
                    token = auth.sign_in(self.client_address[0], body.get("pin"),
                                         label=device_label(self.headers.get("User-Agent")))
                except SignInError as e:
                    self._json(e.status, {"error": str(e)})
                    return
                self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": set_cookie(token)})
                return
            if path.startswith("/api/lan/"):  # managing phone access: this computer only
                if role != "local" or auth is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if path == "/api/lan/revoke" and isinstance(body.get("id"), str):
                    self._json(HTTPStatus.OK, {"revoked": auth.revoke(body["id"])})
                elif path == "/api/lan/pin":
                    auth.renew_pin()
                    self._json(HTTPStatus.OK, self._lan_info())
                else:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
                return
            if not self._signed_in():
                return
            if path == "/api/hello":
                if not presence.valid(body.get("client")):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad client id"})
                    return
                presence.hello(body["client"])
                self._json(HTTPStatus.OK, {"hello": True, "auto_exit": presence.idle_exit})
                return
            self._handle("POST", path, body)

        def _handle(self, method: str, path: str, body: dict[str, Any]) -> None:
            try:
                self._json(HTTPStatus.OK, dispatch(home, method, path, body))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:  # keep the server alive, report the failure
                log.exception("unhandled error")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    return Handler


def serve(home: Home, binds: list[str], port: int, extra_names: set[str] | None = None,
          presence: Presence | None = None, on_ready=None, lan: str | None = None,
          auth: LanAuth | None = None) -> None:
    """Serve until Ctrl+C — or, with presence.idle_exit, until the last page is closed.

    binds: 127.0.0.1 ('local') and, with --tailnet, the Tailscale address ('tailnet').
    lan: the home-network address to listen on too — with PIN sign-in (auth) required there.
    """
    import time

    presence = presence or Presence()
    allowed = {"localhost", "127.0.0.1", *binds, *(extra_names or set())}
    allowed = {a.lower() for a in allowed}
    lan_url = f"http://{lan}:{port}/" if lan else None
    servers = []
    for addr in binds:
        role = "local" if addr in ("127.0.0.1", "localhost") else "tailnet"
        handler = make_handler(home, allowed, presence, role=role, auth=auth, lan_url=lan_url)
        servers.append(ThreadingHTTPServer((addr, port), handler))
    if lan:
        if auth is None:
            raise ValueError("listening on the home network needs PIN sign-in")
        handler = make_handler(home, {lan.lower()}, presence, role="lan", auth=auth, lan_url=lan_url)
        servers.append(ThreadingHTTPServer((lan, port), handler))
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:  # (not `t`: that is the translation function)
        thread.start()
    for addr in binds:
        print(f"  http://{addr}:{port}/")
    if lan:
        print(t("cli.lan_line", url=lan_url, pin=auth.pin))
    # Devices may have moved since last time: look for any that don't answer where we left them.
    threading.Thread(target=lambda: home.status(rediscover="wait"), daemon=True).start()
    if on_ready:
        on_ready()
    try:
        while not presence.should_exit():
            time.sleep(1)
        log.info("last page closed — stopping")
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
        home.close()
