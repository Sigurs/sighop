"""A repeater's metrics as chart geometry, drawn without scripts (repeater-metrics-history D7).

Pure: poll rows in, SVG point strings and labels out; `_chart.html` turns a
`ChartView` into inline SVG. Three steps:

* `derive` — gauges straight from each poll that returned a status; rates
  from the change in a counter between consecutive such polls, divided by
  the time between them. A restart (uptime or the counter going backwards),
  a field an older firmware did not report, or no time elapsed leaves a gap
  (`None`) rather than a spike or a zero.
* `reduce` — at most `MAX_CHART_POINTS` points by averaging consecutive
  points in equal-count buckets. Latest, minimum and maximum are taken from
  the unreduced series, so averaging never hides an extreme from the text.
* `layout` — scales to a fixed viewBox and splits the polyline at gaps.

Polls that returned no status contribute no point; `layout` marks their times
on the axis instead.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from sighop.db.repositories import PollOutcome, SeriesRow

MAX_CHART_POINTS = 360
WIDTH = 640
HEIGHT = 120
PAD = 6
"""Vertical room inside the viewBox so a stroke at the extremes is not clipped."""
TICK = 10
"""Height of a failed-poll mark at the foot of the chart."""


@dataclass(frozen=True, slots=True)
class Point:
    at: dt.datetime
    value: float


type Series = list[Point | None]
"""Points in time order; `None` breaks the line."""


@dataclass(frozen=True, slots=True)
class ChartSpec:
    key: str
    title: str
    render: Callable[[float], str]


def _fixed(places: int, unit: str) -> Callable[[float], str]:
    return lambda v: f"{v:.{places}f} {unit}"


SPECS = (
    ChartSpec("battery", "battery voltage", _fixed(3, "V")),
    ChartSpec("noise", "noise floor", _fixed(0, "dBm")),
    ChartSpec("rssi", "last RSSI", _fixed(0, "dBm")),
    ChartSpec("snr", "last SNR", lambda v: f"{v:+.2f} dB"),
    ChartSpec("rx", "packets received per hour", _fixed(1, "/h")),
    ChartSpec("tx", "packets sent per hour", _fixed(1, "/h")),
    ChartSpec("airtime", "transmit airtime share", _fixed(2, "%")),
    ChartSpec("neighbours", "neighbours", lambda v: f"{v:.0f}"),
)


def has_status(row: SeriesRow) -> bool:
    """A poll returned a status: keyed off the battery field, as the stored
    record is."""
    return row.batt_milli_volts is not None


# --- derive -----------------------------------------------------------------


def derive(rows: Sequence[SeriesRow]) -> dict[str, Series]:
    """Every chart's raw series from poll rows in time order."""
    answered = [row for row in rows if has_status(row)]
    gauges: dict[str, Callable[[SeriesRow], float | None]] = {
        "battery": lambda r: None if r.batt_milli_volts is None else r.batt_milli_volts / 1000,
        "noise": lambda r: r.noise_floor,
        "rssi": lambda r: r.last_rssi,
        "snr": lambda r: r.last_snr_db,
        "neighbours": lambda r: r.neighbours_total,
    }
    series: dict[str, Series] = {key: [] for key in (spec.key for spec in SPECS)}
    for key, read in gauges.items():
        for row in answered:
            value = read(row)
            series[key].append(None if value is None else Point(row.started_at, float(value)))
    for before, after in pairwise(answered):
        seconds = (after.started_at - before.started_at).total_seconds()
        restarted = _delta(before.total_up_time_secs, after.total_up_time_secs)
        valid = seconds > 0 and restarted is not None
        for key, counter, scale in (
            ("rx", "n_packets_recv", 3600.0),
            ("tx", "n_packets_sent", 3600.0),
            ("airtime", "total_air_time_secs", 100.0),
        ):
            change = _delta(getattr(before, counter), getattr(after, counter))
            rate = change / seconds * scale if valid and change is not None else None
            series[key].append(None if rate is None else Point(after.started_at, rate))
    return {key: _tidy(points) for key, points in series.items()}


def _delta(before: int | None, after: int | None) -> int | None:
    """The change, or None when either is missing or it went backwards."""
    if before is None or after is None or after < before:
        return None
    return after - before


