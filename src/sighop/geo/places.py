"""Naming a position from the bundled gazetteer (webhook-place-names D2, D4).

The gazetteer is GeoNames `cities1000` cut down by `scripts/build_places.py`:
populated places (`c`) and sections of them (`s`), each a point. A place is the
nearest city within `CITY_RADIUS_KM`, its country, and the nearest section within
`SECTION_RADIUS_KM` as neighborhood. Nearest, not containing: GeoNames has no
boundaries, and a rough name is what a notification needs.

Loading parses ~170k rows, so `PlaceNamer` does it once, off the event loop, on
first use; nothing here touches the network.

Data © GeoNames (https://www.geonames.org), licensed CC BY 4.0.
"""

from __future__ import annotations

import asyncio
import gzip
import math
import sys
import threading
from array import array
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Protocol

from sighop.logging import Logger, get_logger

CITY_RADIUS_KM = 50.0
SECTION_RADIUS_KM = 3.0
EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = 111.2
PLACES_FILE = "places.tsv.gz"
COUNTRIES_FILE = "countries.tsv"
CITY = "c"
SECTION = "s"

type Cell = tuple[int, int]
type Row = tuple[str, float, float, str, str]
"""kind, latitude, longitude, country code, name — a data-file row."""


class Located(Protocol):
    @property
    def latitude(self) -> float: ...
    @property
    def longitude(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Place:
    neighborhood: str | None
    city: str
    country: str
    country_code: str

    @property
    def label(self) -> str:
        """`neighborhood, city, country`, the neighborhood left out when absent."""
        parts = [self.neighborhood, self.city, self.country]
        return ", ".join(part for part in parts if part is not None)


def distance_km(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    """Great-circle (haversine) distance."""
    phi_a, phi_b = math.radians(latitude_a), math.radians(latitude_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(longitude_b - longitude_a)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _cell(latitude: float, longitude: float) -> Cell:
    return math.floor(latitude), math.floor(longitude)


def _cells_within(latitude: float, longitude: float, radius_km: float) -> set[Cell]:
    """Every 1° cell a point within `radius_km` could lie in, longitude wrapped."""
    reach = radius_km / KM_PER_DEGREE
    south = max(-90, math.floor(latitude - reach))
    north = min(89, math.floor(latitude + reach))
    widest = math.cos(math.radians(min(90.0, max(abs(latitude - reach), abs(latitude + reach)))))
    if widest * KM_PER_DEGREE * 180 <= radius_km:
        longitudes = range(-180, 180)
    else:
        span = reach / widest
        west = math.floor(longitude - span)
        east = math.floor(longitude + span)
        longitudes = range(west, east + 1) if east - west < 360 else range(-180, 180)
    return {
        (row, (column + 180) % 360 - 180)
        for row in range(south, north + 1)
        for column in longitudes
    }


class _Points:
    """One kind of place, column-stored and bucketed by 1° cell."""

    def __init__(self) -> None:
        self.latitudes = array("d")
        self.longitudes = array("d")
        self.codes: list[str] = []
        self.names: list[str] = []
        self.grid: dict[Cell, array[int]] = {}

    def add(self, latitude: float, longitude: float, code: str, name: str) -> None:
        index = len(self.names)
        self.latitudes.append(latitude)
        self.longitudes.append(longitude)
        self.codes.append(sys.intern(code))
        self.names.append(name)
        self.grid.setdefault(_cell(latitude, longitude), array("I")).append(index)

    def nearest(self, latitude: float, longitude: float, radius_km: float) -> int | None:
        """The nearest point's index within the radius; ties go to the earlier row."""
        best: tuple[float, int] | None = None
        for cell in _cells_within(latitude, longitude, radius_km):
            for index in self.grid.get(cell, ()):
                distance = distance_km(
                    latitude, longitude, self.latitudes[index], self.longitudes[index]
                )
                if distance <= radius_km and (best is None or (distance, index) < best):
                    best = (distance, index)
        return None if best is None else best[1]


class Gazetteer:
    def __init__(self, rows: Iterable[Row], countries: Mapping[str, str]) -> None:
        self._cities = _Points()
        self._sections = _Points()
        self._countries = dict(countries)
        for kind, latitude, longitude, code, name in rows:
            points = self._sections if kind == SECTION else self._cities
            points.add(latitude, longitude, code, name)

    @classmethod
    def load(cls) -> Gazetteer:
        """The gazetteer bundled with sighop."""
        folder = resources.files(__package__ or "sighop.geo")
        countries = {}
        for line in (folder / COUNTRIES_FILE).read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                code, name = line.split("\t", 1)
                countries[code] = name
        with (
            (folder / PLACES_FILE).open("rb") as raw,
            gzip.open(raw, "rt", encoding="utf-8") as text,
        ):
            return cls((_row(line) for line in text if not line.startswith("#")), countries)

    def place(self, latitude: float, longitude: float) -> Place | None:
        city = self._cities.nearest(latitude, longitude, CITY_RADIUS_KM)
        if city is None:
            return None
        city_name = self._cities.names[city]
        code = self._cities.codes[city]
        neighborhood = None
        section = self._sections.nearest(latitude, longitude, SECTION_RADIUS_KM)
        if section is not None and self._sections.names[section].casefold() != city_name.casefold():
            neighborhood = self._sections.names[section]
        return Place(
            neighborhood=neighborhood,
            city=city_name,
            country=self._countries.get(code, code),
            country_code=code,
        )


def _row(line: str) -> Row:
    kind, latitude, longitude, code, name = line.rstrip("\n").split("\t")
    return kind, float(latitude), float(longitude), code, name


class PlaceNamer:
    """Names positions, loading the gazetteer once on first use (design D4).

    A load that fails is logged once and remembered: every later position has
    no place, and nothing else is affected.
    """

    def __init__(
        self,
        loader: Callable[[], Gazetteer] = Gazetteer.load,
        logger: Logger | None = None,
    ) -> None:
        self._loader = loader
        self._log = logger or get_logger(component="geo")
        self._lock = threading.Lock()
        self._gazetteer: Gazetteer | None = None
        self._failed = False

    async def name(self, position: Located | None) -> Place | None:
        if position is None:
            return None
        gazetteer = await self._loaded()
        if gazetteer is None:
            return None
        try:
            return gazetteer.place(position.latitude, position.longitude)
        except Exception as exc:
            self._log.error(
                "place_lookup_failed", outcome="error", error=f"{type(exc).__name__}: {exc}"
            )
            return None

    async def _loaded(self) -> Gazetteer | None:
        if self._gazetteer is not None or self._failed:
            return self._gazetteer
        return await asyncio.to_thread(self._load_once)

    def _load_once(self) -> Gazetteer | None:
        """In a worker thread. A thread lock, not an asyncio one, so the shared
        namer is not tied to whichever event loop used it first."""
        with self._lock:
            if self._gazetteer is None and not self._failed:
                try:
                    self._gazetteer = self._loader()
                except Exception as exc:
                    self._failed = True
                    self._log.error(
                        "place_lookup_unavailable",
                        outcome="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
            return self._gazetteer


default_namer = PlaceNamer()
"""The process's namer, shared by the dispatcher and the webhook test."""
