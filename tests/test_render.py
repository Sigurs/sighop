"""Monitor rendering, by string comparison (milestone 2, design D8).

Pure functions in, text out — no device, no file, no event loop. The test that
carries weight is the failed-advert one: it asserts the claimed name is
nowhere in the output.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.monitor.render import (
    Summary,
    render_detail_line,
    render_frame_line,
    render_replay_startup,
    render_startup,
    render_summary,
)
from sighop.net.rx import decode_event
from sighop.protocol.crypto import sign_advert
from sighop.protocol.identity import generate_identity
from sighop.protocol.packet import Packet, PacketHeader, PayloadType, RouteType, encode
from sighop.protocol.payloads import NodeType, build_advert, build_appdata
from sighop.radio.modem import EU868_NARROW, RadioParams, RxEvent, RxMeta, UnparsedEvent
from sighop.radio.probe import AbsenceReason, Absent, FirmwareVersion, ProbeResult

WHEN = dt.datetime(2026, 9, 3, 21, 15, 4, tzinfo=dt.UTC)


def packet_bytes(
    payload_type: PayloadType,
    payload: bytes,
    *,
    route_type: RouteType = RouteType.FLOOD,
    path: bytes = b"",
    hash_size: int = 1,
) -> bytes:
    return encode(
        Packet(
            header=PacketHeader(
                route_type=route_type, payload_type=payload_type, payload_version=1
            ),
            transport_codes=None,
            hop_count=len(path) // hash_size,
            hash_size=hash_size,
            path=path,
            payload=payload,
        )
    )


def record_for(raw: bytes, *, snr: float | None = 2.0, rssi: int | None = -90):
    meta = None if snr is None or rssi is None else RxMeta(snr_db=snr, rssi_dbm=rssi)
    return decode_event(RxEvent(packet=raw, rx_meta=meta, received_at=WHEN))


def advert_bytes(name: str, *, node_type: NodeType = NodeType.REPEATER, tamper: bool = False):
    identity = generate_identity()
    appdata = build_appdata(node_type, name=name)
    advert = sign_advert(identity, timestamp=1_756_000_000, appdata=appdata)
    payload = bytearray(build_advert(advert))
    if tamper:
        payload[-1] ^= 0xFF  # inside the signed message, so verification fails
    return packet_bytes(PayloadType.ADVERT, bytes(payload), path=b"\xa3\x7f")


# --- Frame line ------------------------------------------------------------


def test_frame_line_is_fixed_width_and_carries_the_reception_facts():
    raw = packet_bytes(PayloadType.ACK, b"\x01\x02\x03\x04", path=b"\xa3\x7f")
    line = render_frame_line(record_for(raw))

    assert line == (
        "21:15:04  ACK         FLOOD    h2  path=a3,7f               "
        "snr=+2.00 rssi= -90   8B"
    )


def test_frame_line_shows_absent_signal_values_as_dashes_not_zeros():
    raw = packet_bytes(PayloadType.ACK, b"\x01\x02\x03\x04")
    line = render_frame_line(record_for(raw, snr=None, rssi=None))

    assert "snr=   -- rssi=  --" in line
    assert "0.00" not in line


def test_frame_line_names_the_route_type():
    raw = packet_bytes(PayloadType.ACK, b"\x01\x02\x03\x04", route_type=RouteType.DIRECT)
    assert " DIRECT " in render_frame_line(record_for(raw))


def test_a_long_path_is_truncated_rather_than_wrapping():
    path = bytes(range(15))
    raw = packet_bytes(PayloadType.ACK, b"\x01\x02\x03\x04", path=path, hash_size=3)
    line = render_frame_line(record_for(raw))

    assert "…" in line
    assert len(line.splitlines()) == 1


# --- Detail lines ----------------------------------------------------------


def test_a_verified_advert_shows_its_mark_name_node_type_and_flags():
    detail = render_detail_line(record_for(advert_bytes("[redacted]")))

    assert "✓ advert" in detail
    assert "'[redacted]'" in detail
    assert "REPEATER" in detail
    assert "flags=0x82" in detail


def test_an_advert_whose_signature_fails_never_shows_its_claimed_name():
    """DESIGN.md §5 and §8, design D8. The rule this whole split exists for."""
    detail = render_detail_line(record_for(advert_bytes("Impostor", tamper=True)))

    assert "Impostor" not in detail
    assert "✓" not in detail
    assert "UNVERIFIED" in detail
    assert "bad_signature" in detail
    assert "node_hash=0x" in detail


def test_an_encrypted_envelope_shows_its_shape_and_says_it_was_not_decrypted():
    payload = bytes([0xA3, 0x7F]) + b"\x11\x22" + b"\x00" * 32
    detail = render_detail_line(record_for(packet_bytes(PayloadType.TXT_MSG, payload)))

    assert "encrypted" in detail
    assert "dest=0xa3" in detail
    assert "src=0x7f" in detail
    assert "mac=1122" in detail
    assert "ciphertext=32B" in detail
    assert "not decrypted" in detail


def test_a_group_envelope_shows_its_channel_hash():
    payload = bytes([0x5A]) + b"\x11\x22" + b"\x00" * 16
    detail = render_detail_line(record_for(packet_bytes(PayloadType.GRP_TXT, payload)))

    assert "channel=0x5a" in detail
    assert "not decrypted" in detail


def test_an_ack_shows_its_checksum_and_tail():
    detail = render_detail_line(
        record_for(packet_bytes(PayloadType.ACK, bytes.fromhex("aabbccdd0102")))
    )

    assert "ack  checksum=aabbccdd" in detail
    assert "tail=0102" in detail


def test_a_trace_shows_its_size_and_bytes():
    detail = render_detail_line(record_for(packet_bytes(PayloadType.TRACE, b"\x01\x02\x03")))

    assert "trace  3B" in detail
    assert "010203" in detail


def test_an_uninterpreted_payload_is_labelled_as_such():
    detail = render_detail_line(record_for(packet_bytes(PayloadType.CONTROL, b"\xde\xad")))

    assert "CONTROL not interpreted" in detail
    assert "dead" in detail


def test_a_decode_failure_shows_the_rule_the_offset_and_the_bytes():
    detail = render_detail_line(record_for(b"\x01"))

    assert "decode failed" in detail
    assert "truncated" in detail
    assert "at offset 1" in detail
    assert "raw=01" in detail


def test_a_modem_unparsed_frame_shows_its_reason_and_bytes():
    record = decode_event(
        UnparsedEvent(raw=b"\xde\xad", reason="dangling escape byte", received_at=WHEN)
    )
    detail = render_detail_line(record)

    assert "unparsed frame" in detail
    assert "dangling escape byte" in detail
    assert "raw=dead" in detail


# --- Startup ---------------------------------------------------------------


def probe_result(**overrides) -> ProbeResult:
    fields = {
        "configured_radio": EU868_NARROW,
        "device_name": "Heltec V3",
        "radio": EU868_NARROW,
        "tx_power_dbm": -20,
        "firmware_version": FirmwareVersion(version=1, reserved=0),
        "battery_mv": 4021,
        "mcu_temp_tenths_c": 253,
        "sensors_raw": bytes.fromhex("01670115"),
    }
    return ProbeResult(**{**fields, **overrides})


def test_startup_line_names_what_the_board_reported():
    text = render_startup(probe_result())

    assert "Heltec V3" in text
    assert "firmware=v1" in text
    assert "tx_power=-20 dBm" in text
    assert "869.618 MHz" in text
    assert "BW 62.5 kHz" in text
    assert "SF8" in text
    assert "CR8" in text
    assert "battery=4021 mV" in text
    assert "mcu_temp=25.3 °C" in text


def test_startup_line_shows_each_unanswered_probe_as_unknown():
    text = render_startup(
        probe_result(
            device_name=Absent(reason=AbsenceReason.UNSUPPORTED, error_code=0x05),
            battery_mv=Absent(reason=AbsenceReason.TIMEOUT),
            radio=Absent(reason=AbsenceReason.TIMEOUT),
        )
    )

    assert "unknown(unsupported)" in text
    assert "battery=unknown(timeout)" in text
    assert "not confirmed by the board" in text


def test_startup_line_states_a_radio_readback_mismatch_prominently():
    wrong = RadioParams(freq_hz=869_525_000, bw_hz=250_000, sf=11, cr=5)
    text = render_startup(probe_result(radio=wrong))

    assert "RADIO READBACK MISMATCH" in text
    assert "869.618 MHz" in text  # configured
    assert "869.525 MHz" in text  # read back


def test_startup_line_says_so_when_there_is_no_probe_result():
    assert "unavailable" in render_startup(None)


def test_replay_startup_uses_the_files_provenance():
    provenance = {
        "kind": "capture_meta",
        "sighop": {"version": "0.1.0", "commit_hash": "abc123"},
        "device_name": {"value": "Heltec V3", "reason": None},
        "firmware_version": {"value": {"version": 1, "reserved": 0}, "reason": None},
        "radio": {"value": EU868_NARROW.as_json(), "reason": None},
    }
    text = render_replay_startup(provenance, "captures/x.jsonl")

    assert "replaying captures/x.jsonl" in text
    assert "Heltec V3" in text
    assert "869.618 MHz" in text
    assert "0.1.0" in text


def test_replay_startup_states_missing_provenance_rather_than_inventing_it():
    text = render_replay_startup(None, "captures/2026-09-02.jsonl")

    assert "provenance absent" in text
    assert "no capture_meta header" in text


# --- Summary ---------------------------------------------------------------


def test_summary_line_reports_the_session_counts():
    line = render_summary(
        Summary(
            frames=351,
            decode_failures=0,
            adverts_verified=53,
            adverts_failed=1,
            node_hashes=12,
            reconnects=2,
            reboots=3,
        )
    )

    assert line == (
        "-- frames=351 failed=0 adverts=53 advert_failures=1 "
        "nodes_heard=12 reconnects=2 reboots=3"
    )


@pytest.mark.parametrize("snr", [None, 2.0])
def test_rendering_touches_no_io(snr):
    """The `monitor-cli` isolation scenario: text out of a record, nothing else."""
    record = record_for(packet_bytes(PayloadType.ACK, b"\x01\x02\x03\x04"), snr=snr)
    assert isinstance(render_frame_line(record), str)
    assert isinstance(render_detail_line(record), str)
