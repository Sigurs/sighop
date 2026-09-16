"""The regression corpus, replayed through the *live* pipeline.

`tests/protocol/test_corpus.py` runs the corpus through the protocol functions
directly and holds the recorded distributions. This is a second, higher-level
check over the same evidence: capture file → replay source → `net/rx.py`,
which is the path a live link takes, with only the source swapped (design D1).

Its value is entirely in the cross-check, so it asserts *agreement* rather than
re-recording numbers that live next door: if the pipeline and the offline
harness ever disagree about what these frames are, one of them has drifted —
and until this file existed, nothing would have said which.

Both sides are the **receptions**. Milestone 4 added frames sighop transmitted
to the corpus, and a replay does not yield them: replaying our own packets as
receptions would invent traffic the radio never heard.
"""

from __future__ import annotations

import collections

import pytest

from sighop.net.rx import AdvertOutcome, ModemUnparsed, RxRecord, decode_event, outcome_fields
from sighop.protocol.crypto import VerifiedAdvert, verify_advert
from sighop.protocol.packet import PayloadType, RouteType, decode
from sighop.protocol.payloads import Advert, parse_payload
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import (
    CAPTURE_FILES,
    CAPTURES_DIR,
    EXPECTED_RECEIVED_COUNT,
    EXPECTED_TRANSMITTED_COUNT,
    received_frames,
)
from tests.protocol.test_corpus import EXPECTED_TRANSPORT_FRAMES


def harness_packets():
    """The offline harness's view of the same receptions."""
    return [decode(frame.raw) for frame in received_frames()]


@pytest.fixture(scope="module")
def replayed() -> list[RxRecord]:
    records: list[RxRecord] = []
    skipped = 0
    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
        assert replay.unreadable == [], f"{name}: {replay.unreadable}"
        skipped += replay.transmitted_skipped
    assert skipped == EXPECTED_TRANSMITTED_COUNT, (
        "a transmitted frame was replayed as a reception, or one went missing"
    )
    return records


def test_every_corpus_frame_produces_an_outcome_through_the_live_pipeline(replayed):
    assert len(replayed) == EXPECTED_RECEIVED_COUNT
    assert all(record.outcome is not None for record in replayed)


def decoded_only(replayed: list[RxRecord]) -> list[RxRecord]:
    """The receptions that produced a packet — what the offline harness sees.

    The room-server session's capture carries one `ModemUnparsed` reception (a
    stray `RxMeta` at modem startup, forwarded rather than dropped): it has no
    packet and so no counterpart in `harness_packets()`, which only ever sees
    the corpus's `rx_frame`/`tx_frame` records.
    """
    return [record for record in replayed if record.packet is not None]


def test_the_aggregate_decode_failure_count_is_zero(replayed):
    """Zero failures among frames that reached the decoder. The one known
    exception is `ModemUnparsed`, which never reached it at all — the modem
    itself could not associate an `RxMeta` with a Data frame, so there is no
    packet here for our decoder to have failed on.
    """
    failures = [
        (record.raw.hex(), outcome_fields(record))
        for record in replayed
        if record.failed and not isinstance(record.outcome, ModemUnparsed)
    ]
    assert failures == []


def test_the_pipeline_agrees_with_the_offline_harness_on_payload_types(replayed):
    counts = collections.Counter(record.payload_type for record in decoded_only(replayed))
    harness = collections.Counter(packet.payload_type for packet in harness_packets())
    assert dict(counts) == dict(harness)


def test_the_pipeline_agrees_with_the_offline_harness_on_route_types(replayed):
    counts = collections.Counter(record.route_type for record in decoded_only(replayed))
    harness = collections.Counter(packet.route_type for packet in harness_packets())
    assert dict(counts) == dict(harness)


def test_the_pipeline_agrees_with_the_offline_harness_on_hop_counts(replayed):
    counts = collections.Counter(record.hop_count for record in decoded_only(replayed))
    harness = collections.Counter(packet.hop_count for packet in harness_packets())
    assert dict(counts) == dict(harness)


def test_the_pipeline_agrees_with_the_offline_harness_on_path_hash_sizes(replayed):
    counts = collections.Counter(record.hash_size for record in decoded_only(replayed))
    harness = collections.Counter(packet.hash_size for packet in harness_packets())
    assert dict(counts) == dict(harness)


