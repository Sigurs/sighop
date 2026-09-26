"""Chart series, reduction and geometry (repeater-metrics-history 4.2 / 4.3)."""

from __future__ import annotations

import datetime as dt

from sighop.db.repositories import PollOutcome, SeriesRow
from sighop.web.charts import (
    MAX_CHART_POINTS,
    SPECS,
    Point,
    charts,
    derive,
    layout,
    reduce,
)

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
SPEC = {spec.key: spec for spec in SPECS}


def _row(hours: float, **fields: object) -> SeriesRow:
    values: dict[str, object] = {
        "batt_milli_volts": 4000,
        "noise_floor": -110,
        "last_rssi": -80,
        "last_snr_db": 6.25,
        "n_packets_recv": 1000,
        "n_packets_sent": 500,
        "total_air_time_secs": 100,
        "total_up_time_secs": 10_000,
        "neighbours_total": 3,
    }
    values.update(fields)
    return SeriesRow(started_at=T0 + hours * HOUR, outcome=PollOutcome.SUCCEEDED, **values)  # type: ignore[arg-type]


def _failed(hours: float) -> SeriesRow:
    return SeriesRow(started_at=T0 + hours * HOUR, outcome=PollOutcome.LOGIN_UNANSWERED)


def _values(series: list[Point | None]) -> list[float | None]:
    return [None if p is None else round(p.value, 6) for p in series]


# --- derive -----------------------------------------------------------------


def test_gauges_come_straight_from_each_answered_poll() -> None:
    derived = derive([_row(0), _failed(1), _row(2, batt_milli_volts=3900, neighbours_total=5)])
    assert _values(derived["battery"]) == [4.0, 3.9]
    assert _values(derived["snr"]) == [6.25, 6.25]
    assert _values(derived["neighbours"]) == [3, 5]
    assert [p.at for p in derived["battery"] if p] == [T0, T0 + 2 * HOUR]


def test_rates_are_counter_deltas_over_elapsed_time_placed_at_the_later_poll() -> None:
    derived = derive(
        [
            _row(0),
            _row(
                2,
                n_packets_recv=1300,
                n_packets_sent=600,
                total_air_time_secs=172,
                total_up_time_secs=17_200,
            ),
        ]
    )
    assert _values(derived["rx"]) == [150.0]
    assert _values(derived["tx"]) == [50.0]
    assert _values(derived["airtime"]) == [1.0], "72 s of 7200 s"
    assert derived["rx"][0] is not None and derived["rx"][0].at == T0 + 2 * HOUR


def test_a_failed_poll_between_answered_ones_does_not_break_the_rate() -> None:
    derived = derive([_row(0), _failed(1), _row(2, n_packets_recv=1200, total_up_time_secs=17_200)])
    assert _values(derived["rx"]) == [100.0]


def test_a_restart_leaves_a_gap_not_a_negative_or_a_spike() -> None:
    derived = derive(
        [
            _row(0),
            _row(1, n_packets_recv=1100, total_up_time_secs=13_600),
            _row(2, n_packets_recv=50, total_up_time_secs=3000),
            _row(3, n_packets_recv=150, total_up_time_secs=6600),
        ]
    )
    assert _values(derived["rx"]) == [100.0, None, 100.0]
    assert _values(derived["tx"]) == [0.0, None, 0.0]


def test_uptime_going_back_breaks_rates_even_when_counters_did_not() -> None:
    derived = derive(
        [
            _row(0),
            _row(1, n_packets_recv=5000, total_up_time_secs=100),
            _row(2, n_packets_recv=5100),
        ]
    )
    assert _values(derived["rx"]) == [100.0], "the restart interval is dropped, no leading gap"


def test_older_firmware_leaves_gaps_in_that_series_only() -> None:
    derived = derive(
        [
            _row(0),
            _row(1, noise_floor=None, total_air_time_secs=None),
            _row(2, total_air_time_secs=300),
            _row(3, total_air_time_secs=336),
        ]
    )
    assert _values(derived["noise"]) == [-110, None, -110, -110]
    assert _values(derived["airtime"]) == [1.0], "no rate across a missing value"
    assert None not in derived["rx"]
    assert _values(derived["battery"]) == [4.0, 4.0, 4.0, 4.0]


