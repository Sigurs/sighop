"""The startup capability probe (milestone 2, design D4/D5).

Driven through a fake requester rather than a modem: the probe's contract is
"a request in, a value or a structured absence out", and DESIGN.md §4.1's rule
that no board is assumed to support anything is exactly what these scenarios
check.
"""

from __future__ import annotations

import asyncio

from sighop.radio.modem import (
    ERROR_NO_CALLBACK,
    ERROR_UNKNOWN_CMD,
    EU868_NARROW,
    SENSOR_PERMISSIONS_ALL,
    SUB_GET_BATTERY,
    SUB_GET_DEVICE_NAME,
    SUB_GET_MCU_TEMP,
    SUB_GET_RADIO,
    SUB_GET_SENSORS,
    SUB_GET_TX_POWER,
    SUB_GET_VERSION,
    RadioParams,
    RequestOk,
    RequestRejected,
    RequestResult,
    RequestTimedOut,
)
from sighop.radio.probe import (
    AbsenceReason,
    Absent,
    FirmwareVersion,
    ProbeResult,
    run_probe,
)

SENSORS_LPP = bytes.fromhex("01670115026864")  # CayenneLPP, never parsed here

FULL_ANSWERS: dict[int, bytes] = {
    SUB_GET_DEVICE_NAME: b"Heltec V3",
    SUB_GET_RADIO: EU868_NARROW.to_bytes(),
    SUB_GET_TX_POWER: bytes([0xEC]),  # -20 dBm, signed
    SUB_GET_VERSION: bytes((0x01, 0x00)),
    SUB_GET_BATTERY: (4021).to_bytes(2, "little"),
    SUB_GET_MCU_TEMP: (-53).to_bytes(2, "little", signed=True),
    SUB_GET_SENSORS: SENSORS_LPP,
}


class FakeRequester:
    """Answers from a table; anything absent from it is a timeout."""

    def __init__(
        self,
        answers: dict[int, bytes] | None = None,
        rejections: dict[int, int] | None = None,
    ) -> None:
        self.answers = dict(answers or {})
        self.rejections = dict(rejections or {})
        self.requests: list[tuple[int, bytes]] = []

    async def request(
        self, sub_command: int, data: bytes = b"", *, timeout: float | None = None
    ) -> RequestResult:
        self.requests.append((sub_command, data))
        if sub_command in self.rejections:
            return RequestRejected(
                sub_command=sub_command, error_code=self.rejections[sub_command]
            )
        if sub_command in self.answers:
            return RequestOk(
                sub_command=sub_command,
                response_code=sub_command | 0x80,
                data=self.answers[sub_command],
            )
        return RequestTimedOut(sub_command=sub_command)


async def probe(requester: FakeRequester, configured: RadioParams = EU868_NARROW):
    return await run_probe(requester, configured)


async def test_a_board_that_answers_everything_is_fully_recorded():
    requester = FakeRequester(FULL_ANSWERS)
    result = await probe(requester)

    assert result.device_name == "Heltec V3"
    assert result.radio == EU868_NARROW
    assert result.tx_power_dbm == -20
    assert result.firmware_version == FirmwareVersion(version=1, reserved=0)
    assert result.battery_mv == 4021
    assert result.mcu_temp_tenths_c == -53
    assert result.sensors_raw == SENSORS_LPP
    assert result.radio_matches_configured is True


async def test_sensors_are_requested_with_all_permissions_and_left_uninterpreted():
    requester = FakeRequester(FULL_ANSWERS)
    result = await probe(requester)

    assert (SUB_GET_SENSORS, bytes((SENSOR_PERMISSIONS_ALL,))) in requester.requests
    # Raw bytes, not named sensor values: the shape follows build flags (§4.1).
    assert isinstance(result.sensors_raw, bytes)


async def test_an_unsupported_sub_command_is_recorded_and_probing_continues():
    for sub_command, error_code in (
        (SUB_GET_MCU_TEMP, ERROR_NO_CALLBACK),
        (SUB_GET_SENSORS, ERROR_UNKNOWN_CMD),
        (SUB_GET_BATTERY, ERROR_UNKNOWN_CMD),
        (SUB_GET_DEVICE_NAME, ERROR_UNKNOWN_CMD),
    ):
        answers = {k: v for k, v in FULL_ANSWERS.items() if k != sub_command}
        requester = FakeRequester(answers, rejections={sub_command: error_code})
        result = await probe(requester)

        absent = _field_for(result, sub_command)
        assert isinstance(absent, Absent), sub_command
        assert absent.reason is AbsenceReason.UNSUPPORTED
        assert absent.error_code == error_code
        # every other sub-command was still asked, and still answered
        assert len(requester.requests) == len(FULL_ANSWERS)
        assert result.firmware_version == FirmwareVersion(version=1, reserved=0)


