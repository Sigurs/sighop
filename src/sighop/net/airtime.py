"""LoRa time on air, computed from the radio parameters the board reported.

This is the number the duty-cycle ceiling rests on (DESIGN.md §4.3), so it is
computed rather than assumed, and from the *live* `GetRadio` readback rather
than from configuration: a board that silently reverted to its build defaults
reports nothing about it, and presets differ by several times per packet
(SF12/125 kHz is over four times the EU narrow preset).

Note the EU narrow and the legacy 250 kHz/SF11 presets are *not* among the pairs
that differ wildly — DESIGN.md §4.3 says they differ "by more than an order of
magnitude" and they do not; they are within ~7%, legacy marginally the faster of
the two above 32 bytes. `tests/test_airtime.py` keeps that recorded, because it
is exactly the kind of thing that gets re-derived from intuition.

Three of the formula's inputs are firmware facts, not textbook defaults, and
getting them from the source is what makes this match the radio (design D1):

* **The preamble is spreading-factor-dependent.** `preambleLengthForSF` in
  `related-repos/MeshCore/src/helpers/radiolib/RadioLibWrappers.h:56` returns
  32 symbols at SF <= 8 and 16 above, and `SetRadio` re-applies it. The
  RadioLib default of 8 is what DESIGN.md §4.3's original deaf-window table
  assumed, which is why it understated every packet by ~15% (design D3).
* **Explicit header, CRC on** — `CustomSX1262.h:69` calls `setCRC(1)`, and
  RadioLib's header type defaults to explicit.
* **Low data-rate optimisation follows RadioLib's automatic rule**: on when the
  symbol time reaches 16 ms. At BW 62.5 kHz that is SF >= 10, so it is *off* at
  the default preset.

The formula itself is Semtech's, as used by RadioLib's `getTimeOnAir()`; the
firmware's own `calcMaxPacketMillis` (`RadioLibWrappers.cpp:241`) folds the same
constants together differently for its CSMA timers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sighop.logging import Logger, get_logger
from sighop.radio.modem import RadioParams
from sighop.radio.probe import Absent, Requester, probe_airtime

PREAMBLE_SYMBOLS_LOW_SF = 32
"""Symbols at SF <= 8 (`preambleLengthForSF`)."""

PREAMBLE_SYMBOLS_HIGH_SF = 16
"""Symbols above SF 8."""

PREAMBLE_SF_THRESHOLD = 8

LDRO_SYMBOL_TIME_S = 0.016
"""RadioLib enables low data-rate optimisation at or above this symbol time."""

MIN_SF = 5
MAX_SF = 12

MIN_CR = 5
MAX_CR = 8
"""Coding rate as its denominator: 5..8 means 4/5..4/8, as `SetRadio` carries it."""

MAX_PACKET_BYTES = 255
"""`KISS_MAX_PACKET_SIZE` — the largest payload the modem will transmit."""


class UnsupportedRadioParams(ValueError):
    """Radio parameters outside the range this computation is defined for."""


class NoRadioReadback(RuntimeError):
    """No parameters were read back from the board, so airtime is unknown.

    Refusing to transmit is the only safe answer: falling back to configured
    values would compute a budget for a radio configuration the board may not
    be running, and the duty-cycle ceiling would then be enforced against
    fiction. Silence costs nothing; an unlawful transmission does.
    """


def require_params(params: RadioParams | None) -> RadioParams:
    """The observed parameters, or refuse. Every admission path goes through here."""
    if params is None:
        raise NoRadioReadback(
            "no GetRadio readback available; refusing to compute airtime from configured values"
        )
    return params


def preamble_symbols(sf: int) -> int:
    """The preamble the firmware configures for this spreading factor."""
    return PREAMBLE_SYMBOLS_LOW_SF if sf <= PREAMBLE_SF_THRESHOLD else PREAMBLE_SYMBOLS_HIGH_SF


def symbol_time_s(sf: int, bw_hz: int) -> float:
    """Duration of one LoRa symbol: 2**SF / bandwidth."""
    return float(2**sf) / bw_hz


def low_data_rate_optimized(sf: int, bw_hz: int) -> bool:
    """RadioLib's automatic rule: on once a symbol lasts 16 ms or longer."""
    return symbol_time_s(sf, bw_hz) >= LDRO_SYMBOL_TIME_S


def _validate(params: RadioParams) -> None:
    if not MIN_SF <= params.sf <= MAX_SF:
        raise UnsupportedRadioParams(f"spreading factor {params.sf} outside {MIN_SF}..{MAX_SF}")
    if not MIN_CR <= params.cr <= MAX_CR:
        raise UnsupportedRadioParams(
            f"coding rate {params.cr} outside {MIN_CR}..{MAX_CR} (4/5..4/8)"
        )
    if params.bw_hz <= 0:
        raise UnsupportedRadioParams(f"bandwidth {params.bw_hz} Hz is not positive")