def test_no_answered_poll_gives_empty_series() -> None:
    derived = derive([_failed(0), _failed(1)])
    assert all(series == [] for series in derived.values())


# --- reduce -----------------------------------------------------------------


def _many(count: int) -> list[Point | None]:
    return [Point(T0 + i * HOUR, float(i % 100)) for i in range(count)]


def test_a_short_series_is_not_reduced() -> None:
    points = _many(10)
    assert reduce(points) == points


def test_ten_thousand_points_are_reduced_to_the_bound() -> None:
    reduced = reduce(_many(10_000))
    assert 0 < sum(p is not None for p in reduced) <= MAX_CHART_POINTS


def test_reduction_averages_consecutive_points() -> None:
    points = [Point(T0 + i * HOUR, float(i)) for i in range(4)]
    assert reduce(points, max_points=2) == [
        Point(T0 + 0.5 * HOUR, 0.5),
        Point(T0 + 2.5 * HOUR, 2.5),
    ]


def test_reduction_stays_bounded_however_many_gaps() -> None:
    points: list[Point | None] = []
    for i in range(2000):
        points.extend([Point(T0 + i * HOUR, 1.0), None])
    reduced = reduce(points)
    assert sum(p is not None for p in reduced) <= MAX_CHART_POINTS
    assert None in reduced


# --- layout -----------------------------------------------------------------


def test_extremes_survive_reduction_in_the_stated_minimum_and_maximum() -> None:
    points = [Point(T0 + i * HOUR, 1.0) for i in range(10_000)]
    points[5000] = Point(points[5000].at, 99.0)
    points[7000] = Point(points[7000].at, -42.0)
    view = layout(SPEC["neighbours"], points, start=T0, end=T0 + 10_000 * HOUR)
    assert view.maximum == "99" and view.minimum == "-42"
    assert sum(len(line.split()) for line in view.lines) <= MAX_CHART_POINTS
    assert view.latest == "1"


def test_geometry_fits_the_viewbox_and_breaks_at_gaps() -> None:
    raw: list[Point | None] = [
        Point(T0, 3.0),
        Point(T0 + HOUR, 4.0),
        None,
        Point(T0 + 3 * HOUR, 5.0),
    ]
    view = layout(SPEC["battery"], raw, start=T0, end=T0 + 4 * HOUR)
    assert len(view.lines) == 2
    coordinates = [
        tuple(map(float, pair.split(","))) for line in view.lines for pair in line.split()
    ]
    assert all(0 <= px <= view.width and 0 <= py <= view.height for px, py in coordinates)
    assert coordinates[0][0] == 0.0
    assert coordinates[0][1] > coordinates[-1][1], "a higher value is drawn higher"
    assert (view.latest, view.minimum, view.maximum) == ("5.000 V", "3.000 V", "5.000 V")


def test_a_single_point_is_drawn_as_a_dot() -> None:
    view = layout(SPEC["snr"], [Point(T0 + HOUR, 2.5)], start=T0, end=T0 + 2 * HOUR)
    assert len(view.lines) == 1 and len(view.lines[0].split()) == 2
    assert view.latest == view.minimum == view.maximum == "+2.50 dB"


def test_an_empty_series_is_empty_but_still_marks_failures() -> None:
    view = layout(
        SPEC["rx"],
        [],
        start=T0,
        end=T0 + 2 * HOUR,
        failed=[(T0 + HOUR, PollOutcome.LOGIN_UNANSWERED)],
    )
    assert view.empty and view.lines == []
    assert [(mark.x, mark.outcome) for mark in view.failures] == [
        (view.width / 2, PollOutcome.LOGIN_UNANSWERED)
    ]


def test_charts_mark_failed_polls_on_every_chart() -> None:
    rows = [_row(0), _failed(1), _row(2)]
    views = charts(rows, start=T0, end=T0 + 2 * HOUR)
    assert [view.key for view in views] == [spec.key for spec in SPECS]
    assert all(len(view.failures) == 1 for view in views)
