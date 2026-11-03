"""Natural-region trip names (regions.py + trips.region_name): "Šumava", not "Kolinec a okolí"."""
import io
import os
import sqlite3
from datetime import datetime as _dt

import pytest

import i18n
import regions
import trips
from test_trips_hub import Lib, _db

HERE = os.path.dirname(os.path.abspath(__file__))

# real places (GeoNames)
SUSICE = (49.2311, 13.5202)
KASPERSKE = (49.1429, 13.5563)
ZELEZNA_RUDA = (49.1374, 13.2352)
HARTMANICE = (49.1694, 13.4545)
KOLINEC = (49.2988, 13.4397)
FRAUENAU = (48.9886, 13.3013)
ZWIESEL = (49.0167, 13.2333)
REGEN = (48.9701, 13.1279)
HORAZDOVICE = (49.3208, 13.7010)
SPINDLERUV_MLYN = (50.7262, 15.6092)
PRAHA = (50.0875, 14.4214)


@pytest.mark.parametrize("place,part", [
    (SUSICE, "sumava"), (KASPERSKE, "sumava"), (ZELEZNA_RUDA, "sumava"), (HARTMANICE, "sumava"),
    (KOLINEC, "sumava"), (FRAUENAU, "bayerischer-wald"), (ZWIESEL, "bayerischer-wald"),
    (SPINDLERUV_MLYN, "krkonose"),
])
def test_places_fall_into_their_region(place, part):
    hit = regions.part_at(*place)
    assert hit and hit[1]["id"] == part


@pytest.mark.parametrize("place", [PRAHA, HORAZDOVICE, (50.0, 12.0), (35.0, 139.0)])
def test_places_outside_every_region(place):
    assert regions.part_at(*place) is None


def test_the_libraries_sumava_week_is_named_after_the_region():
    lib = Lib()
    lib.sumava_week()
    j = [c for c in trips.segment(lib.items) if c.kind == trips.KIND_JOURNEY][0]
    title, location = trips.title_for(j.items, j.hub, regional=True)
    assert location == "Šumava"
    assert title.startswith("Výlet: Šumava (")
    # day chapters keep their towns
    assert [trips.title_for(ch.items)[1] for ch in j.children][:2] == ["Susice", "Kasperske Hory"]


def _trip(points):
    items = []
    for i, (pt, city) in enumerate(points):
        items.append({"id": i + 1, "dt": _dt(2026, 7, 6 + i // 4, 10 + i % 4), "lat": pt[0], "lon": pt[1], "city": city})
    return items


def test_cross_border_trip_names_the_majority_side_or_both():
    cz = [(SUSICE, "Susice"), (KASPERSKE, "Kasperske Hory"), (ZELEZNA_RUDA, "Zelezna Ruda")] * 3
    de = [(FRAUENAU, "Frauenau"), (ZWIESEL, "Zwiesel"), (REGEN, "Regen")] * 3
    assert trips.region_name(_trip(cz + de[:2])) == "Šumava"
    assert trips.region_name(_trip(de + cz[:2])) == "Bavorský les"
    assert trips.region_name(_trip(cz + de)) == "Šumava a Bavorský les"


def test_no_region_for_a_village_a_city_or_a_scattered_trip():
    one_town = [(KOLINEC, "Kolinec")] * 6
    assert trips.region_name(_trip(one_town)) is None, "one town: its name, not the region"
    tight = [((KOLINEC[0] + 0.001 * i, KOLINEC[1]), "Kolinec" if i % 2 else "Tedrazice") for i in range(6)]
    assert trips.region_name(_trip(tight)) is None, "two hamlets 1 km apart are not a landscape"
    city = [((PRAHA[0] + 0.02 * (i % 3), PRAHA[1] + 0.03 * (i % 2)), f"Praha {i % 3 + 1}") for i in range(9)]
    assert trips.region_name(_trip(city)) is None, "Prague is not in a natural region"
    mixed = [(SUSICE, "Susice"), (PRAHA, "Praha"), (HORAZDOVICE, "Horazdovice")] * 3
    assert trips.region_name(_trip(mixed)) is None, "under 60 % inside the range"
    t, loc = trips.title_for(_trip([(KOLINEC, "Kolinec"), (HORAZDOVICE, "Horazdovice")] * 2), regional=True)
    assert loc == "Kolinec" and "a okolí" in t, "falls back to the town naming"


def test_top_level_trips_only_and_migration():
    src = io.open(os.path.join(HERE, "trips.py"), encoding="utf-8").read()
    assert "title_for(c.items, c.hub, regional=parent_id is None)" in src
    assert trips.ALGO_VERSION >= 4, "existing libraries re-name once"


def test_apply_writes_the_region_into_the_event():
    lib = Lib()
    lib.sumava_week()
    conn = _db(lib)
    trips.recluster(conn)
    rows = conn.execute("SELECT title, location_name, kind FROM events ORDER BY id").fetchall()
    tops = [r for r in rows if r[2] == trips.KIND_JOURNEY]
    assert len(tops) == 1 and tops[0][1] == "Šumava"
    assert all(r[1] != "Šumava" for r in rows if r[2] == trips.KIND_CHAPTER)


@pytest.mark.parametrize("lang,single,pair", [
    ("cs", "Šumava", "Šumava a Bavorský les"),
    ("en", "Šumava", "Šumava and Bavarian Forest"),
    ("de", "Böhmerwald", "Böhmerwald und Bayerischer Wald"),
    ("fr", "Šumava", "Šumava et Forêt bavaroise"),
    ("es", "Šumava", "Šumava y Selva de Baviera"),
    ("ja", "シュマヴァ", "シュマヴァとバイエルンの森"),
    ("zh-CN", "舒马瓦", "舒马瓦和巴伐利亚森林"),
])
def test_region_names_are_localized(lang, single, pair):
    before = i18n.current()
    try:
        i18n.set_language(lang, retranslate=False)
        assert i18n.event_place("Šumava") == single
        assert i18n.event_place("Šumava a Bavorský les").replace("⁨", "").replace("⁩", "") == pair
        assert i18n.event_place("Kolinec a okolí") == i18n.tr("event-city-area", city="Kolinec")
        assert i18n.event_place("Kolinec") == "Kolinec"
    finally:
        i18n.set_language(before, retranslate=False)


def test_every_part_has_all_languages_and_a_closed_polygon():
    for r in regions.RANGES:
        for p in r["parts"]:
            assert set(p["names"]) >= {"cs", "en", "de", "fr", "es", "ja", "zh-CN"}, p["id"]
            assert len(p["poly"]) >= 5
            assert p["cc"] in {"CZ", "DE", "AT", "PL", "SK"}
