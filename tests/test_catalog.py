"""#5: the app catalog is a data file (src/tvpult/apps.json) with a schema; ids seen on the TV's year go first."""

import copy
import json
from pathlib import Path

import pytest

from tvpult.catalog import App, CatalogError, app_catalog, app_names, catalog, parse_catalog
from tvpult.config import TvConfig
from tvpult.devices import SamsungTv

APPS_JSON = Path(__file__).resolve().parent.parent / "src" / "tvpult" / "apps.json"
RAW = json.loads(APPS_JSON.read_text(encoding="utf-8"))


# --- the shipped file ---------------------------------------------------------------------------------
def test_the_shipped_catalog_follows_the_schema():
    apps = parse_catalog(RAW)
    assert len(apps) >= 10 and all(isinstance(a, App) for a in apps.values())
    assert catalog() == apps


def test_every_app_has_an_english_name_and_regional_ones_a_local_name():
    for app in catalog().values():
        assert app.name("en")
        if "RU" in app.regions:
            assert "ru" in app.names, app.alias


def test_the_file_stays_hand_editable():
    """One id per line, so a contributor's PR adds or changes one line."""
    text = APPS_JSON.read_text(encoding="utf-8")
    assert text.endswith("}\n") and "\r" not in text
    for app in RAW["apps"].values():
        for i in app["ids"]:
            assert json.dumps(i, ensure_ascii=False, separators=(", ", ": ")) in text


def test_known_ids_kept_from_before_the_move():
    cat = app_catalog()
    assert cat["youtube"] == ["111299001912"]
    assert cat["browser"] == ["org.tizen.browser", "3201907018784", "3202010022079"]
    assert cat["kinopoisk"] == ["111399000037"]


# --- the schema -------------------------------------------------------------------------------------------
def _with(change):
    raw = copy.deepcopy(RAW)
    change(raw)
    return raw


@pytest.mark.parametrize("change", [
    lambda r: r.update(schema=2),
    lambda r: r.update(extra=1),
    lambda r: r.update(apps={}),
    lambda r: r["apps"].update({"Bad Alias": r["apps"]["youtube"]}),
    lambda r: r["apps"]["youtube"].pop("regions"),
    lambda r: r["apps"]["youtube"].update(colour="red"),
    lambda r: r["apps"]["youtube"]["names"].pop("en"),
    lambda r: r["apps"]["youtube"]["names"].update(en=" "),
    lambda r: r["apps"]["youtube"]["names"].update(de="YouTube"),
    lambda r: r["apps"]["youtube"]["names"].update(ru="x" * 41),
    lambda r: r["apps"]["youtube"].update(regions=[]),
    lambda r: r["apps"]["youtube"].update(regions=["ru"]),
    lambda r: r["apps"]["youtube"].update(regions=["*", "RU"]),
    lambda r: r["apps"]["youtube"].update(regions=["RU", "RU"]),
    lambda r: r["apps"]["youtube"].update(ids=[]),
    lambda r: r["apps"]["youtube"].update(ids=["111299001912"]),
    lambda r: r["apps"]["youtube"].update(ids=[{"id": "bad id"}]),
    lambda r: r["apps"]["youtube"].update(ids=[{"id": "111299001912", "note": "x"}]),
    lambda r: r["apps"]["youtube"].update(ids=[{"id": "111", "seen": [2022, 2017]}]),
    lambda r: r["apps"]["youtube"].update(ids=[{"id": "111", "seen": [22]}]),
    lambda r: r["apps"]["youtube"].update(ids=[{"id": "111", "seen": [True]}]),
    lambda r: r["apps"]["youtube"].update(ids=[{"id": "111", "seen": "2022"}]),
    lambda r: r["apps"]["netflix"]["ids"].append({"id": "111299001912"}),  # the same id under two apps
])
def test_schema_violations_are_refused(change):
    with pytest.raises(CatalogError, match="apps.json"):
        parse_catalog(_with(change))


def test_a_minimal_valid_entry():
    apps = parse_catalog({"schema": 1, "apps": {"my.app": {"names": {"en": "Mine"}, "regions": ["DE", "AT"],
                                                           "ids": [{"id": "3201234567890"}]}}})
    assert apps["my.app"].name("ru") == "Mine"  # no Russian name: English
    assert apps["my.app"].regions == ("DE", "AT") and apps["my.app"].ids[0].seen == ()


# --- probing order -------------------------------------------------------------------------------------------
def test_ids_seen_on_the_tvs_year_are_tried_first():
    assert app_catalog(year=2022)["browser"] == ["3202010022079", "org.tizen.browser", "3201907018784"]
    assert app_catalog(year=2017)["browser"] == ["org.tizen.browser", "3201907018784", "3202010022079"]
    assert app_catalog(year=2030)["browser"] == app_catalog()["browser"]  # unknown year: file order


def test_users_own_ids_still_come_first():
    cat = app_catalog({"browser": ["my.browser"], "myapp": ["111"]}, year=2022)
    assert cat["browser"][:2] == ["my.browser", "3202010022079"] and cat["myapp"] == ["111"]


def test_a_2022_tv_is_asked_for_the_2022_id_first():
    asked = []

    def rest(path, timeout=3.0):
        asked.append(path.removeprefix("applications/"))
        return {"name": "Internet"} if path.endswith("3202010022079") else None

    tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0"))
    tv._rest = rest
    tv.year = 2022
    apps = {a["alias"]: a["id"] for a in tv.installed_apps()}
    assert apps["browser"] == "3202010022079"
    assert "org.tizen.browser" not in asked  # found before getting there


def test_the_year_comes_from_the_platform_code_on_the_identity_check():
    tv = SamsungTv(TvConfig(id="t", name="T", host="0.0.0.0"))  # no MAC configured: nothing else to check
    tv._check_identity({"model": "17_KANTM_UHD"})
    assert tv.year == 2017


# --- where the names go --------------------------------------------------------------------------------------
def test_the_page_gets_the_app_names():
    from tvpult.server import _page

    html = _page().decode("utf-8")
    script = html[html.index("const APPS = "):]
    names = json.loads(script[len("const APPS = "):script.index(";")])
    assert names == app_names() and names["kinopoisk"] == {"en": "Kinopoisk", "ru": "Кинопоиск"}
    assert names["netflix"]["ru"] == "Netflix"  # no separate Russian name: the English one


def test_cli_lists_the_catalog(capsys):
    from tvpult.cli import main

    assert main(["catalog"]) == 0
    out = capsys.readouterr().out
    assert "kinopoisk  Кинопоиск" in out and "111399000037 (2017, 2022)" in out
