"""The startup airtime cross-check (milestone 3, design D2).

The board answers `GetAirtime` with its own estimate. Comparing it against ours
is the only independent check we get on the number the duty-cycle ceiling is
enforced against, and the point of these tests is that a disagreement is loud
and changes nothing: we keep using the computed value either way.
"""

from __future__ import annotations

import pytest

from sighop.net.airtime import (
    CHECK_LENGTHS,
    NoRadioReadback,
    compare,
    cross_check_airtime,
    require_params,
    time_on_air_ms,
)
from sighop.radio.modem import (
    ERROR_UNKNOWN_CMD,
    EU868_NARROW,
    SUB_GET_AIRTIME,
    RequestOk,
    RequestRejected,
    RequestResult,
    RequestTimedOut,
)


class FakeRequester:
    """Answers `GetAirtime` from a per-length table of milliseconds."""

    def __init__(
        self,
        milliseconds: dict[int, int] | None = None,
        *,
        error_code: int | None = None,
        timeout: bool = False,
    ) -> None:
        self.milliseconds = dict(milliseconds or {})
        self.error_code = error_code
        self.timeout = timeout
        self.requests: list[tuple[int, bytes]] = []

    async def request(
        self, sub_command: int, data: bytes = b"", *, timeout: float | None = None
    ) -> RequestResult:
        self.requests.append((sub_command, data))
        if self.error_code is not None:
            return RequestRejected(sub_command=sub_command, error_code=self.error_code)
        if self.timeout:
            return RequestTimedOut(sub_command=sub_command)
        payload_len = data[0]
        return RequestOk(
            sub_command=sub_command,
            response_code=sub_command | 0x80,
            data=self.milliseconds[payload_len].to_bytes(4, "little"),
        )


class RecordingLogger:
    """Captures events at the two levels the wide-event contract allows."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))


def _truthful_board() -> dict[int, int]:
    """What a correct board answers: our figure, truncated to whole ms."""
    return {length: int(time_on_air_ms(length, EU868_NARROW)) for length in CHECK_LENGTHS}


async def test_an_agreeing_board_is_recorded_and_changes_nothing() -> None:
    requester = FakeRequester(_truthful_board())
    logger = RecordingLogger()

    result = await cross_check_airtime(requester, EU868_NARROW, logger=logger)

    assert result.available
    assert result.disagreements == ()
    assert [(sub, data[0]) for sub, data in requester.requests] == [
        (SUB_GET_AIRTIME, length) for length in CHECK_LENGTHS
    ]


async def test_millisecond_truncation_alone_is_not_a_disagreement() -> None:
    """The board divides microseconds by 1000, so it is always up to 1 ms low."""
    board = {length: int(time_on_air_ms(length, EU868_NARROW)) for length in CHECK_LENGTHS}
    result = compare(EU868_NARROW, dict(board))

    assert result.disagreements == ()


async def test_a_disagreeing_board_is_reported_at_error_level() -> None:
    board = _truthful_board()
    board[64] = int(board[64] * 0.5)  # a board running a preset we did not expect
    logger = RecordingLogger()

    result = await cross_check_airtime(FakeRequester(board), EU868_NARROW, logger=logger)

    assert [check.payload_len for check in result.disagreements] == [64]
    levels = [level for level, _event, _fields in logger.events]
    assert "error" in levels
    (_level, event, fields) = next(entry for entry in logger.events if entry[0] == "error")
    assert event == "airtime_cross_check_mismatch"
    # Both figures present: a mismatch nobody can act on is not worth logging.
    mismatch = next(c for c in fields["checks"] if c["payload_len"] == 64)  # type: ignore[union-attr,index]
    assert mismatch["reported_ms"] == board[64]
    assert mismatch["computed_ms"] == pytest.approx(time_on_air_ms(64, EU868_NARROW), abs=0.01)


async def test_a_disagreement_does_not_change_the_value_used() -> None:
    board = _truthful_board()
    board[255] = 1  # absurd, and still not authoritative
    result = compare(EU868_NARROW, dict(board))

    check = next(c for c in result.checks if c.payload_len == 255)
    assert check.disagrees
    assert check.computed_ms == pytest.approx(time_on_air_ms(255, EU868_NARROW))


async def test_a_board_without_the_sub_command_leaves_the_computation_unchecked() -> None:
    logger = RecordingLogger()
    result = await cross_check_airtime(
        FakeRequester(error_code=ERROR_UNKNOWN_CMD), EU868_NARROW, logger=logger
    )

    assert not result.available
    assert result.disagreements == ()
    assert all(check.absence_reason == "unsupported" for check in result.checks)
    assert [level for level, _e, _f in logger.events] == ["info"]


async def test_a_silent_board_records_a_timeout_rather_than_failing() -> None:
    result = await cross_check_airtime(FakeRequester(timeout=True), EU868_NARROW)

    assert not result.available
    assert all(check.absence_reason == "timeout" for check in result.checks)


def test_transmission_is_refused_without_a_radio_readback() -> None:
    with pytest.raises(NoRadioReadback):
        require_params(None)

    assert require_params(EU868_NARROW) == EU868_NARROW