def _tidy(points: Sequence[Point | None]) -> Series:
    """No leading, trailing or doubled gaps."""
    tidy: Series = []
    for point in points:
        if point is None and (not tidy or tidy[-1] is None):
            continue
        tidy.append(point)
    while tidy and tidy[-1] is None:
        tidy.pop()
    return tidy


# --- reduce -----------------------------------------------------------------


def reduce(points: Sequence[Point | None], max_points: int = MAX_CHART_POINTS) -> Series:
    """At most `max_points` points, each the mean of consecutive raw points.

    A bucket that spans a gap is preceded by one, so the line still breaks
    near where it did; buckets never exceed `max_points`, however many gaps.
    """
    count = sum(point is not None for point in points)
    if count <= max_points:
        return list(points)
    size = math.ceil(count / max_points)
    reduced: Series = []
    bucket: list[Point] = []
    gap = False

    def flush() -> None:
        nonlocal gap
        if gap and reduced:
            reduced.append(None)
        gap = False
        epoch = bucket[0].at
        offset = sum((p.at - epoch).total_seconds() for p in bucket) / len(bucket)
        mean = sum(p.value for p in bucket) / len(bucket)
        reduced.append(Point(epoch + dt.timedelta(seconds=offset), mean))
        bucket.clear()

    for point in points:
        if point is None:
            gap = True
            continue
        bucket.append(point)
        if len(bucket) == size:
            flush()
    if bucket:
        flush()
    return reduced


# --- layout -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureMark:
    x: float
    at: dt.datetime
    outcome: PollOutcome


@dataclass(frozen=True, slots=True)
class ChartView:
    key: str
    title: str
    width: int
    height: int
    lines: list[str]
    """SVG `points` strings, one per unbroken run; a lone point is a tiny
    segment drawn as a dot by its round cap."""
    failures: list[FailureMark]
    latest: str | None
    minimum: str | None
    maximum: str | None

    @property
    def empty(self) -> bool:
        return self.latest is None

    @property
    def foot(self) -> int:
        """Where failed-poll marks start: they rise from the bottom edge."""
        return self.height - TICK


def failures(rows: Sequence[SeriesRow]) -> list[tuple[dt.datetime, PollOutcome]]:
    return [(row.started_at, row.outcome) for row in rows if not has_status(row)]


def layout(
    spec: ChartSpec,
    raw: Sequence[Point | None],
    *,
    start: dt.datetime,
    end: dt.datetime,
    failed: Sequence[tuple[dt.datetime, PollOutcome]] = (),
    width: int = WIDTH,
    height: int = HEIGHT,
    max_points: int = MAX_CHART_POINTS,
) -> ChartView:
    """Scale one series into the viewBox; text stats come from `raw`."""
    values = [point.value for point in raw if point is not None]
    span = max((end - start).total_seconds(), 1.0)

    def x(at: dt.datetime) -> float:
        return round(min(max((at - start).total_seconds() / span, 0.0), 1.0) * width, 1)

    marks = [FailureMark(x=x(at), at=at, outcome=outcome) for at, outcome in failed]
    if not values:
        return ChartView(spec.key, spec.title, width, height, [], marks, None, None, None)
    low, high = min(values), max(values)
    if high == low:
        low, high = low - 1, high + 1
    plot = height - 2 * PAD - TICK

    def y(value: float) -> float:
        return round(PAD + (high - value) / (high - low) * plot, 1)

    lines: list[str] = []
    run: list[tuple[float, float]] = []
    for point in [*reduce(raw, max_points), None]:
        if point is not None:
            run.append((x(point.at), y(point.value)))
            continue
        if len(run) == 1:
            run.append((run[0][0] + 0.01, run[0][1]))
        if run:
            lines.append(" ".join(f"{px},{py}" for px, py in run))
        run = []
    last = next(point for point in reversed(raw) if point is not None)
    return ChartView(
        spec.key,
        spec.title,
        width,
        height,
        lines,
        marks,
        latest=spec.render(last.value),
        minimum=spec.render(min(values)),
        maximum=spec.render(max(values)),
    )


def charts(rows: Sequence[SeriesRow], *, start: dt.datetime, end: dt.datetime) -> list[ChartView]:
    """Every chart for one repeater over [start, end]."""
    derived = derive(rows)
    failed = failures(rows)
    return [layout(spec, derived[spec.key], start=start, end=end, failed=failed) for spec in SPECS]
