# Proposal

## Why

Repeater collection now stores every poll, but the panel only shows the latest status and an
outcome-only poll list, so the history cannot actually be looked at: no trend of battery, noise or
traffic, and no way to see what an earlier poll returned. Retention is capped at 365 days, which
forces deletion of a record some operators want to keep indefinitely. And although most repeaters
are mains powered, some run on batteries, and nothing on the node says which — a falling battery is
only noticed when the repeater disappears.

## What Changes

- Retention window gains a **keep forever** setting as its maximum, above 365 days. Kept forever
  means pruning deletes nothing. Default stays 30 days. Stored as "no retention bound".
- Each repeater's metrics page gains **trend charts** over a selectable range (last 24 hours,
  7 days, 30 days, everything kept): battery voltage, noise floor, last RSSI and SNR, packets
  received and sent per hour, transmit airtime share, and neighbour count, with failed polls
  marked. Charts are drawn by the server; no chart library is added.
- Rates in the charts are derived from counter deltas between consecutive answered polls; a
  repeater restart (uptime or a counter going backwards) breaks the line instead of producing a
  spike. The latest-status table keeps showing counters exactly as reported.
- Each poll-history row **links to that poll**: its full status fields and neighbour list as
  returned at the time.
- Battery **capacity estimate** from the reported voltage using a single-cell Li-ion discharge
  curve. The contacts page notes, on a repeater row, when its latest recorded status estimates
  under 50%, with the voltage, estimate and reading time. Readings that do not look like a single
  Li-ion cell (below 2.5 V, including 0, or above 4.4 V) are treated as "no battery sensed" and
  never noted. The metrics page shows the estimate beside the voltage.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `repeater-metrics`: settings requirement allows no retention bound (keep forever); pruning
  requirement deletes nothing when kept forever; new requirement for the battery capacity
  estimate.
- `web-admin`: system-page collection form offers "keep forever" as the retention maximum.
- `web-dashboard`: metrics page gains trend charts with a range selector and per-poll drill-down;
  contact table gains the low-battery note.

## Impact

- `alembic/versions/0012_*`: `repeater_collection.retention_days` becomes nullable; check
  constraint rewritten to allow NULL.
- `src/sighop/db/models.py`, `src/sighop/db/repositories.py`: `CollectionSettings.retention_days`
  becomes `int | None`; new poll reads (time-range series, one poll by id with neighbours, latest
  status per key batch).
- `src/sighop/net/collect.py`: prune skipped when retention is unbounded.
- New pure module for battery estimate and chart series/SVG geometry (under `src/sighop/web/`).
- `src/sighop/web/routes/collect.py`, `routes/system.py`, templates `metrics.html`,
  `system.html`, `contacts.html`/`_collect_cell.html`, new poll-detail template, `panel.css`.
- Tests: repositories, collector prune, web admin, web dashboard, schema.
- No new dependency; no protocol change.
