"""Battery capacity estimate from voltage (repeater-metrics-history 2.1)."""

from __future__ import annotations

import pytest

from sighop.web.battery import estimate, is_low


def test_a_full_cell_is_above_90_percent_and_not_low() -> None:
    percent = estimate(4150)
    assert percent is not None and percent > 90
    assert not is_low(4150)


def test_the_anchor_points() -> None:
    assert estimate(4200) == 100
    assert estimate(4400) == 100
    assert estimate(3750) == 50
    assert estimate(3300) == 0
    assert estimate(2500) == 0


def test_a_half_drained_cell_is_low() -> None:
    percent = estimate(3600)
    assert percent is not None and percent < 50
    assert is_low(3600)
    assert not is_low(3750), "50% is not below 50%"


def test_values_between_points_are_interpolated() -> None:
    assert estimate(3650) == round((25 + 42) / 2)
    assert estimate(3250) == 0


@pytest.mark.parametrize("mv", [0, 2400, 2499, 4401, 5100])
def test_no_battery_sensed_gives_no_estimate_and_is_never_low(mv: int) -> None:
    assert estimate(mv) is None
    assert not is_low(mv)


def test_not_reported() -> None:
    assert estimate(None) is None
    assert not is_low(None)
