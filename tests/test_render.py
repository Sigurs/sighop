"""Monitor rendering, by string comparison (milestone 2, design D8).

Pure functions in, text out — no device, no file, no event loop. The test that
carries weight is the failed-advert one: it asserts the claimed name is
nowhere in the output.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

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


# --- The runtime status line (milestone 3) ---------------------------------


def _status(**overrides):
    from sighop.net.tx import SchedulerStats, SchedulerStatus

    fields: dict[str, Any] = {
        "transmit_enabled": False,
        "duty_cycle_pct": 12.5,
        "duty_cycle_used_ms": 45_000.0,
        "duty_cycle_ceiling_ms": 360_000.0,
        "reserve_reached": False,
        "above_regulatory_default": False,
        "queue_depths": {0: 0, 1: 0, 2: 2, 3: 1},
        "stats": SchedulerStats(transmitted=4, suppressed=7, dropped=1, failed=2),
    }
    fields.update(overrides)
    return SchedulerStatus(**fields)


def _dedup_stats(**overrides):
    from sighop.net.dedup import DedupStats

    fields: dict[str, Any] = {
        "considered": 100,
        "duplicates": 11,
        "passed_through": 89,
        "entries": 42,
        "peak_entries": 51,
        "max_entries": 4096,
        "ttl_seconds": 300.0,
        "widest_interval_seconds": 4.6,
        "evictions_by_age": 0,
        "evictions_by_cap": 0,
    }
    fields.update(overrides)
    return DedupStats(**fields)


def test_the_status_line_leads_with_the_gate_and_the_duty_cycle():
    from sighop.monitor.render import render_status

    line = render_status(
        _status(), dedup=_dedup_stats(), learned_paths=5, contacts=3
    )

    # Milestone 5 appends persistence: the state, and three counters that read
    # zero rather than being omitted (`runtime-cli` spec).
    assert line == (
        "== TX=disabled duty= 12.5% (45/360s) q=0/0/2/1 tx=4 sup=7 drop=1 fail=2 "
        "dup=11.0% cache=42/4096 paths=5 contacts=3 "
        "persist=off log_drop=0 route_drop=0 backfill=0"
    )


def test_an_enabled_transmitter_says_so_in_the_status_line():
    from sighop.monitor.render import render_status

    line = render_status(_status(transmit_enabled=True), dedup=_dedup_stats(), learned_paths=0)

    assert line.startswith("== TX=ENABLED ")


def test_the_status_line_marks_the_reserve_and_a_raised_ceiling():
    from sighop.monitor.render import render_status

    line = render_status(
        _status(reserve_reached=True, above_regulatory_default=True),
        dedup=_dedup_stats(),
        learned_paths=0,
    )

    assert "RESERVE(classes 2-3 stalled)" in line
    assert "ABOVE-10%" in line


def test_the_status_line_reports_an_active_advert_override():
    from sighop.monitor.render import render_status

    line = render_status(_status(), dedup=_dedup_stats(), learned_paths=0, active_overrides=2)

    assert line.endswith("overrides=2")


def test_the_startup_banner_states_that_nothing_will_be_sent():
    from sighop.monitor.render import render_run_startup

    text = render_run_startup(
        "modem: Heltec V4 OLED",
        transmit_enabled=False,
        ceiling_pct=10.0,
        above_regulatory_default=False,
    )

    assert "transmit disabled — nothing will be sent" in text


def test_the_startup_banner_is_emphatic_when_transmit_is_enabled():
    from sighop.monitor.render import render_run_startup

    text = render_run_startup(
        "modem: Heltec V4 OLED",
        transmit_enabled=True,
        ceiling_pct=10.0,
        above_regulatory_default=False,
    )

    assert "** TRANSMIT ENABLED **" in text
    assert "360 s/hour" in text


def test_the_startup_banner_warns_about_a_raised_ceiling():
    from sighop.monitor.render import render_run_startup

    text = render_run_startup(
        "modem: Heltec V4 OLED",
        transmit_enabled=True,
        ceiling_pct=50.0,
        above_regulatory_default=True,
    )

    assert "above the EU 868 10% limit" in text


def test_stubs_are_rendered_as_ephemeral():
    from sighop.monitor.render import render_stubs
    from sighop.net.adverts import EntityStub
    from sighop.protocol.identity import generate_identity

    stub = EntityStub(entity_id="stub-1", name="skogen", identity=generate_identity())
    text = render_stubs([stub])

    assert "skogen" in text
    assert "ephemeral" in text
    assert render_stubs([]) == "stubs: none"


# --- Direct messages (milestone 4) -----------------------------------------
#
# Pure formatting, asserted by string comparison. The rule under test is the one
# §8 makes hard: a decrypted direct message's sender is *claimed*, and never
# appears in the shape reserved for signature-verified content.


def _contact(name: str = "peer"):
    from sighop.net.contacts import Contact
    from sighop.protocol.payloads import WireText

    return Contact(
        public_key=bytes(range(32)),
        name=WireText.from_bytes(name.encode()),
        advert_verified=True,
    )


def test_a_sent_message_line_names_its_route_attempt_and_expected_ack():
    from sighop.monitor.render import render_message_sent
    from sighop.net.dm import MessageSent, Route

    line = render_message_sent(
        MessageSent(
            message_id="m1",
            entity_name="skogen",
            contact=_contact(),
            text="hej",
            attempt=1,
            route=Route(flood=False),
            packet_id="pkt1",
            size_bytes=48,
            airtime_ms=612.0,
            ack_timeout_ms=4422.0,
            expected_ack=b"\xde\xad\xbe\xef",
            transmitted=True,
        )
    )

    assert line == (
        "          -> dm sent  to 'peer'  attempt 1  DIRECT h0  48B  "
        "air=612ms  ack_by=4422ms  expect=deadbeef  id=pkt1"
    )


def test_a_suppressed_send_says_the_gate_was_closed():
    from sighop.monitor.render import render_message_sent
    from sighop.net.dm import MessageSent, Route

    line = render_message_sent(
        MessageSent(
            message_id="m1",
            entity_name="skogen",
            contact=_contact(),
            text="hej",
            attempt=0,
            route=Route(flood=True),
            packet_id="pkt1",
            size_bytes=48,
            airtime_ms=612.0,
            ack_timeout_ms=10292.0,
            expected_ack=b"\xde\xad\xbe\xef",
            transmitted=False,
        )
    )

    assert "not sent (gate closed)" in line
    assert "FLOOD" in line


def test_a_matched_acknowledgement_names_the_attempt_and_latency():
    from sighop.monitor.render import render_ack_matched
    from sighop.net.dm import AckMatched

    line = render_ack_matched(
        AckMatched(
            message_id="m1",
            contact=_contact(),
            checksum=b"\xde\xad\xbe\xef",
            attempt=2,
            latency_ms=812.0,
            packet_id="pkt9",
            payload_bytes=6,
        )
    )

    assert line == (
        "          <- ack deadbeef (6B) matched attempt 2 from 'peer' after "
        "812ms  id=pkt9"
    )


def test_an_unmatched_acknowledgement_says_how_many_sends_were_outstanding():
    from sighop.monitor.render import render_ack_unmatched
    from sighop.net.dm import AckUnmatched

    line = render_ack_unmatched(
        AckUnmatched(checksum=b"\x01\x02\x03\x04", packet_id="pkt9", outstanding=2)
    )

    assert line == "          <- ack 01020304 matched none of 2 outstanding sends  id=pkt9"


def test_a_delivered_send_and_an_unacknowledged_one_read_differently():
    from sighop.monitor.render import render_send_resolved
    from sighop.net.dm import Route, SendOutcome, SendResolved, SendResult

    delivered = render_send_resolved(
        SendResolved(
            outcome=SendOutcome(
                result=SendResult.ACKNOWLEDGED,
                message_id="m1",
                attempts=1,
                route=Route(flood=False),
                ack_latency_ms=812.0,
            ),
            contact=_contact(),
            text="hej",
        )
    )
    failed = render_send_resolved(
        SendResolved(
            outcome=SendOutcome(
                result=SendResult.UNACKNOWLEDGED,
                message_id="m2",
                attempts=4,
                route=Route(flood=False),
                reason="no acknowledgement after 4 attempts",
            ),
            contact=_contact(),
            text="hej",
        )
    )

    assert delivered == (
        "          dm delivered to 'peer' after 1 attempt(s) in 812ms  id=m1"
    )
    assert failed == (
        "          ! dm unacknowledged to 'peer' after 4 attempt(s): "
        "no acknowledgement after 4 attempts  id=m2"
    )


def test_a_received_message_marks_its_sender_as_claimed_and_never_verified():
    from sighop.monitor.render import VERIFIED_MARK, render_message_received
    from sighop.net.dm import MessageReceived
    from sighop.protocol.payloads import TextMessageBody, TextType, WireText

    line = render_message_received(
        MessageReceived(
            entity_name="skogen",
            contact=_contact(),
            body=TextMessageBody(
                timestamp=1_700_000_000,
                txt_type=TextType.PLAIN,
                attempt=0,
                text=WireText.from_bytes(b"hej"),
            ),
            packet_id="pkt3",
            candidates_tried=2,
            acknowledged=True,
        )
    )

    assert "claimed 'peer'" in line
    assert VERIFIED_MARK not in line, "a 2-byte MAC match was rendered as verified"
    assert line == (
        "          ✗ dm from claimed 'peer' (key=0001020304050607) to skogen: "
        "'hej'  ts=1700000000 attempt=0 tried=2 acked  id=pkt3"
    )


def test_an_undecryptable_message_reports_the_candidate_count():
    from sighop.monitor.render import render_message_undecryptable
    from sighop.net.dm import MessageUndecryptable

    line = render_message_undecryptable(
        MessageUndecryptable(
            packet_id="pkt4", dest_hash=0x2A, src_hash=0x91, candidates_tried=3
        )
    )

    assert line == (
        "          encrypted dm  dest=0x2a src=0x91  not decrypted after "
        "3 candidate key(s)  id=pkt4"
    )


def test_a_message_that_did_not_parse_says_it_was_not_acknowledged():
    from sighop.monitor.render import render_message_unparsable
    from sighop.net.dm import MessageUnparsable

    line = render_message_unparsable(
        MessageUnparsable(
            packet_id="pkt5",
            entity_name="skogen",
            contact=_contact(),
            reason="truncated: body too short",
        )
    )

    assert "not acknowledged" in line
    assert "truncated" in line


def test_persistent_entities_are_not_rendered_as_ephemeral():
    from sighop.monitor.render import render_stubs
    from sighop.net.adverts import EntityStub
    from sighop.protocol.identity import generate_identity

    stub = EntityStub(
        entity_id="skogen",
        name="skogen",
        identity=generate_identity(),
        persistent=True,
        keyfile="/tmp/skogen.json",
    )

    text = render_stubs([stub])

    assert "persistent" in text
    assert "ephemeral" not in text


def test_the_banner_with_the_gate_open_names_no_identity_when_there_is_none():
    from sighop.monitor.render import render_run_startup

    text = render_run_startup(
        "modem: Heltec V4 OLED",
        transmit_enabled=True,
        ceiling_pct=10.0,
        above_regulatory_default=False,
    )

    assert "packets WILL be transmitted on air" in text
    assert "originating identities: none" in text
