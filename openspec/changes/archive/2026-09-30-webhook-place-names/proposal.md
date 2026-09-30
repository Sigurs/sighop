# Proposal

## Why

A new-node webhook shows advertised coordinates as a bare `59.329460, 18.068580` maps link. A reader
has to click it to learn where the node even is. Naming the place — neighborhood where known, city,
country — makes the notification readable at a glance.

## What Changes

- New offline place lookup: a gazetteer derived from GeoNames `cities1000` (populated places with
  population ≥ 1000, plus their named sections) and GeoNames country names, bundled as package data.
  No network call, no new runtime dependency, no coordinates leave the host.
- Lookup maps a position to a place: nearest city/town within a bounded distance, its country, and —
  when a named city section lies close by — a neighborhood. Beyond the distance bound (sea, wilderness)
  there is no place.
- Place is resolved once per event on the delivery side (never on the reception path), so every retry
  and every webhook names the same place. The gazetteer loads lazily off the event loop on first use;
  a load failure logs once and events go out without a place.
- Discord embed: the Location field shows the place (`Neighborhood, City, Country`) above the existing
  coordinate maps link; unchanged when no place resolves or no position was advertised.
- JSON (schema 1, additive): `node.position.place` object with `neighborhood` (or null), `city`,
  `country`, `country_code`; null when no place resolves.
- Sample (test) event resolves a place for its sample position like a real one.
- A maintainer script regenerates the bundled data file from GeoNames downloads; README credits
  GeoNames (CC BY 4.0).

## Capabilities

### New Capabilities
- `place-lookup`: offline reverse geocoding of a position to neighborhood/city/country from a bundled
  gazetteer, including its bounds, load behaviour and data provenance.

### Modified Capabilities
- `webhooks`: JSON position gains an additive `place`; Discord location field shows the place name;
  events resolve their place once before delivery; the sample event carries a resolved place.

## Impact

- New package `src/sighop/geo/` (gazetteer module + compressed data file, a few MB) shipped in the
  wheel and image.
- `src/sighop/webhooks/events.py` (`WebhookEvent` gains `place`), `render.py` (both formats),
  `dispatcher.py` (resolve on dispatch and in `send_sample`).
- New maintainer script `scripts/build_places.py`; README attribution.
- Tests: gazetteer unit tests on a small fixture, one test against the bundled data, render and
  dispatcher tests updated.
- Memory: gazetteer held in columnar arrays once loaded (roughly 28 MB resident, measured).
