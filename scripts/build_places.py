"""Regenerate sighop's bundled gazetteer from GeoNames (webhook-place-names design D1).

    uv run python scripts/build_places.py

Downloads `cities1000.zip` and `countryInfo.txt` from the GeoNames export and
writes `src/sighop/geo/places.tsv.gz` and `src/sighop/geo/countries.tsv`. Output
depends only on the downloads: rows are sorted by GeoNames id, the gzip header
carries no timestamp, and the header line dates the data by the server's
Last-Modified rather than by when this ran. Stdlib only.

Data © GeoNames (https://www.geonames.org), licensed CC BY 4.0.
"""

from __future__ import annotations

import email.utils
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

EXPORT = "https://download.geonames.org/export/dump/"
CITIES = "cities1000"
COUNTRIES = "countryInfo.txt"
OUT = Path(__file__).resolve().parent.parent / "src" / "sighop" / "geo"
CREDIT = "Data (c) GeoNames https://www.geonames.org CC BY 4.0"
COORDINATE_DECIMALS = 4

SECTION = "PPLX"
DROPPED = frozenset({"PPLH", "PPLQ", "PPLW", "PPLCH"})
"""Historical, abandoned, destroyed and religious-centre places: not somewhere a node is."""


def _download(name: str) -> tuple[bytes, str]:
    request = urllib.request.Request(EXPORT + name, headers={"User-Agent": "sighop build_places"})
    with urllib.request.urlopen(request, timeout=120) as response:
        modified = response.headers.get("Last-Modified")
        body = response.read()
    date = email.utils.parsedate_to_datetime(modified).date().isoformat() if modified else "unknown"
    return body, date


def _coordinate(text: str) -> str:
    return f"{float(text):.{COORDINATE_DECIMALS}f}"


def places(archive: bytes) -> list[tuple[int, str]]:
    """(geonames id, row) for every kept place, sorted by id."""
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        text = bundle.read(f"{CITIES}.txt").decode("utf-8")
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        columns = line.split("\t")
        feature_class, feature_code = columns[6], columns[7]
        if feature_class != "P" or feature_code in DROPPED:
            continue
        name = " ".join(columns[1].split())
        if not name:
            continue
        kind = "s" if feature_code == SECTION else "c"
        latitude, longitude = _coordinate(columns[4]), _coordinate(columns[5])
        rows.append((int(columns[0]), f"{kind}\t{latitude}\t{longitude}\t{columns[8]}\t{name}"))
    rows.sort()
    return rows


def countries(text: str) -> list[str]:
    """`code<TAB>English name`, sorted by code."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        rows.append(f"{columns[0]}\t{columns[4]}")
    return sorted(rows)


def main() -> int:
    archive, cities_date = _download(f"{CITIES}.zip")
    info, countries_date = _download(COUNTRIES)
    kept = places(archive)
    header = f"# GeoNames {CITIES} {cities_date}; {CREDIT}\n"
    body = header + "".join(f"{row}\n" for _, row in kept)
    with (
        (OUT / "places.tsv.gz").open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as out,
    ):
        out.write(body.encode("utf-8"))
    country_rows = countries(info.decode("utf-8"))
    (OUT / "countries.tsv").write_text(
        f"# GeoNames {COUNTRIES} {countries_date}; {CREDIT}\n"
        + "".join(f"{row}\n" for row in country_rows),
        encoding="utf-8",
    )
    print(f"{len(kept)} places, {len(country_rows)} countries -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
