"""Startup capability probe: ask the board what it is, and record the answers.

DESIGN.md §4.1 requires every telemetry sub-command to be treated as optional
and probe-detected — the board is observable through `GetDeviceName`,
`GetSensors`, `SetTxPower` semantics and RX sensitivity, none of which sighop
may assume. So the probe asks, records what came back, and records an absence
where nothing did (design D4). It never raises and never blocks the link from
becoming ready: a board that answers nothing still receives frames normally.

The `GetRadio` readback is the one answer that is compared rather than merely
recorded (design D5). A preset that is subtly wrong receives nothing at all,
and silence is not a symptom anyone can act on.

Protocol reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sighop.logging import Logger, get_logger, wide_event
from sighop.radio.modem import (
    ERROR_NO_CALLBACK,
    ERROR_UNKNOWN_CMD,
    SENSOR_PERMISSIONS_ALL,
    SUB_GET_AIRTIME,
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


class AbsenceReason(StrEnum):
    """Why a probed value is missing — a stable value for logs and headers."""

    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    UNDECODABLE = "undecodable"


@dataclass(frozen=True, slots=True)
class Absent:
    """A probed value the board did not supply, with the reason it did not.

    An absence is a value, not an error (design D4). `error_code` carries the
    modem's own code for `UNSUPPORTED` — `UnknownCmd` (0x05) and `NoCallback`
    (0x03) mean different things about the board and both are worth keeping.
    """

    reason: AbsenceReason
    error_code: int | None = None
    detail: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "value": None,
            "reason": str(self.reason),
            "error_code": self.error_code,
            "detail": self.detail,
        }


type Probed[T] = T | Absent


@dataclass(frozen=True, slots=True)
class FirmwareVersion:
    """The `Version` (0x91) response: a version byte and a reserved byte."""

    version: int
    reserved: int

    def as_json(self) -> dict[str, int]:
        return {"version": self.version, "reserved": self.reserved}


class Requester(Protocol):
    """The slice of `Modem` the probe uses. Nothing here reads frames."""

    async def request(
        self, sub_command: int, data: bytes = b"", *, timeout: float | None = None
    ) -> RequestResult: ...


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the board said about itself, absences included.

    `configured_radio` is what we applied; `radio` is what the board reported
    back. They are kept apart so a disagreement is visible rather than
    averaged away (design D5/D6).
    """

    configured_radio: RadioParams
    device_name: Probed[str]
    radio: Probed[RadioParams]
    tx_power_dbm: Probed[int]
    firmware_version: Probed[FirmwareVersion]
    battery_mv: Probed[int]
    mcu_temp_tenths_c: Probed[int]
    sensors_raw: Probed[bytes]

    @property
    def radio_matches_configured(self) -> bool | None:
        """True/False against the applied parameters, None if unanswered."""
        if isinstance(self.radio, Absent):
            return None
        return self.radio == self.configured_radio

    @property
    def observed_radio(self) -> RadioParams | None:
        """The read-back parameters — never the configured ones as a stand-in."""
        return None if isinstance(self.radio, Absent) else self.radio

    def as_json(self) -> dict[str, object]:
        """The probe as JSON-able values, absences carrying their reason."""
        return {
            "configured_radio": self.configured_radio.as_json(),
            "radio_matches_configured": self.radio_matches_configured,
            "device_name": _field(self.device_name),
            "radio": _field(self.radio, lambda value: value.as_json()),
            "tx_power_dbm": _field(self.tx_power_dbm),
            "firmware_version": _field(self.firmware_version, lambda v: v.as_json()),
            "battery_mv": _field(self.battery_mv),
            "mcu_temp_tenths_c": _field(self.mcu_temp_tenths_c),
            "sensors_raw_hex": _field(self.sensors_raw, lambda value: value.hex()),
        }


def _field[T](
    probed: Probed[T], encode: Callable[[T], object] = lambda value: value
) -> dict[str, object]:
    if isinstance(probed, Absent):
        return probed.as_json()
    return {"value": encode(probed), "reason": None, "error_code": None, "detail": ""}


def _absence(result: RequestRejected | RequestTimedOut) -> Absent:
    if isinstance(result, RequestTimedOut):
        return Absent(reason=AbsenceReason.TIMEOUT)
    detail = ""
    if result.error_code == ERROR_UNKNOWN_CMD:
        detail = "UnknownCmd: the firmware does not implement this sub-command"
    elif result.error_code == ERROR_NO_CALLBACK:
        detail = "NoCallback: the board does not provide this feature"
    return Absent(reason=AbsenceReason.UNSUPPORTED, error_code=result.error_code, detail=detail)


