# Tasks

## 1. Retention keep forever

- [x] 1.1 Add migration `alembic/versions/0012_repeater_retention_forever.py` (D1): `retention_days` nullable, check `retention_days IS NULL OR BETWEEN 1 AND 365`; downgrade maps NULL→365 and restores NOT NULL + old check. Update `RepeaterCollectionRow` in `db/models.py` and the expected head in `tests/test_db_schema.py`; verify the head test and upgrade/downgrade/upgrade test pass
- [x] 1.2 `CollectionSettings.retention_days: int | None`, `validate()` skips range for None, `save()` accepts None (`db/repositories.py`); verify with new cases in `tests/test_repeater_repositories.py` (stores and reads back forever; 0 and 366 still refused)
- [x] 1.3 `RepeaterCollector._prune` deletes nothing when retention is None, still advances `_last_pruned` (D3); verify with `tests/test_collect.py` cases "kept forever" and "forever changed back to days"
- [x] 1.4 System form: `retention_forever` checkbox, blank days refused, forever shows default days in field (D2) in `web/routes/system.py` and `system.html`; verify with `tests/test_web_admin.py` cases for choosing forever, blank retention refused, junk still refused

## 2. Battery estimate

- [x] 2.1 Add `src/sighop/web/battery.py` with curve, `estimate()`, `is_low()` (D4); verify with new `tests/test_battery.py` covering 4.15 V (>90%, not low), 3.75 V (50%), 3.60 V (<50%, low), 0 V / 2.4 V / 5.1 V (None, not low), None (None)
- [x] 2.2 `RepeaterPollRepository.latest_status_for(keys)` (D5); verify in `tests/test_repeater_repositories.py` that a failed newer poll does not hide the older status and one query serves many keys
- [x] 2.3 `CollectCell.battery` populated in `collection_cells` for low readings only, rendered in `_collect_cell.html` with voltage, estimate and `display.when` time; verify in `tests/test_web_dashboard.py`: 3.60 V row marked, 0 V row unmarked, failed-latest-after-low still marked, companion row unmarked
- [x] 2.4 `stat_readings` battery row shows estimate or "no battery sensed" (D9); verify metrics page test for 3.95 V shows `3.950 V` and a percentage

## 3. Poll drill-down

- [x] 3.1 `RepeaterPollRepository.get(poll_id)` returning the poll with neighbours, None when missing; verify in `tests/test_repeater_repositories.py`
- [x] 3.2 Route `GET /contacts/{key}/metrics/polls/{poll_id}` and template `metrics_poll.html` (D9); history rows in `metrics.html` link to it; verify in `tests/test_web_dashboard.py`: old succeeded poll shows its own status/neighbours not latest, failed poll states no status, other repeater's key → 404, unknown id → 404

## 4. Charts

- [x] 4.1 `SeriesRow` and `RepeaterPollRepository.series(public_key, since=)` (D6); verify in `tests/test_repeater_repositories.py` ascending order, `since` bound, `None` returns all, failed polls included with null stats
- [x] 4.2 `src/sighop/web/charts.py` `derive()` (D7): gauges, per-hour packet rates, tx airtime %, gaps on restart/negative Δ/missing field; verify with `tests/test_charts.py` (restart gap, older-firmware gap, rate arithmetic)
- [x] 4.3 `reduce()` bounded to `MAX_CHART_POINTS` with min/max from raw, and `layout()` producing SVG geometry, failure ticks, latest/min/max; verify in `tests/test_charts.py` (10k points → ≤360, extremes preserved in stats, single-point and empty series)
- [x] 4.4 `_chart.html` macro (inline SVG, `<title>`, theme-aware colours in `panel.css`), `?range=` handling in `metrics` route (D8, default 7d, unknown → default), range links and empty-range message in `metrics.html`; verify in `tests/test_web_dashboard.py`: default 7d, `range=all` spans oldest poll, empty range message with wider-range links, page contains no `<script>` from charts and no external URL
- [x] 4.5 Update retention note on metrics page ("kept forever" vs N days); verify in dashboard test

## 5. Wrap-up

- [x] 5.1 Run `./build.sh` checks: `uv run --locked ruff format --check`, `uv run --locked ruff check`, `uv run --locked mypy`, `uv run --locked --env-file .env.dev pytest -q`; all pass
- [ ] 5.2 Render the metrics, poll, contacts and system pages against the dev database with seeded polls (restart, failed poll, low battery, forever retention) and check charts and marks visually in light and dark themes