def time_on_air_s(payload_len: int, params: RadioParams) -> float:
    """Seconds on air for a `payload_len`-byte packet under `params`.

    `params` must be the board's readback, never configured values — the caller
    is responsible for that distinction, and `tx` refuses to admit anything when
    no readback exists.
    """
    if payload_len < 0:
        raise ValueError(f"payload length {payload_len} is negative")
    _validate(params)

    sf = params.sf
    tsym = symbol_time_s(sf, params.bw_hz)

    # 4.25 symbols of sync word + start-of-frame delimiter, except at SF5/SF6
    # where Semtech's constant is 6.25 (the firmware carries the same pair as
    # `sfCoeff1_x4` 17/25 in quarter-symbols). MeshCore's presets do not reach
    # below SF7, so the low-SF branch is unexercised by any real configuration.
    sync_symbols = 6.25 if sf <= 6 else 4.25
    preamble_time = (preamble_symbols(sf) + sync_symbols) * tsym

    de = 2 if low_data_rate_optimized(sf, params.bw_hz) else 0
    # CRC on (+16), explicit header (no -20 term).
    numerator = 8 * payload_len - 4 * sf + 28 + 16
    denominator = 4 * (sf - de)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * params.cr, 0)

    return preamble_time + payload_symbols * tsym


def time_on_air_ms(payload_len: int, params: RadioParams) -> float:
    """Milliseconds on air — the unit the budget and the wide events use."""
    return time_on_air_s(payload_len, params) * 1000.0


# --- Cross-check against the board's own estimate (design D2) ----------------
#
# Everything above is a pure function of bytes and parameters. What follows asks
# the modem what *it* thinks, and compares. It lives here rather than in the
# probe because the comparison, not the query, is the interesting part.

CHECK_LENGTHS: tuple[int, ...] = (16, 64, 128, MAX_PACKET_BYTES)
"""The ladder probed at startup. Short and long, where a formula error shows."""

TOLERANCE_MS = 1.0
"""The board truncates to whole milliseconds, so 1 ms of disagreement is free."""

TOLERANCE_FRACTION = 0.02
"""Beyond the truncation, 2%. Both must be exceeded to count as a disagreement."""


@dataclass(frozen=True, slots=True)
class LengthCheck:
    """One rung of the ladder: what we computed against what the board said."""

    payload_len: int
    computed_ms: float
    reported_ms: int | None
    absence_reason: str | None = None

    @property
    def delta_ms(self) -> float | None:
        if self.reported_ms is None:
            return None
        return self.computed_ms - self.reported_ms

    @property
    def disagrees(self) -> bool:
        """Both tolerances must be exceeded — a millisecond alone proves nothing."""
        if self.reported_ms is None:
            return False
        delta = abs(self.computed_ms - self.reported_ms)
        if delta <= TOLERANCE_MS:
            return False
        return delta > TOLERANCE_FRACTION * self.computed_ms

    def as_json(self) -> dict[str, object]:
        return {
            "payload_len": self.payload_len,
            "computed_ms": round(self.computed_ms, 3),
            "reported_ms": self.reported_ms,
            "delta_ms": None if self.delta_ms is None else round(self.delta_ms, 3),
            "absence_reason": self.absence_reason,
            "disagrees": self.disagrees,
        }


@dataclass(frozen=True, slots=True)
class CrossCheck:
    """The whole ladder. `available` is False when the board answers nothing."""

    params: RadioParams
    checks: tuple[LengthCheck, ...]

    @property
    def available(self) -> bool:
        return any(check.reported_ms is not None for check in self.checks)

    @property
    def disagreements(self) -> tuple[LengthCheck, ...]:
        return tuple(check for check in self.checks if check.disagrees)

    def as_json(self) -> dict[str, object]:
        return {
            "params": self.params.as_json(),
            "available": self.available,
            "disagreement_count": len(self.disagreements),
            "checks": [check.as_json() for check in self.checks],
        }


def compare(params: RadioParams, reported: dict[int, int | str]) -> CrossCheck:
    """Compare our computation against reported values, keyed by payload length.

    A value is either the board's milliseconds (`int`) or the reason it is
    absent (`str`). Pure — the querying happens in `cross_check_airtime`.
    """
    checks = []
    for payload_len, value in reported.items():
        computed = time_on_air_ms(payload_len, params)
        if isinstance(value, int):
            checks.append(LengthCheck(payload_len, computed, value))
        else:
            checks.append(LengthCheck(payload_len, computed, None, absence_reason=value))
    return CrossCheck(params=params, checks=tuple(checks))


async def cross_check_airtime(
    requester: Requester,
    params: RadioParams,
    *,
    lengths: Sequence[int] = CHECK_LENGTHS,
    logger: Logger | None = None,
) -> CrossCheck:
    """Ask the board its estimate for each length and compare against ours.

    Never raises and never changes the value the scheduler uses: a disagreement
    is reported at error level, naming both figures, and the run proceeds on the
    computed number (design D2). A board that does not implement `GetAirtime`
    leaves the computation unchecked, which is recorded rather than treated as a
    failure.
    """
    log = logger or get_logger(component="airtime")
    reported: dict[int, int | str] = {}
    for payload_len in lengths:
        probed = await probe_airtime(requester, payload_len)
        if isinstance(probed, Absent):
            reported[payload_len] = str(probed.reason)
        else:
            reported[payload_len] = probed

    result = compare(params, reported)
    if result.disagreements:
        # Error level and its own event, for the same reason the radio readback
        # mismatch gets one: the duty-cycle ceiling is enforced against this
        # number, and nothing else in the system would notice it drifting.
        log.error("airtime_cross_check_mismatch", **result.as_json())
    else:
        log.info("airtime_cross_check", **result.as_json())
    return result
