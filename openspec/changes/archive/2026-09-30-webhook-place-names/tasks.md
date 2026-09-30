# Tasks

## 1. Gazetteer data

- [x] 1.1 Write `scripts/build_places.py` (stdlib only): download `cities1000.zip` and `countryInfo.txt` from `download.geonames.org/export/dump/`, filter per design D1, write `src/sighop/geo/places.tsv.gz` (header with download date and GeoNames CC BY 4.0 credit; rows sorted by GeoNames id, coordinates at 4 decimals) and `src/sighop/geo/countries.tsv`; verify by running it twice and confirming byte-identical output apart from the date line
- [x] 1.2 Generate and commit the data files; check gzipped size (design Open Question: >5 MB → 3 decimals or lzma) and `uv build` wheel lists both files (`unzip -l dist/*.whl | grep geo/`)

## 2. Place lookup (`sighop.geo`)

- [x] 2.1 Add `src/sighop/geo/__init__.py` with frozen `Place` and `Gazetteer` (columnar stores, 1° grid, haversine, 50 km city / 3 km section rules, name-equals-city drop, antimeridian wrap, stable tie-break, unknown code → code) per design D2; unit tests on a small in-memory fixture cover each `place-lookup` scenario (section within 3 km, section at 5 km, open sea, antimeridian, tie-break, same-name section) — `uv run pytest tests/test_geo.py`
- [x] 2.2 Add `Gazetteer.load()` reading the bundled files via `importlib.resources`, and a test against the real data: 59.329460, 18.068580 → city `Stockholm`, country `Sweden`, code `SE`; a mid-Atlantic point → `None`
- [x] 2.3 Add `PlaceNamer` (async, `asyncio.Lock`, `to_thread` load once, cached failure logged once as `place_lookup_unavailable`, lookup exception → `None` + log) and module-level default instance per design D4; tests: loader called once across several `name()` calls, failing loader logs once and returns `None`, `None` position → `None` without loading

## 3. Webhook events and rendering

- [x] 3.1 Add `place: Place | None = None` to `WebhookEvent`; verify existing webhook tests still pass
- [x] 3.2 JSON: `node.position.place` object or `null` in `render_json`; tests for named place, place with null neighborhood, position with no place, no position (position stays `null`)
- [x] 3.3 Discord: `discord_location(position, place)` puts the escaped `neighborhood, city, country` line (neighborhood omitted when absent) above the maps link; tests for the three Location scenarios plus a place name with markdown characters shown literally
- [x] 3.4 Update module docstring of `render.py` to mention the place line and `null` place

## 4. Resolution before delivery

- [x] 4.1 `WebhookDispatcher` takes `places: PlaceNamer` (default module instance) and resolves the place in `run()` after popping, before `dispatch()`, via `dataclasses.replace`; tests with a fake namer: json + discord webhooks receive the same place, a retried delivery carries the same place, namer called once per event, event without position never calls namer, namer returning `None` still delivers
- [x] 4.2 `send_sample` resolves the sample position's place with the same namer (keyword-injectable); test that a Discord sample message shows the place line and the link
- [x] 4.3 Confirm `on_observation` does no lookup (test: namer untouched until `run()` processes the event)

## 5. Docs and gates

- [x] 5.1 README: GeoNames attribution (CC BY 4.0) and one line on how to regenerate the data with `scripts/build_places.py`
- [ ] 5.2 Run `uv run ruff check`, `uv run ruff format --check`, type check and full `uv run pytest`; all green
- [x] 5.3 Manual: send a Discord test webhook from the panel and confirm the Location field shows `…Stockholm, Sweden` above the coordinates link
