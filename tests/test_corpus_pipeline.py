"""The regression corpus, replayed through the *live* pipeline.

`tests/protocol/test_corpus.py` runs the same 997 frames through the protocol
functions directly and stays exactly as it is. This is a second, higher-level
check over the same evidence: capture file → replay source → `net/rx.py`,
which is the path a live link takes, with only the source swapped (design D1).

Its value is entirely in the cross-check. If the pipeline and the offline
harness ever disagree about what these frames are, one of them has drifted —
and until this file existed, nothing would have said which.
"""

from __future__ import annotations

import collections

import pytest

from sighop.net.rx import AdvertOutcome, RxRecord, decode_event, outcome_fields
from sighop.protocol.crypto import VerifiedAdvert, verify_advert
from sighop.protocol.packet import PayloadType, RouteType, decode
from sighop.protocol.payloads import Advert, parse_payload
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import (
    CAPTURE_FILES,
    CAPTURES_DIR,
    EXPECTED_FRAME_COUNT,
    load_corpus,
)
from tests.protocol.test_corpus import (
    EXPECTED_ADVERT_COUNT,
    EXPECTED_HASH_SIZES,
    EXPECTED_HOP_COUNTS,
    EXPECTED_PAYLOAD_TYPES,
    EXPECTED_ROUTE_TYPES,
    EXPECTED_TRANSPORT_FRAMES,
)


@pytest.fixture(scope="module")
def replayed() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
        assert replay.unreadable == [], f"{name}: {replay.unreadable}"
    return records


def test_every_corpus_frame_produces_an_outcome_through_the_live_pipeline(replayed):
    assert len(replayed) == EXPECTED_FRAME_COUNT
    assert all(record.outcome is not None for record in replayed)


def test_the_aggregate_decode_failure_count_is_zero(replayed):
    failures = [(record.raw.hex(), outcome_fields(record)) for record in replayed if record.failed]
    assert failures == []


def test_the_pipeline_agrees_with_the_offline_harness_on_payload_types(replayed):
    counts = collections.Counter(record.payload_type for record in replayed)
    assert dict(counts) == EXPECTED_PAYLOAD_TYPES


def test_the_pipeline_agrees_with_the_offline_harness_on_route_types(replayed):
    counts = collections.Counter(record.route_type for record in replayed)
    assert dict(counts) == EXPECTED_ROUTE_TYPES


def test_the_pipeline_agrees_with_the_offline_harness_on_hop_counts(replayed):
    counts = collections.Counter(record.hop_count for record in replayed)
    assert dict(counts) == EXPECTED_HOP_COUNTS


def test_the_pipeline_agrees_with_the_offline_harness_on_path_hash_sizes(replayed):
    counts = collections.Counter(record.hash_size for record in replayed)
    assert dict(counts) == EXPECTED_HASH_SIZES


def test_the_pipeline_verifies_exactly_the_adverts_the_harness_does(replayed):
    """Same adverts verified, same names — the two paths must not diverge."""
    pipeline_names = [
        record.outcome.verified.appdata.name.text
        for record in replayed
        if isinstance(record.outcome, AdvertOutcome)
        and isinstance(record.outcome.verification, VerifiedAdvert)
    ]

    harness_names = []
    for frame in load_corpus():
        packet = decode(frame.raw)
        if packet.payload_type is not PayloadType.ADVERT:
            continue
        parsed = parse_payload(packet.payload_type, packet.payload)
        assert isinstance(parsed, Advert)
        verified = verify_advert(parsed)
        assert isinstance(verified, VerifiedAdvert)
        harness_names.append(verified.appdata.name.text)

    assert len(pipeline_names) == EXPECTED_ADVERT_COUNT
    assert pipeline_names == harness_names


def test_the_pipeline_carries_the_recorded_signal_values(replayed):
    """SNR and RSSI survive capture, replay and decode unchanged."""
    from tests.protocol.corpus import load_corpus as frames

    for record, frame in zip(replayed, frames(), strict=True):
        assert record.snr_db == frame.snr_db
        assert record.rssi_dbm == frame.rssi_dbm


def test_the_pipeline_sees_the_same_single_transport_routed_frame(replayed):
    """The offline harness asserts this frame's transport codes; here the point
    is only that the live path classifies it the same way and does not drop it.
    """
    routed = [
        record
        for record in replayed
        if record.route_type in (RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT)
    ]

    assert len(routed) == EXPECTED_TRANSPORT_FRAMES
    assert routed[0].route_type is RouteType.TRANSPORT_FLOOD
    assert not routed[0].failed
