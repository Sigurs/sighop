"""The live RX decode stage (milestone 2, `rx-decode`).

Driven from the replay source over real captured frames wherever the corpus
holds one — a synthetic event proves the pipeline handles what we imagined,
which is the weaker of the two things worth knowing. Hand-built events appear
only for the shapes the corpus lacks: a truncated frame, a bad signature, a
reserved payload type.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from sighop.net.rx import (
    AdvertOutcome,
    ModemUnparsed,
    Payload,
    PayloadFailure,
    RxRecord,
    StructuralFailure,
    Uninterpreted,
    decode_event,
    decode_stream,
    discovery_summary,
    outcome_fields,
)
from sighop.protocol.crypto import AdvertVerificationFailure, VerifiedAdvert
from sighop.protocol.packet import PayloadType, RouteType
from sighop.protocol.payloads import DirectEnvelope, GroupEnvelope
from sighop.radio.modem import RxEvent, RxMeta, UnparsedEvent
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import (
    AMBIENT,
    CORPUS_DIR,
    CORPUS_FILES,
    EXPECTED_RECEIVED_COUNT,
    EXPECTED_TRANSMITTED_COUNT,
)
from tests.protocol.synthetic import discover_responder, discover_response_payload

EXPECTED_RECEIVED_ADVERT_COUNT = 79
"""Adverts the mesh sent us. Two fewer than the 81 the corpus holds: the exchange
file carries two adverts sighop itself transmitted (its own and the room it
hosts, each zero-hop), and this stage only ever sees receptions."""


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    """Every corpus *reception*, decoded through the live pipeline.

    A replay yields receptions only: the frames sighop transmitted are recorded
    in the corpus but skipped here, because feeding our own packets back in as
    receptions would invent traffic the radio never heard.
    """
    records: list[RxRecord] = []
    skipped = 0
    for name in CORPUS_FILES:
        replay = CaptureReplay.open(CORPUS_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
        assert replay.unreadable == [], (
            f"{name} has unreadable lines: {[str(u) for u in replay.unreadable]}"
        )
        skipped += replay.transmitted_skipped
    assert skipped == EXPECTED_TRANSMITTED_COUNT, (
        "the replay did not pass over exactly the frames sighop transmitted"
    )
    return records


def find(records: list[RxRecord], payload_type: PayloadType) -> RxRecord:
    for record in records:
        if record.payload_type is payload_type:
            return record
    raise AssertionError(f"no {payload_type.name} frame in the corpus")


# --- Decoding --------------------------------------------------------------


def test_a_well_formed_packet_decodes_into_a_full_record(corpus_records):
    record = find(corpus_records, PayloadType.ADVERT)

    assert record.packet is not None
    assert record.route_type in RouteType
    assert record.hop_count is not None
    assert record.hash_size in (1, 2, 3)
    assert record.path == record.packet.path
    assert record.size_bytes == len(record.raw)


def test_signal_values_are_carried_when_the_modem_correlated_rxmeta(corpus_records):
    with_signal = [r for r in corpus_records if r.snr_db is not None]
    assert with_signal, "the corpus records RxMeta for its frames"
    assert all(r.rssi_dbm is not None for r in with_signal)


def test_an_event_with_no_rxmeta_carries_absent_signal_values_not_zeros():
    record = decode_event(RxEvent(packet=b"\x04\x00", rx_meta=None))

    assert record.snr_db is None
    assert record.rssi_dbm is None


def test_replayed_timestamps_are_used_rather_than_the_wall_clock():
    when = dt.datetime(2026, 9, 2, 21, 15, 4, tzinfo=dt.UTC)
    record = decode_event(RxEvent(packet=b"\x04\x00", rx_meta=None, received_at=when))

    assert record.received_at == when


# --- Every frame produces an outcome ---------------------------------------


def test_every_corpus_frame_produces_an_outcome_and_none_unexpectedly_fail(corpus_records):
    """Every reception produces some outcome, and the only failure among them
    is the one known `ModemUnparsed` artifact at the start of the room-server
    session — a stray `RxMeta` the modem reported before any Data frame,
    forwarded rather than dropped (`net/rx.py`'s own docstring for the case).
    Any other failure means a real frame stopped decoding.
    """
    assert len(corpus_records) == EXPECTED_RECEIVED_COUNT
    failures = [r for r in corpus_records if r.failed]
    unexpected = [r for r in failures if not isinstance(r.outcome, ModemUnparsed)]
    assert unexpected == [], [outcome_fields(r) for r in unexpected]
    assert len(failures) == 1, [outcome_fields(r) for r in failures]


def test_a_structurally_invalid_frame_reports_the_violated_rule():
    record = decode_event(RxEvent(packet=b"\x01", rx_meta=None))  # ends before path length

    assert isinstance(record.outcome, StructuralFailure)
    assert record.failed
    assert record.packet is None
    assert record.outcome.failure.offset == 1
    assert record.outcome.failure.raw == b"\x01"


def test_a_payload_failure_keeps_the_header_and_path_observable():
    # A valid FLOOD/ACK header with a 3-byte payload: ACK is 4 or 6 bytes.
    raw = bytes([RouteType.FLOOD | (PayloadType.ACK << 2), 0x00]) + b"\x01\x02\x03"
    record = decode_event(RxEvent(packet=raw, rx_meta=None))

    assert isinstance(record.outcome, PayloadFailure)
    assert record.packet is not None
    assert record.payload_type is PayloadType.ACK
    assert record.hop_count == 0


def test_an_uninterpreted_payload_type_is_an_outcome_not_a_failure():
    raw = bytes([RouteType.FLOOD | (PayloadType.CONTROL << 2), 0x00]) + b"\xde\xad"
    record = decode_event(RxEvent(packet=raw, rx_meta=None))

    assert isinstance(record.outcome, Uninterpreted)
    assert record.outcome.payload_type is PayloadType.CONTROL
    assert record.outcome.raw == b"\xde\xad"
    assert not record.failed


# --- Node discovery ---------------------------------------------------------

DISCOVER_REQ_FRAME = bytes([RouteType.DIRECT | (PayloadType.CONTROL << 2), 0x00]) + bytes.fromhex(
    "80069a7d3916"
)
DISCOVER_RESP_FRAME = (
    bytes([RouteType.DIRECT | (PayloadType.CONTROL << 2), 0x00]) + discover_response_payload()
)
RESPONDER_KEY_PREFIX = discover_responder().public_key[:8].hex()


def test_a_discovery_request_has_its_own_outcome():
    record = decode_event(RxEvent(packet=DISCOVER_REQ_FRAME, rx_meta=None))

    assert not record.failed
    assert outcome_fields(record) == {
        "outcome": "discover_request",
        "control_tag": "9a7d3916",
        "discover_filter": 0x06,
        "discover_prefix_only": False,
    }


def test_a_discovery_response_has_its_own_outcome_and_only_a_key_prefix():
    record = decode_event(RxEvent(packet=DISCOVER_RESP_FRAME, rx_meta=None))

    assert not record.failed
    assert outcome_fields(record) == {
        "outcome": "discover_response",
        "control_tag": "9a7d3916",
        "discover_node_type": "REPEATER",
        "discover_snr_db": 11.75,
        "discover_key_prefix": RESPONDER_KEY_PREFIX,
    }


def test_a_malformed_discovery_payload_is_a_payload_failure():
    record = decode_event(RxEvent(packet=DISCOVER_REQ_FRAME + b"\x00", rx_meta=None))

    assert isinstance(record.outcome, PayloadFailure)
    assert record.payload_type is PayloadType.CONTROL


def test_discovery_summary_for_a_request():
    record = decode_event(RxEvent(packet=DISCOVER_REQ_FRAME, rx_meta=None))

    assert discovery_summary(record) == "discover request  tag=9a7d3916  wants=CHAT, REPEATER"


def test_discovery_summary_for_a_response_marks_the_key_as_a_claim():
    record = decode_event(RxEvent(packet=DISCOVER_RESP_FRAME, rx_meta=None))

    assert discovery_summary(record) == (
        "discover response  tag=9a7d3916  REPEATER  reported snr=+11.75 dB  "
        f"claimed key={RESPONDER_KEY_PREFIX} (unauthenticated)"
    )


def test_discovery_summary_is_absent_for_anything_else():
    raw = bytes([RouteType.FLOOD | (PayloadType.CONTROL << 2), 0x00]) + b"\xde\xad"

    assert discovery_summary(decode_event(RxEvent(packet=raw, rx_meta=None))) is None


def test_a_modem_unparsed_frame_is_forwarded_with_its_reason():
    record = decode_event(UnparsedEvent(raw=b"\xde\xad", reason="dangling escape byte"))

    assert isinstance(record.outcome, ModemUnparsed)
    assert record.outcome.reason == "dangling escape byte"
    assert record.raw == b"\xde\xad"
    assert record.packet is None


# --- Adverts ---------------------------------------------------------------


def test_a_verified_advert_carries_its_name_through_the_verification(corpus_records):
    adverts = [r for r in corpus_records if isinstance(r.outcome, AdvertOutcome)]
    assert len(adverts) == EXPECTED_RECEIVED_ADVERT_COUNT

    verified = [r.outcome.verified for r in adverts]
    assert all(isinstance(v, VerifiedAdvert) for v in verified)
    assert all(v.appdata.name is not None for v in verified)


def test_a_bad_signature_yields_a_verification_failure_with_no_content(corpus_records):
    advert = find(corpus_records, PayloadType.ADVERT)
    tampered = bytearray(advert.raw)
    tampered[-1] ^= 0xFF  # last appdata byte: inside the signed message

    record = decode_event(RxEvent(packet=bytes(tampered), rx_meta=None))

    assert isinstance(record.outcome, AdvertOutcome)
    assert isinstance(record.outcome.verification, AdvertVerificationFailure)
    assert record.outcome.verified is None
    # the failure type carries no advert content at all
    assert not hasattr(record.outcome.verification, "appdata")


# --- Encrypted payloads ----------------------------------------------------


def test_an_encrypted_payload_with_no_key_is_a_normal_outcome(corpus_records):
    record = find(corpus_records, PayloadType.TXT_MSG)

    assert isinstance(record.outcome, Payload)
    assert isinstance(record.outcome.payload, DirectEnvelope)
    assert not record.failed
    assert record.src_hash == record.outcome.payload.src_hash
    assert record.dest_hash == record.outcome.payload.dest_hash
    assert outcome_fields(record)["decrypt_outcome"] == "no_key_held"


def test_a_group_payload_reports_its_channel_hash(corpus_records):
    record = find(corpus_records, PayloadType.GRP_TXT)

    assert isinstance(record.outcome.payload, GroupEnvelope)
    assert record.dest_hash == record.outcome.payload.channel_hash


# --- Reception identity ----------------------------------------------------


def test_identical_bytes_received_twice_get_different_reception_ids():
    event = RxEvent(packet=b"\x04\x00", rx_meta=None)
    first, second = decode_event(event), decode_event(event)

    assert first.packet_id != second.packet_id


def test_every_corpus_record_has_a_distinct_reception_id(corpus_records):
    ids = {record.packet_id for record in corpus_records}
    assert len(ids) == len(corpus_records)


# --- The wide event --------------------------------------------------------


class RecordingLogger:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event, **fields):
        self.events.append(("info", event, fields))

    def error(self, event, **fields):
        self.events.append(("error", event, fields))


async def stream(events, logger):
    return [record async for record in decode_stream(_aiter(events), logger=logger)]


async def _aiter(events):
    for event in events:
        yield event


async def test_one_packet_rx_event_per_frame_with_the_design_fields():
    logger = RecordingLogger()
    advert_raw = _first_advert_bytes()

    await stream([RxEvent(packet=advert_raw, rx_meta=RxMeta(snr_db=2.0, rssi_dbm=-90))], logger)

    assert len(logger.events) == 1
    level, name, fields = logger.events[0]
    assert (level, name) == ("info", "packet_rx")
    for key in (
        "packet_id",
        "route_type",
        "payload_type",
        "path_len",
        "path",
        "size_bytes",
        "snr",
        "rssi",
        "src_hash",
        "outcome",
    ):
        assert key in fields, key
    assert fields["snr"] == 2.0
    assert fields["rssi"] == -90
    assert fields["outcome"] == "advert_verified"
    # Milestone 3 and 4 fields are omitted, not stubbed.
    assert "dup" not in fields
    assert "matched_entities" not in fields
    assert "airtime_ms" not in fields


async def test_a_frame_that_fails_to_decode_still_emits_a_packet_rx_event():
    logger = RecordingLogger()

    await stream([RxEvent(packet=b"\x01", rx_meta=RxMeta(snr_db=-2.0, rssi_dbm=-120))], logger)

    level, name, fields = logger.events[0]
    assert (level, name) == ("error", "packet_rx")
    assert fields["outcome"] == "structural_failure"
    assert fields["size_bytes"] == 1
    assert fields["snr"] == -2.0
    assert fields["rssi"] == -120
    assert fields["failure_reason"] == "truncated"


async def test_the_reception_id_threads_from_the_record_into_its_log_event():
    logger = RecordingLogger()
    records = await stream([RxEvent(packet=b"\x04\x00", rx_meta=None)], logger)

    assert logger.events[0][2]["packet_id"] == records[0].packet_id


async def test_the_stage_keeps_no_state_between_frames():
    """Design D9: duplicates stay visible as duplicates."""
    logger = RecordingLogger()
    event = RxEvent(packet=_first_advert_bytes(), rx_meta=None)

    records = await stream([event, event, event], logger)

    assert len(records) == 3
    assert len(logger.events) == 3
    assert all(isinstance(r.outcome, AdvertOutcome) for r in records)


def _first_advert_bytes() -> bytes:
    with (CORPUS_DIR / AMBIENT).open() as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("kind") != "rx_frame":
                continue
            raw = bytes.fromhex(record["raw_hex"])
            decoded = decode_event(RxEvent(packet=raw, rx_meta=None))
            if decoded.payload_type is PayloadType.ADVERT:
                return raw
    raise AssertionError("no advert in the corpus")
