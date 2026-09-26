"""Battery capacity estimated from a repeater's reported voltage (repeater-metrics-history D4).

A single-cell lithium-ion discharge curve, interpolated between its points. It
is rough by nature — the reading is taken under load and cells age — so the
spec fixes only the anchors (4.20 V full, 3.30 V empty, about 3.75 V half) and
this table may be tuned without a spec change.

A reading that does not look like one Li-ion cell (below 2.5 V, including the
0 V a mains-powered board reports, or above 4.4 V) means no battery sensed: no
estimate, and never low.
"""

from __future__ import annotations

from itertools import pairwise

_CURVE_MV = (
    (3300, 0),
    (3400, 5),
    (3500, 12),
    (3600, 25),
    (3700, 42),
    (3750, 50),
    (3800, 56),
    (3900, 68),
    (4000, 80),
    (4100, 90),
    (4200, 100),
)
NO_BATTERY_BELOW_MV = 2500
NO_BATTERY_ABOVE_MV = 4400
LOW_BELOW_PERCENT = 50


def estimate(mv: int | None) -> int | None:
    """Whole percent 0 to 100, or None when not reported or no battery sensed."""
    if mv is None or mv < NO_BATTERY_BELOW_MV or mv > NO_BATTERY_ABOVE_MV:
        return None
    if mv <= _CURVE_MV[0][0]:
        return 0
    for (low_mv, low_pct), (high_mv, high_pct) in pairwise(_CURVE_MV):
        if mv <= high_mv:
            return round(low_pct + (high_pct - low_pct) * (mv - low_mv) / (high_mv - low_mv))
    return 100


def is_low(mv: int | None) -> bool:
    """An estimate exists and is below 50%."""
    percent = estimate(mv)
    return percent is not None and percent < LOW_BELOW_PERCENT
