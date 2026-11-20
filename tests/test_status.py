"""#33: the requests to a TV don't depend on how many pages are open, and a slow TV can't make them pile up."""

import threading
import time

from tvpult import devices
from tvpult.config import Config, TvConfig
from tvpult.devices import STATUS_SECONDS, Home, SamsungTv
from tvpult.server import dispatch


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _home():
    home = Home(Config(tvs={"living": TvConfig(id="living", name="L", host="192.168.1.50")}),
                local_ip_fn=lambda: None, adapters_fn=list)
    asked = []

    def status():
        asked.append(home.tvs["living"].host)
        return {"id": "living", "kind": "tv", "online": True, "power": "on"}

    home.tvs["living"].status = status
    home.clock = home._clock = FakeClock()
    return home, asked


# --- one status per device, shared ---------------------------------------------------------------------
def _poll(pages: int, seconds: int = 30) -> int:
    home, asked = _home()
    start = home.clock.t
    for second in range(seconds + 1):
        home.clock.t = start + second
        for page in range(pages):
            if (second - page) % 5 == 0:  # each page polls every 5 s, a second apart from the others
                home.status()
    return len(asked)


def test_two_pages_ask_the_tv_no_more_than_one():
    assert _poll(pages=2) == _poll(pages=1) == 7  # 0, 5, … 30 s
    for pages in (4, 10):  # however many: never more than one request per STATUS_SECONDS
        assert _poll(pages) <= 30 / STATUS_SECONDS + 1


def test_callers_arriving_meanwhile_wait_for_the_same_answer():
    home, _ = _home()
    calls, gate = [], threading.Event()

    def slow_status():
        calls.append(1)
        gate.wait(2)
        return {"id": "living", "kind": "tv", "online": True}

    home.tvs["living"].status = slow_status
    results = []
    threads = [threading.Thread(target=lambda: results.append(home.status())) for _ in range(5)]
    for th in threads:
        th.start()
    time.sleep(0.2)
    gate.set()
    for th in threads:
        th.join(3)
    assert len(calls) == 1 and len(results) == 5


def test_an_action_or_a_move_asks_again():
    home, asked = _home()
    home.status()
    home.status()
    assert len(asked) == 1
    home.tvs["living"].send_keys = lambda keys: keys
    dispatch(home, "POST", "/api/tv/living/key", {"keys": ["ok"]})
    home.status()
    assert len(asked) == 2  # the page refreshes right after a button: it shows the result, not a snapshot
    home.tvs["living"].host = "192.168.1.77"
    home.status()
    assert asked[-1] == "192.168.1.77"
    home.clock.t += STATUS_SECONDS
    home.status()
    assert len(asked) == 4


# --- the app on screen ---------------------------------------------------------------------------------
APPS = [{"alias": a, "id": i, "name": a.title()} for a, i in (("youtube", "1"), ("netflix", "2"), ("okko", "3"))]


def _tv(on_screen):
    tv = SamsungTv(TvConfig(id="living", name="L", host="192.168.1.50"))
    tv._apps, tv._apps_at = list(APPS), time.time()
    asked = []

    def rest(path, timeout=3.0):
        app_id = path.rsplit("/", 1)[1]
        asked.append(app_id)
        return {"visible": app_id == on_screen[0]}

    tv._rest = rest
    return tv, asked


def test_the_last_app_seen_is_asked_first_and_alone():
    on_screen = ["2"]
    tv, asked = _tv(on_screen)
    assert tv.current_app() == "Netflix" and sorted(asked) == ["1", "2", "3"]
    asked.clear()
    assert tv.current_app() == "Netflix" and asked == ["2"]  # one request while it stays on screen


def test_live_tv_doesnt_ask_every_app_every_time():
    on_screen = [None]  # live TV or HDMI: no app visible
    tv, asked = _tv(on_screen)
    assert tv.current_app() is None and len(asked) == 3
    asked.clear()
    assert tv.current_app() is None and asked == []  # not again before APP_SCAN_SECONDS
    tv._scan_at -= devices.APP_SCAN_SECONDS
    on_screen[0] = "3"
    assert tv.current_app() == "Okko" and len(asked) == 3


def test_an_app_started_from_the_remote_is_asked_first():
    tv, asked = _tv([None])
    tv._require_online = lambda: None
    tv._with_remote = lambda fn: None
    tv.launch("okko")
    assert tv._last_app["id"] == "3"


# --- the app list ------------------------------------------------------------------------------------------
def test_an_old_app_list_is_refreshed_in_the_background():
    tv = SamsungTv(TvConfig(id="living", name="L", host="192.168.1.50"))
    old = [APPS[0]]
    tv._apps, tv._apps_at = old, time.time() - devices.APP_CACHE_SECONDS - 1
    started, gate = [], threading.Event()

    def probe():
        started.append(1)
        gate.wait(2)
        tv._apps, tv._apps_at = list(APPS), time.time()
        return tv._apps

    tv._probe_apps = probe
    assert tv.installed_apps() is old  # at once, with the old list
    assert tv.installed_apps() is old and len(started) <= 1  # one refresh at a time
    gate.set()
    for _ in range(50):
        if tv._apps is not old:
            break
        time.sleep(0.02)
    assert tv.installed_apps() == APPS and len(started) == 1


def test_the_page_polls_one_at_a_time():
    page = (devices.paths.PACKAGE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    assert "if (refreshing) { refreshAgain = true; return; }" in page
