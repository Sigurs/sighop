# Design

## Context

See proposal.md — Why. Relevant current state:

- `repeater_collection.retention_days` is `NOT NULL` with check `BETWEEN 1 AND 365`
  (migration 0011). `CollectionSettings.retention_days: int`, range
  `COLLECTION_RETENTION_DAYS_RANGE = (1, 365)` in `db/repositories.py`; validated in
  `CollectionSettings.validate()`, parsed by `_whole_number` in `web/routes/system.py`.
- `RepeaterCollector._prune` (`net/collect.py`) always prunes `now - retention_days`.
- `repeater_poll` holds one row per poll with every `RepeaterStats` field as its own nullable
  column (`last_snr` as `last_snr_db`), `neighbours_total`, `outcome`, `route`, `reason`; index
  `(public_key, started_at)` already exists. `_poll()` rebuilds `RepeaterStats` only when
  `batt_milli_volts` and `last_snr_db` are non-null — "returned a status" is keyed off
  `batt_milli_volts IS NOT NULL` (as `latest_with_status` does).
- Metrics page (`web/routes/collect.py`, `metrics.html`) shows latest status, neighbours and
  `history(limit=200)`. Contacts cell (`_collect_cell.html`) uses `latest_for(keys)` which has no
  stats guarantee (latest poll may be failed).
- Panel: htmx + Jinja, static vendored JS only, timestamps `<time class="when">` localised by
  `display.js`. No chart code exists.

## Goals / Non-Goals

**Goals:**
- Retention "forever" without a sentinel number.
- Charts readable at hourly polling over a year kept forever, with bounded page weight.
- Battery estimate is one pure function used by contacts, metrics and poll pages.

**Non-Goals:**
- Per-neighbour history (SNR of a given neighbour over time).
- Configurable battery chemistry or threshold; multi-cell packs.
- Interactive charts (zoom, hover tooltips beyond SVG `<title>`), CSV export.
- Battery marks for companions/rooms (only repeaters are polled).

## Decisions

### D1. Keep forever is `retention_days = NULL`
Migration `0012_repeater_retention_forever`: `ALTER COLUMN retention_days DROP NOT NULL`, drop
`ck_repeater_collection_retention_days`, recreate as
`retention_days IS NULL OR retention_days BETWEEN 1 AND 365`. Downgrade: `UPDATE ... SET
retention_days = 365 WHERE retention_days IS NULL`, restore NOT NULL and old check.
`CollectionSettings.retention_days: int | None`; `validate()` skips the range check for `None`.
Room retention already uses NULL for "no bound", so this matches house style.
*Alternative:* sentinel `0` or `36500` — rejected: leaks a magic number into the form, the check
constraint and every reader.

### D2. Form: a "keep forever" checkbox beside the days field
The system form gets `retention_forever` (checkbox, value `true`). Ticked → `None`, days field
ignored (but re-shown on refusal). Unticked → days parsed by `_whole_number` as today; blank is
refused ("not a whole number"), never read as forever. Label states range "1–365, or keep
forever" and that nothing collected is then deleted. When stored as forever the days field shows
the default (30) so unticking yields a sane value.
*Alternative:* accept the word "forever" in the text field — rejected: typo-prone, and junk must
be refused not coerced.

### D3. Prune skipped when unbounded
`_prune` still stamps `_last_pruned` (keeps cadence logic unchanged) but issues no delete when
`retention_days is None`. Metrics page note reads "kept forever" instead of "N days".

### D4. Battery estimate: pure function, piecewise-linear 1S Li-ion table
New module `src/sighop/web/battery.py` (web-only consumer, pure, no I/O):

```
_CURVE_MV = ((3300, 0), (3400, 5), (3500, 12), (3600, 25), (3700, 42),
             (3750, 50), (3800, 56), (3900, 68), (4000, 80), (4100, 90), (4200, 100))
NO_BATTERY_BELOW_MV = 2500; NO_BATTERY_ABOVE_MV = 4400; LOW_BELOW_PERCENT = 50
def estimate(mv: int | None) -> int | None   # None = not reported or no battery sensed
def is_low(mv: int | None) -> bool           # estimate is not None and < 50
```
Clamps 2500–3300 → 0 and 4200–4400 → 100. The curve is rough by nature (reading taken under
load, cell ageing); the spec fixes only the anchor points and outcomes so the table can be tuned
without a spec change.
*Alternative:* linear 3.3–4.2 V — rejected: Li-ion is flat mid-range, linear overstates drain
near 3.8 V.