async def _probe[T](
    requester: Requester,
    sub_command: int,
    parse: Callable[[bytes], T],
    data: bytes = b"",
) -> Probed[T]:
    result = await requester.request(sub_command, data)
    if not isinstance(result, RequestOk):
        return _absence(result)
    try:
        return parse(result.data)
    except ValueError as exc:
        return Absent(
            reason=AbsenceReason.UNDECODABLE,
            detail=f"{exc} (response bytes: {result.data.hex()})",
        )


def _parse_device_name(data: bytes) -> str:
    """UTF-8, no terminator. Non-UTF-8 is an absence, not a garbled name."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"device name is not valid UTF-8: {exc}") from exc


def _parse_tx_power(data: bytes) -> int:
    if not data:
        raise ValueError("TxPower response carries no power byte")
    return int.from_bytes(data[:1], "little", signed=True)


def _parse_version(data: bytes) -> FirmwareVersion:
    if len(data) < 2:
        raise ValueError(f"Version response needs 2 bytes, got {len(data)}")
    return FirmwareVersion(version=data[0], reserved=data[1])


def _parse_battery(data: bytes) -> int:
    if len(data) < 2:
        raise ValueError(f"Battery response needs 2 bytes, got {len(data)}")
    return int.from_bytes(data[:2], "little")


def _parse_mcu_temp(data: bytes) -> int:
    if len(data) < 2:
        raise ValueError(f"MCUTemp response needs 2 bytes, got {len(data)}")
    return int.from_bytes(data[:2], "little", signed=True)


def _parse_sensors(data: bytes) -> bytes:
    """CayenneLPP, kept raw: its shape follows build flags (§4.1, design D4)."""
    return data


def _parse_airtime(data: bytes) -> int:
    """`Airtime` (0x8F): uint32 milliseconds, little-endian."""
    if len(data) < 4:
        raise ValueError(f"Airtime response needs 4 bytes, got {len(data)}")
    return int.from_bytes(data[:4], "little")


async def probe_airtime(requester: Requester, payload_len: int) -> Probed[int]:
    """Ask the board its own airtime estimate, in milliseconds, for a length.

    The answer is the firmware's `getEstAirtimeFor` — RadioLib's `getTimeOnAir`
    divided by 1000, so truncated to whole milliseconds. It exists here to
    cross-check our own computation (design D2), never to be used in its place:
    it costs a round-trip, it is a millisecond coarse, and it is unavailable in
    replay, where most of the scheduler's tests run.
    """
    if not 0 <= payload_len <= 0xFF:
        raise ValueError(f"payload length {payload_len} does not fit the 1-byte request")
    return await _probe(requester, SUB_GET_AIRTIME, _parse_airtime, bytes((payload_len,)))


async def run_probe(
    requester: Requester,
    configured: RadioParams,
    *,
    logger: Logger | None = None,
) -> ProbeResult:
    """Ask the board about itself. Never raises; every failure is a value."""
    log = logger or get_logger(component="modem")
    with wide_event(log, "modem_probe") as fields:
        result = ProbeResult(
            configured_radio=configured,
            device_name=await _probe(requester, SUB_GET_DEVICE_NAME, _parse_device_name),
            radio=await _probe(requester, SUB_GET_RADIO, RadioParams.from_bytes),
            tx_power_dbm=await _probe(requester, SUB_GET_TX_POWER, _parse_tx_power),
            firmware_version=await _probe(requester, SUB_GET_VERSION, _parse_version),
            battery_mv=await _probe(requester, SUB_GET_BATTERY, _parse_battery),
            mcu_temp_tenths_c=await _probe(requester, SUB_GET_MCU_TEMP, _parse_mcu_temp),
            sensors_raw=await _probe(
                requester,
                SUB_GET_SENSORS,
                _parse_sensors,
                bytes((SENSOR_PERMISSIONS_ALL,)),
            ),
        )
        fields.update(result.as_json())
        if result.radio_matches_configured is False:
            fields["outcome"] = "radio_readback_mismatch"

    if result.radio_matches_configured is False:
        # Error level and its own event: the failure this catches is a modem
        # that receives nothing, whose only other symptom is silence (D5).
        log.error(
            "radio_readback_mismatch",
            applied=configured.as_json(),
            read_back=result.observed_radio.as_json() if result.observed_radio else None,
        )
    return result