def test_the_pipeline_verifies_exactly_the_adverts_the_harness_does(replayed):
    """Same adverts verified, same names — the two paths must not diverge."""
    pipeline_names = [
        record.outcome.verified.appdata.name.text
        for record in replayed
        if isinstance(record.outcome, AdvertOutcome)
        and isinstance(record.outcome.verification, VerifiedAdvert)
    ]

    harness_names = []
    for packet in harness_packets():
        if packet.payload_type is not PayloadType.ADVERT:
            continue
        parsed = parse_payload(packet.payload_type, packet.payload)
        assert isinstance(parsed, Advert)
        verified = verify_advert(parsed)
        assert isinstance(verified, VerifiedAdvert)
        harness_names.append(verified.appdata.name.text)

    assert pipeline_names, "no adverts verified through the live pipeline"
    assert pipeline_names == harness_names


def test_the_pipeline_carries_the_recorded_signal_values(replayed):
    """SNR and RSSI survive capture, replay and decode unchanged.

    Paired against `received_frames()` rather than `replayed` directly: the
    room-server session's `ModemUnparsed` reception has no `CorpusFrame`
    counterpart (it is not a `rx_frame`/`tx_frame` record) and carries no
    signal values of its own to compare.
    """
    for record, frame in zip(decoded_only(replayed), received_frames(), strict=True):
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


# --- Channels (change `channel-messaging`, task 6.2) -------------------------


ALL_CAPTURES = tuple(sorted(path.name for path in CAPTURES_DIR.glob("*.jsonl")))
"""Every capture, as `tests/protocol/test_channel_foreign_decrypt.py` reads them:
the Public channel's 85 distinct frames span files the protocol corpus does not
name."""

EXPECTED_PUBLIC_DECRYPTED = 85
EXPECTED_UNKNOWN_CHANNEL = 108


async def _replay_with_channels(*, public_loaded: bool) -> dict[str, int]:
    """Every capture through ingest, the bus, the contact store and the channel
    messenger, each subscription drained after every frame so no subscriber
    queue overflows and the count is a count of frames rather than of drops."""
    from sighop.net.bus import IngressPipeline, NetworkBus
    from sighop.net.channels import ChannelKind, ChannelMessenger, ChannelSet, LoadedChannel
    from sighop.net.contacts import ContactStore
    from sighop.net.dedup import DedupCache
    from sighop.net.paths import PathStore
    from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY
    from tests.test_tx import RecordingLogger

    submissions: list[object] = []

    def submit(submission: object) -> object:
        submissions.append(submission)
        raise AssertionError("a replay with channels loaded submitted a packet")

    bus = NetworkBus(logger=RecordingLogger())
    contacts = ContactStore(logger=RecordingLogger())
    contact_queue = bus.subscribe("contacts")
    channel_queue = bus.subscribe("channels")
    pipeline = IngressPipeline(
        bus=bus, dedup=DedupCache(), paths=PathStore(), logger=RecordingLogger()
    )
    channels = ChannelSet(
        channels=(LoadedChannel(1, "Public", ChannelKind.PUBLIC, PUBLIC_CHANNEL_KEY),)
        if public_loaded
        else ()
    )
    messenger = ChannelMessenger(
        submit=submit,  # type: ignore[arg-type]
        channels=channels,
        logger=RecordingLogger(),
    )
    pipeline.observers.append(messenger.observe)
    for name in ALL_CAPTURES:
        for event in CaptureReplay.open(CAPTURES_DIR / name).read():
            pipeline.ingest(decode_event(event))
            while not channel_queue.queue.empty():
                await messenger.handle(channel_queue.queue.get_nowait())
            while not contact_queue.queue.empty():
                await contacts.handle(contact_queue.queue.get_nowait())
    return {
        "considered": pipeline.dedup.stats.considered,
        "duplicates": pipeline.duplicates,
        "delivered": pipeline.delivered,
        "contacts": len(contacts),
        "paths": pipeline.paths.destination_count,
        "decrypted": messenger.decrypted,
        "unknown": messenger.unknown,
        "undecryptable": messenger.undecryptable,
        "submissions": len(submissions),
    }


async def test_loading_public_changes_no_reception_count_and_decrypts_its_frames():
    without = await _replay_with_channels(public_loaded=False)
    with_public = await _replay_with_channels(public_loaded=True)

    for key in ("considered", "duplicates", "delivered", "contacts", "paths"):
        assert with_public[key] == without[key], f"loading Public changed {key}"
    assert with_public["decrypted"] == EXPECTED_PUBLIC_DECRYPTED
    assert with_public["unknown"] == EXPECTED_UNKNOWN_CHANNEL
    assert with_public["undecryptable"] == 0
    assert without["decrypted"] == 0
    assert without["unknown"] == EXPECTED_PUBLIC_DECRYPTED + EXPECTED_UNKNOWN_CHANNEL
    assert with_public["submissions"] == without["submissions"] == 0
