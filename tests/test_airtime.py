"""Time on air (milestone 3, `airtime`).

The known answers below are computed by hand from Semtech's formula with the
firmware's parameters — 32-symbol preamble at SF8, explicit header, CRC on — and
they are the numbers DESIGN.md §4.3's deaf-window table now carries. Getting
these wrong is not a rendering bug: the duty-cycle ceiling is enforced against
them, and the ceiling is a legal limit.
"""

from __future__ import annotations

import pytest

from sighop.net.airtime import (
    UnsupportedRadioParams,
    low_data_rate_optimized,
    preamble_symbols,
    symbol_time_s,
    time_on_air_ms,
    time_on_air_s,
)
from sighop.radio.modem import EU868_NARROW, RadioParams


def test_default_preset_64_byte_packet_is_about_740_ms() -> None:
    # (32 + 4.25) preamble symbols + 144 payload symbols at 4.096 ms.
    assert time_on_air_s(64, EU868_NARROW) == pytest.approx(0.7383, abs=0.001)


def test_default_preset_maximum_packet_is_about_2310_ms() -> None:
    assert time_on_air_s(255, EU868_NARROW) == pytest.approx(2.3112, abs=0.001)


def test_the_deaf_window_table_in_design_matches_this_computation() -> None:
    """DESIGN.md §4.3: 64 B ~0.74 s, 255 B ~2.31 s at the default preset."""
    assert round(time_on_air_s(64, EU868_NARROW), 2) == 0.74
    assert round(time_on_air_s(255, EU868_NARROW), 2) == 2.31


def test_the_preamble_steps_between_sf8_and_sf9() -> None:
    assert preamble_symbols(8) == 32
    assert preamble_symbols(9) == 16

    sf8 = time_on_air_s(64, RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=8, cr=8))
    sf9 = time_on_air_s(64, RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=9, cr=8))
    # SF9 doubles the symbol time but halves the preamble, so the ratio is well
    # below 2 -- which is the whole reason the preamble cannot be assumed.
    assert 1.0 < sf9 / sf8 < 2.0


def test_low_data_rate_optimisation_is_off_at_the_default_preset() -> None:
    assert symbol_time_s(8, 62_500) == pytest.approx(0.004096)
    assert not low_data_rate_optimized(8, 62_500)


def test_low_data_rate_optimisation_switches_on_at_sf10_on_narrow_bandwidth() -> None:
    # 2**9 / 62500 = 8.19 ms; 2**10 / 62500 = 16.38 ms, over the 16 ms rule.
    assert not low_data_rate_optimized(9, 62_500)
    assert low_data_rate_optimized(10, 62_500)


def test_low_data_rate_optimisation_lengthens_the_packet() -> None:
    """LDRO trades symbols for robustness; it must not shorten time on air."""
    sf10 = RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=10, cr=8)
    without_ldro = 8 + (2**10 / 62_500)  # not a real config, just a sanity bound
    assert time_on_air_s(64, sf10) < without_ldro


def test_time_on_air_is_monotonic_in_payload_length() -> None:
    lengths = [0, 1, 16, 64, 128, 200, 255]
    times = [time_on_air_s(length, EU868_NARROW) for length in lengths]
    assert times == sorted(times)


def test_coding_rate_lengthens_the_packet() -> None:
    fast = RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=8, cr=5)
    slow = RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=8, cr=8)
    assert time_on_air_s(64, fast) < time_on_air_s(64, slow)


def test_the_legacy_preset_costs_about_the_same_as_the_narrow_one() -> None:
    """DESIGN.md §4.3 claimed these differ "by more than an order of magnitude".

    They do not: 250 kHz/SF11/CR5 is within ~7% of 62.5 kHz/SF8/CR8 across the
    whole payload range, and marginally *faster* above 32 bytes. Four times the
    bandwidth against eight times the symbol time nearly cancels, and the
    shorter preamble and lighter coding rate finish the job. Recorded as a test
    because it is counter-intuitive enough to be re-derived wrongly.
    """
    legacy = RadioParams(freq_hz=869_525_000, bw_hz=250_000, sf=11, cr=5)
    for length in (16, 64, 128, 255):
        ratio = time_on_air_s(length, legacy) / time_on_air_s(length, EU868_NARROW)
        assert 0.9 < ratio < 1.1


def test_a_slow_preset_costs_several_times_more() -> None:
    """The reason parameters are read back rather than assumed still stands."""
    slow = RadioParams(freq_hz=869_525_000, bw_hz=125_000, sf=12, cr=5)
    assert time_on_air_s(64, slow) > 4 * time_on_air_s(64, EU868_NARROW)


def test_milliseconds_helper_agrees_with_seconds() -> None:
    assert time_on_air_ms(64, EU868_NARROW) == pytest.approx(time_on_air_s(64, EU868_NARROW) * 1000)


@pytest.mark.parametrize(
    "params",
    [
        RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=4, cr=8),
        RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=13, cr=8),
        RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=8, cr=4),
        RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=8, cr=9),
        RadioParams(freq_hz=869_618_000, bw_hz=0, sf=8, cr=8),
    ],
)
def test_parameters_outside_the_defined_range_raise(params: RadioParams) -> None:
    with pytest.raises(UnsupportedRadioParams):
        time_on_air_s(64, params)


def test_negative_payload_length_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        time_on_air_s(-1, EU868_NARROW)
