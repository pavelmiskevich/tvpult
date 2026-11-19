"""#34: a TV that is simply off doesn't make tvpult scan the networks every minute."""

from tvpult.config import Config, TvConfig
from tvpult.devices import SEARCH_BACKOFF, Home
from tvpult.discovery import Found
from tvpult.server import Presence, dispatch


class World:
    """A fake clock, this computer's address, and a TV that is on or off."""

    def __init__(self):
        self.t = 1_800_000_000.0
        self.ip = "192.168.1.20"
        self.tv_on = False
        self.scans = 0
        self.found: dict[str, Found] = {}

    def home(self) -> Home:
        def locate(wanted, subnets, hints=None):
            self.scans += 1
            return dict(self.found)

        home = Home(Config(tvs={"living": TvConfig(id="living", name="L", host="192.168.1.50")}),
                    locate_fn=locate, local_ip_fn=lambda: self.ip, adapters_fn=list)
        home._wall = lambda: self.t
        home.tvs["living"].status = lambda: {"id": "living", "kind": "tv", "online": self.tv_on}
        home.relocate_in_background = home._search_for  # synchronous, for the test
        return home

    def poll(self, home: Home, seconds: int) -> int:
        """The page asks for the status every 5 s for `seconds`; returns the scans made meanwhile."""
        before = self.scans
        for _ in range(seconds // 5):
            self.t += 5
            home.status(rediscover="background")
        return self.scans - before


def test_a_tv_off_for_an_hour_causes_a_handful_of_scans_not_60():
    w = World()
    home = w.home()
    first_hour = w.poll(home, 3600)
    assert first_hour <= 8  # 1, 2, 5, 15, 15, 15 … minutes apart
    overnight = [w.poll(home, 3600) for _ in range(3)]
    assert all(n <= 3600 // SEARCH_BACKOFF[-1] for n in overnight)  # then 4 an hour at most


def test_backoff_steps():
    w = World()
    home = w.home()
    times = []
    for _ in range(3600 // 5):
        w.t += 5
        before = w.scans
        home.status(rediscover="background")
        if w.scans > before:
            times.append(w.t)
    gaps = [round(b - a) for a, b in zip(times, times[1:], strict=False)]
    assert gaps[:4] == [60, 120, 300, 900]


def test_a_tv_that_answers_again_starts_from_the_first_step():
    w = World()
    home = w.home()
    w.poll(home, 3600)
    w.tv_on = True
    w.poll(home, 60)
    w.tv_on = False
    assert w.poll(home, 70) >= 1  # searched within a minute of going missing again


def test_a_tv_switched_off_from_the_remote_is_not_searched_for():
    w = World()
    w.tv_on = True
    home = w.home()
    tv = home.tvs["living"]
    tv.online = lambda: True
    tv.info = lambda: {"PowerState": "on"}
    tv.send_keys = lambda keys: keys
    assert tv.power_off() == "turned off"
    w.tv_on = False
    assert w.poll(home, 3600) == 0
    w.tv_on = True
    w.poll(home, 10)
    assert not tv.switched_off  # back: searched for normally again when it goes missing


def test_moving_to_another_network_searches_at_once():
    w = World()
    home = w.home()
    w.poll(home, 3600)  # well into the long steps
    w.ip = "10.0.0.20"
    assert w.poll(home, 5) == 1


def test_a_new_page_or_a_button_searches_at_once():
    w = World()
    home = w.home()
    w.poll(home, 3600)
    assert w.poll(home, 5) == 0
    home.search_soon()  # what /api/hello does for a page that just opened
    assert w.poll(home, 5) == 1
    assert w.poll(home, 5) == 0
    home.tvs["living"].send_keys = lambda keys: keys
    w.t += SEARCH_BACKOFF[0]  # (the overall once-a-minute limit still holds)
    dispatch(home, "POST", "/api/tv/living/key", {"keys": ["ok"]})
    assert w.poll(home, 5) == 1


def test_found_at_a_new_address_resets_its_steps():
    w = World()
    home = w.home()
    w.poll(home, 600)
    w.found = {"living": Found("192.168.1.77", "mac")}
    w.t += SEARCH_BACKOFF[-1]
    w.poll(home, 5)
    assert home.tvs["living"].host == "192.168.1.77" and "living" not in home._misses


def test_hello_tells_whether_the_page_is_new():
    p = Presence()
    assert p.hello("0" * 32) is True and p.hello("0" * 32) is False