async def test_a_timed_out_sub_command_is_recorded_and_probing_continues():
    answers = {k: v for k, v in FULL_ANSWERS.items() if k != SUB_GET_TX_POWER}
    requester = FakeRequester(answers)
    result = await probe(requester)

    assert result.tx_power_dbm == Absent(reason=AbsenceReason.TIMEOUT)
    assert result.device_name == "Heltec V3"
    assert len(requester.requests) == len(FULL_ANSWERS)


async def test_a_board_that_answers_nothing_still_produces_a_result():
    requester = FakeRequester({})
    result = await probe(requester)

    assert isinstance(result, ProbeResult)
    for sub_command in FULL_ANSWERS:
        absent = _field_for(result, sub_command)
        assert absent == Absent(reason=AbsenceReason.TIMEOUT)
    assert result.radio_matches_configured is None
    assert result.observed_radio is None
    # configured parameters are never substituted for an unanswered readback
    assert result.configured_radio == EU868_NARROW


async def test_radio_readback_mismatch_is_visible_in_the_result():
    wrong = RadioParams(freq_hz=869_525_000, bw_hz=250_000, sf=11, cr=5)
    requester = FakeRequester({**FULL_ANSWERS, SUB_GET_RADIO: wrong.to_bytes()})
    result = await probe(requester, EU868_NARROW)

    assert result.radio == wrong
    assert result.observed_radio == wrong
    assert result.configured_radio == EU868_NARROW
    assert result.radio_matches_configured is False


async def test_a_non_utf8_device_name_is_an_absence_not_a_garbled_name():
    requester = FakeRequester({**FULL_ANSWERS, SUB_GET_DEVICE_NAME: b"Helt\xffc"})
    result = await probe(requester)

    assert isinstance(result.device_name, Absent)
    assert result.device_name.reason is AbsenceReason.UNDECODABLE
    assert "utf-8" in result.device_name.detail.lower()


async def test_a_short_response_is_an_absence_not_a_crash():
    requester = FakeRequester({**FULL_ANSWERS, SUB_GET_RADIO: b"\x01\x02"})
    result = await probe(requester)

    assert isinstance(result.radio, Absent)
    assert result.radio.reason is AbsenceReason.UNDECODABLE
    assert result.radio_matches_configured is None


async def test_probe_json_records_absences_with_their_reason():
    requester = FakeRequester(
        {k: v for k, v in FULL_ANSWERS.items() if k != SUB_GET_BATTERY},
        rejections={SUB_GET_SENSORS: ERROR_UNKNOWN_CMD},
    )
    as_json = (await probe(requester)).as_json()

    assert as_json["device_name"] == {
        "value": "Heltec V3",
        "reason": None,
        "error_code": None,
        "detail": "",
    }
    assert as_json["battery_mv"]["value"] is None
    assert as_json["battery_mv"]["reason"] == "timeout"
    assert as_json["sensors_raw_hex"]["value"] is None
    assert as_json["sensors_raw_hex"]["reason"] == "unsupported"
    assert as_json["sensors_raw_hex"]["error_code"] == ERROR_UNKNOWN_CMD
    assert as_json["radio"]["value"] == EU868_NARROW.as_json()
    assert as_json["configured_radio"] == EU868_NARROW.as_json()


async def test_probe_never_raises_when_the_link_disappears_mid_probe():
    class DeadRequester(FakeRequester):
        async def request(self, sub_command, data=b"", *, timeout=None):
            await asyncio.sleep(0)
            return RequestTimedOut(sub_command=sub_command)

    result = await probe(DeadRequester())
    assert isinstance(result, ProbeResult)


def _field_for(result: ProbeResult, sub_command: int):
    return {
        SUB_GET_DEVICE_NAME: result.device_name,
        SUB_GET_RADIO: result.radio,
        SUB_GET_TX_POWER: result.tx_power_dbm,
        SUB_GET_VERSION: result.firmware_version,
        SUB_GET_BATTERY: result.battery_mv,
        SUB_GET_MCU_TEMP: result.mcu_temp_tenths_c,
        SUB_GET_SENSORS: result.sensors_raw,
    }[sub_command]
