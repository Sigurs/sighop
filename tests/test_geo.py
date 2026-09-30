"""Naming a position (webhook-place-names tasks 2.1-2.3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from sighop.geo import Gazetteer, Place, PlaceNamer
from sighop.geo.places import CITY_RADIUS_KM, distance_km

COUNTRIES = {"SE": "Sweden", "FJ": "Fiji", "NO": "Norway"}


@dataclass(frozen=True)
class Point:
    latitude: float
    longitude: float


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))


def _stockholm() -> Gazetteer:
    return Gazetteer(
        [
            ("c", 59.3293, 18.0686, "SE", "Stockholm"),
            ("c", 59.8586, 17.6389, "SE", "Uppsala"),
            ("s", 59.3150, 18.0700, "SE", "Södermalm"),
            ("s", 59.8586, 17.6389, "SE", "uppsala"),
        ],
        COUNTRIES,
    )


def test_a_section_within_3_km_is_the_neighborhood() -> None:
    place = _stockholm().place(59.3200, 18.0690)
    assert place == Place(
        neighborhood="Södermalm", city="Stockholm", country="Sweden", country_code="SE"
    )
    assert place.label == "Södermalm, Stockholm, Sweden"


def test_a_section_5_km_away_is_no_neighborhood() -> None:
    latitude = 59.3150 + 5 / 111.2
    place = _stockholm().place(latitude, 18.0700)
    assert place is not None
    assert place.neighborhood is None
    assert place.label == "Stockholm, Sweden"


def test_a_section_named_like_its_city_is_left_out() -> None:
    place = _stockholm().place(59.8580, 17.6390)
    assert place == Place(neighborhood=None, city="Uppsala", country="Sweden", country_code="SE")


def test_open_sea_has_no_place() -> None:
    assert _stockholm().place(57.0, 20.5) is None


def test_the_city_radius_is_50_km() -> None:
    gazetteer = Gazetteer([("c", 0.0, 0.0, "SE", "Edge")], COUNTRIES)
    inside = 49.9 / 111.19
    outside = 50.1 / 111.19
    assert distance_km(0, 0, inside, 0) < CITY_RADIUS_KM < distance_km(0, 0, outside, 0)
    assert gazetteer.place(inside, 0.0) is not None
    assert gazetteer.place(outside, 0.0) is None


def test_a_town_across_the_antimeridian_is_found() -> None:
    gazetteer = Gazetteer([("c", -16.0, -179.95, "FJ", "Eastside")], COUNTRIES)
    place = gazetteer.place(-16.0, 179.99)
    assert place is not None
    assert (place.city, place.country) == ("Eastside", "Fiji")


def test_high_latitudes_search_more_than_one_longitude_cell() -> None:
    # At 70°N a degree of longitude is ~38 km: a town 45 km east is two cells over.
    gazetteer = Gazetteer([("c", 70.0, 21.2, "NO", "Farther")], COUNTRIES)
    assert distance_km(70.0, 20.0, 70.0, 21.2) < CITY_RADIUS_KM
    place = gazetteer.place(70.0, 20.0)
    assert place is not None
    assert place.city == "Farther"


def test_equal_distances_go_to_the_earlier_row() -> None:
    rows = [("c", 0.0, 0.1, "SE", "First"), ("c", 0.0, -0.1, "SE", "Second")]
    assert Gazetteer(rows, COUNTRIES).place(0.0, 0.0).city == "First"
    assert Gazetteer(rows[::-1], COUNTRIES).place(0.0, 0.0).city == "Second"


def test_an_unknown_country_code_is_shown_as_itself() -> None:
    place = Gazetteer([("c", 1.0, 1.0, "ZZ", "Nowhere")], COUNTRIES).place(1.0, 1.0)
    assert (place.country, place.country_code) == ("ZZ", "ZZ")


# --- The bundled data (task 2.2) ---------------------------------------------


@pytest.fixture(scope="module")
def bundled() -> Gazetteer:
    return Gazetteer.load()


def test_the_bundled_gazetteer_names_stockholm(bundled: Gazetteer) -> None:
    place = bundled.place(59.329460, 18.068580)
    assert place is not None
    assert (place.city, place.country, place.country_code) == ("Stockholm", "Sweden", "SE")


def test_the_bundled_gazetteer_has_nothing_mid_atlantic(bundled: Gazetteer) -> None:
    assert bundled.place(35.0, -40.0) is None


# --- PlaceNamer (task 2.3) ---------------------------------------------------


def test_the_gazetteer_is_loaded_once() -> None:
    calls = []

    def loader() -> Gazetteer:
        calls.append(1)
        return _stockholm()

    namer = PlaceNamer(loader, RecordingLogger())

    async def run() -> list[Place | None]:
        first = await asyncio.gather(*(namer.name(Point(59.32, 18.069)) for _ in range(3)))
        return [*first, await namer.name(Point(59.858, 17.639))]

    places = asyncio.run(run())
    assert calls == [1]
    assert [p.city for p in places if p is not None] == ["Stockholm"] * 3 + ["Uppsala"]


def test_a_failed_load_is_logged_once_and_names_nothing() -> None:
    calls = []

    def loader() -> Gazetteer:
        calls.append(1)
        raise OSError("damaged")

    logger = RecordingLogger()
    namer = PlaceNamer(loader, logger)

    async def run() -> list[Place | None]:
        return [await namer.name(Point(59.32, 18.069)) for _ in range(3)]

    assert asyncio.run(run()) == [None, None, None]
    assert calls == [1]
    assert [(level, event) for level, event, _ in logger.events] == [
        ("error", "place_lookup_unavailable")
    ]


def test_no_position_names_nothing_without_loading() -> None:
    def loader() -> Gazetteer:
        raise AssertionError("loaded")

    assert asyncio.run(PlaceNamer(loader, RecordingLogger()).name(None)) is None


def test_a_lookup_that_raises_names_nothing() -> None:
    class Broken(Gazetteer):
        def place(self, latitude: float, longitude: float) -> Place | None:
            raise ValueError("bad")

    logger = RecordingLogger()
    namer = PlaceNamer(lambda: Broken([], {}), logger)
    assert asyncio.run(namer.name(Point(1.0, 1.0))) is None
    assert [event for _, event, _ in logger.events] == ["place_lookup_failed"]