### D5. Contacts low-battery mark uses the latest status-bearing poll per key
New `RepeaterPollRepository.latest_status_for(keys) -> dict[bytes, PollRecord]` — same
`DISTINCT ON (public_key)` query as `latest_for` with `batt_milli_volts IS NOT NULL`, no
neighbours. `collection_cells` gains one query and `CollectCell.battery: BatteryMark | None`
(mv, percent, collected_at), set only when `is_low`. Applies to every repeater row with any
stored status, not only currently selected ones — a deselected repeater's last reading is still
the best knowledge. Rendered in `_collect_cell.html` as a warning line
("low battery — 3.60 V, ~25%, <time>"), so the htmx re-render after ticking keeps it.
*Alternative:* derive from `latest_for` — rejected: a failed latest poll would hide a low
battery (spec scenario "A failed poll after a low reading").

### D6. Series read: one range query, columns only
`RepeaterPollRepository.series(public_key, *, since: datetime | None) -> list[SeriesRow]` —
selects `started_at, outcome, batt_milli_volts, noise_floor, last_rssi, last_snr_db,
n_packets_recv, n_packets_sent, total_air_time_secs, total_up_time_secs, neighbours_total`
ordered by `started_at ASC, id ASC`; `since=None` for "everything kept". `SeriesRow` is a frozen
dataclass in `repositories.py` (collector-record pattern). Uses existing
`(public_key, started_at)` index. A year of hourly polls is ~8.8k narrow rows — acceptable.

### D7. Chart model built in a pure module; SVG emitted by a Jinja macro
`src/sighop/web/charts.py` (pure):
- `derive(rows) -> dict[str, list[Point | None]]`: gauges direct (battery V, noise dBm, RSSI dBm,
  SNR dB, neighbours total). Rates between consecutive status rows *a→b*:
  `Δcounter / Δt_hours` for rx/tx packets, `Δair_secs / Δt_secs × 100` for tx airtime %; point
  placed at *b*. No rate when Δuptime < 0, Δcounter < 0, Δt ≤ 0, or either value is None
  (restart / older firmware). A gap is `None` in the list → polyline split.
- `reduce(points, max_points=MAX_CHART_POINTS=360)`: bucket consecutive points into equal-count
  buckets, plot bucket mean at bucket mean time; min/max stats computed on the unreduced series.
- `layout(series, start, end, width=640, height=120) -> ChartView`: scaled polyline segments as
  SVG point strings, y-axis min/max ticks, x-axis start/end labels, latest/min/max text, and
  failed-poll tick x positions (outcomes ≠ succeeded/neighbours_incomplete with no status).
Template macro `_chart.html` renders `<svg viewBox=... role="img">` with `<title>`, polylines,
failure ticks, CSS-var colours from `panel.css` (theme-aware). No JS; axis end labels use
`display.when` so `display.js` localises them.
*Alternative:* vendoring uPlot/Chart.js — rejected: new static dependency and JS requirement for
a feature SVG covers; the panel works without JS.

### D8. Range selector is a GET parameter
`/contacts/{key}/metrics?range=24h|7d|30d|all`, default `7d`; unknown value → default. Rendered
as plain links (current one marked). With retention not forever, "all" is bounded by retention
anyway. Empty range → message plus links to wider ranges.

### D9. Poll drill-down route
`GET /contacts/{key}/metrics/polls/{poll_id}` → new `RepeaterPollRepository.get(poll_id)` with
neighbours (reuse `latest_with_status` neighbour load). 404 when missing or
`poll.public_key != key`. Template `metrics_poll.html` reuses `stat_readings` and
`neighbour_views`; `stat_readings` battery row gains the estimate (`"3.950 V · ~72%"` or
`"0.000 V · no battery sensed"`). History rows in `metrics.html` link their started time.

## Risks / Trade-offs

- [Voltage curve wrong for a given board, or mains board ADC floats in 2.5–4.4 V] → false
  low-battery mark; accepted per user ("note if"); table tunable in one place.
- [Keep forever grows `repeater_poll`/`repeater_neighbour` unbounded] → ~9k polls and ≤~100k
  neighbour rows per repeater-year at hourly polling; charts read only narrow columns and reduce
  to 360 points. Documented on the form.
- [Averaging hides a single spike] → min/max stats are computed from raw points and shown per
  chart.
- [Counter wrap (uint32)] → treated like a restart (negative Δ → gap); negligible at mesh rates.

## Migration Plan

Deploy runs `alembic upgrade head` (0012) as usual; existing row keeps its days value. Rollback:
downgrade sets NULL → 365 before restoring NOT NULL, so no data loss beyond what 365-day pruning
would then remove.
