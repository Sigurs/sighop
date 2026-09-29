"""Group channels, both directions (change `channel-messaging`, `channel-messaging` spec).

The receive tests that matter most use a corpus frame a stock MeshCore node
encrypted on the Public channel, so at least one of them is not sighop checking
sighop. The post tests are a loopback, and prove that composition and the repeat
registry compose — `test_channel_foreign_decrypt.py` is what proves the wire.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from sighop.net.bus import IngressPipeline, NetworkBus, PriorityClass, TxResult
from sighop.net.channels import (
    MAX_CHANNEL_TEXT_LEN,
    MAX_PATHS,
    ChannelKind,
    ChannelMessageReceived,
    ChannelMessageRecord,
    ChannelMessenger,
    ChannelNotLoadedError,
    ChannelOutcome,
    ChannelPostRefused,
    ChannelPostResolved,
    ChannelPostSubmitted,
    ChannelRepeatHeard,
    ChannelSet,
    ChannelSetChanged,
    ChannelTextTooLongError,
    ChannelUndecryptable,
    ChannelUnknown,
    ChannelUnsupportedText,
    HeardRegistry,
    IdentityNotLoadedError,
    LoadedChannel,
    RepeatRegistry,
    SenderNameSeparatorError,
    TransmitDisabledError,
    build_channel_packet,
)
from sighop.net.dedup import DedupCache
from sighop.net.rx import RxRecord, decode_event
from sighop.protocol.crypto import (
    PUBLIC_CHANNEL_KEY,
    ChannelKey,
    channel_key_from_hashtag,
    mac_then_decrypt,
)
from sighop.protocol.packet import PAYLOAD_VERSION_1, Packet, PacketHeader, PayloadType, RouteType
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    GroupEnvelope,
    TextMessageBody,
    TextType,
    WireText,
    build_group_envelope,
    build_text_message_body,
    parse_group_text_body,
    parse_payload,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import RxEvent, RxMeta
from tests.protocol.corpus import CHANNELS, _load_file
from tests.test_dm import START, Entity, RecordingSubmit, TickingClock
from tests.test_tx import RecordingLogger

PUBLIC = LoadedChannel(id=1, name="Public", kind=ChannelKind.PUBLIC, key=PUBLIC_CHANNEL_KEY)
HASHTAG = LoadedChannel(
    id=2, name="#dev-sighop", kind=ChannelKind.HASHTAG, key=channel_key_from_hashtag("#dev-sighop")
)


class Sink:
    def __init__(self, *, accept: bool = True) -> None:
        self.records: list[ChannelMessageRecord] = []
        self.accept = accept

    def offer(self, record: ChannelMessageRecord) -> bool:
        self.records.append(record)
        return self.accept


def colliding_key(channel: LoadedChannel) -> ChannelKey:
    """A pre-shared key, different from `channel`'s, under the same one-byte hash."""
    for seed in range(100_000):
        key = ChannelKey(key=seed.to_bytes(16, "little"))
        if key.channel_hash == channel.channel_hash and key.key != channel.key.key:
            return key
    raise AssertionError("no colliding key found")  # pragma: no cover


def corpus_public_frame() -> bytes:
    for frame in _load_file(CHANNELS):
        packet = decode_packet(frame.raw)
        if isinstance(packet, DecodeFailure):
            continue
        if packet.header.payload_type is PayloadType.GRP_TXT and packet.payload[0] == 0x11:
            return frame.raw
    raise AssertionError("the corpus holds no Public frame")  # pragma: no cover


def reception(raw: bytes, *, at: dt.datetime = START, packet_id: str | None = None) -> RxRecord:
    return decode_event(
        RxEvent(packet=raw, rx_meta=RxMeta(snr_db=7.5, rssi_dbm=-60), received_at=at),
        received_at=at,
        packet_id=packet_id,
    )


def group_frame(
    channel: LoadedChannel,
    text: bytes,
    *,
    txt_type: TextType = TextType.PLAIN,
    payload_type: PayloadType = PayloadType.GRP_TXT,
    hop_count: int = 0,
) -> bytes:
    from sighop.protocol.crypto import encrypt_then_mac

    plaintext = build_text_message_body(
        TextMessageBody(
            timestamp=1_757_000_000,
            txt_type=txt_type,
            attempt=0,
            text=WireText.from_bytes(text),
            sender_key_prefix=b"\x01\x02\x03\x04" if txt_type is TextType.SIGNED_PLAIN else None,
        )
    )
    mac, ciphertext = encrypt_then_mac(channel.key.secret, plaintext)
    payload = build_group_envelope(
        GroupEnvelope(
            payload_type=payload_type,
            channel_hash=channel.channel_hash,
            mac=mac,
            ciphertext=ciphertext,
        )
    )
    return encode_packet(
        Packet(
            header=PacketHeader(
                route_type=RouteType.FLOOD,
                payload_type=payload_type,
                payload_version=PAYLOAD_VERSION_1,
            ),
            transport_codes=None,
            hop_count=hop_count,
            hash_size=1,
            path=bytes(range(hop_count)),
            payload=payload,
        )
    )


def messenger(
    *channels: LoadedChannel,
    entities: tuple[Entity, ...] = (),
    submit: RecordingSubmit | None = None,
    gate: bool = True,
    clock: TickingClock | None = None,
    **kwargs: object,
) -> tuple[ChannelMessenger, Sink, list[object], RecordingSubmit]:
    sink = Sink()
    events: list[object] = []
    submit = submit or RecordingSubmit()
    m = ChannelMessenger(
        submit=submit,
        entities=list(entities),
        transmit_enabled=lambda: gate,
        channels=ChannelSet(channels=channels),
        clock=clock or TickingClock(),
        on_event=events.append,
        logger=RecordingLogger(),
        records=sink,
        **kwargs,  # type: ignore[arg-type]
    )
    return m, sink, events, submit


# --- 4.1 The channel set ----------------------------------------------------


def test_by_hash_returns_every_channel_sharing_a_hash_in_load_order() -> None:
    twin = LoadedChannel(id=3, name="twin", kind=ChannelKind.PSK, key=colliding_key(PUBLIC))
    channels = ChannelSet(channels=(PUBLIC, HASHTAG, twin))

    assert channels.by_hash(0x11) == (PUBLIC, twin)
    assert channels.by_hash(HASHTAG.channel_hash) == (HASHTAG,)
    assert channels.by_id(2) is HASHTAG
    assert channels.by_id(99) is None
    assert len(channels) == 3


def test_adopting_a_different_set_reports_what_changed() -> None:
    """The CLI promises a running run applies a change within 60 s; the run says
    so when it does, or the promise can only be checked from a second surface.
    """
    m, _, events, _ = messenger(PUBLIC)

    m.replace_channels(ChannelSet(channels=(PUBLIC, HASHTAG)))
    m.replace_channels(ChannelSet(channels=(PUBLIC, HASHTAG)))  # same set, silent
    m.replace_channels(ChannelSet(channels=(HASHTAG,)))

    changes = [e for e in events if isinstance(e, ChannelSetChanged)]
    assert [(c.added, c.removed, c.loaded) for c in changes] == [
        ((HASHTAG.name,), (), 2),
        ((), (PUBLIC.name,), 1),
    ]


def test_the_first_set_a_run_loads_is_not_reported_as_a_change() -> None:
    """A run's startup report already names the channels it loaded."""
    events: list[object] = []
    m = ChannelMessenger(
        submit=RecordingSubmit(),
        on_event=events.append,
        logger=RecordingLogger(),
    )

    m.replace_channels(ChannelSet(channels=(PUBLIC,)))
    assert [e for e in events if isinstance(e, ChannelSetChanged)] == []

    m.replace_channels(ChannelSet(channels=(PUBLIC, HASHTAG)))
    assert len([e for e in events if isinstance(e, ChannelSetChanged)]) == 1


def test_guessable_marks_public_and_hashtag_but_not_psk() -> None:
    assert PUBLIC.guessable and HASHTAG.guessable
    assert not LoadedChannel(4, "private", ChannelKind.PSK, ChannelKey(bytes(16))).guessable


def test_a_loaded_channel_never_prints_its_key() -> None:
    private = LoadedChannel(4, "private", ChannelKind.PSK, ChannelKey(b"\xab" * 16))
    assert "ab" * 4 not in repr(private)


# --- 4.2 Receiving ----------------------------------------------------------


async def test_a_corpus_public_frame_decrypts_under_public() -> None:
    m, sink, events, _ = messenger(PUBLIC)

    await m.handle(reception(corpus_public_frame(), packet_id="corpus"))

    received = [e for e in events if isinstance(e, ChannelMessageReceived)]
    assert len(received) == 1
    assert received[0].channel_name == "Public"
    assert received[0].unverified_sender_name
    assert m.decrypted == 1
    [record] = sink.records
    assert record.inbound and record.outcome is ChannelOutcome.RECEIVED
    assert record.ref == "corpus" and record.channel_id == 1
    assert record.unverified_sender_name == received[0].unverified_sender_name
    assert record.text.decode() == received[0].body
    assert record.snr_db == 7.5 and record.rssi_dbm == -60


async def test_the_second_channel_under_a_shared_hash_wins_when_its_mac_does() -> None:
    twin = LoadedChannel(id=3, name="twin", kind=ChannelKind.PSK, key=colliding_key(PUBLIC))
    m, sink, events, _ = messenger(PUBLIC, twin)

    await m.handle(reception(group_frame(twin, b"alice: hi")))

    [event] = events
    assert isinstance(event, ChannelMessageReceived)
    assert event.channel_name == "twin" and event.channels_tried == 2
    assert [record.channel_id for record in sink.records] == [3]


async def test_an_unknown_hash_is_counted_and_not_recorded() -> None:
    m, sink, events, _ = messenger(PUBLIC)

    await m.handle(reception(group_frame(HASHTAG, b"bob: hello")))

    [event] = events
    assert isinstance(event, ChannelUnknown) and event.channel_hash == HASHTAG.channel_hash
    assert m.unknown == 1 and not sink.records


async def test_a_matching_hash_with_no_matching_mac_is_undecryptable() -> None:
    twin = LoadedChannel(id=3, name="twin", kind=ChannelKind.PSK, key=colliding_key(PUBLIC))
    m, sink, events, _ = messenger(PUBLIC)

    await m.handle(reception(group_frame(twin, b"carol: secret")))

    [event] = events
    assert isinstance(event, ChannelUndecryptable)
    assert event.channel_hash == 0x11 and event.channels_tried == 1
    assert m.undecryptable == 1 and not sink.records


async def test_a_non_plain_text_type_is_counted_and_not_recorded() -> None:
    m, sink, events, _ = messenger(HASHTAG)

    await m.handle(reception(group_frame(HASHTAG, b"dave: ping", txt_type=TextType.CLI_DATA)))

    [event] = events
    assert isinstance(event, ChannelUnsupportedText)
    assert event.txt_type == int(TextType.CLI_DATA)
    assert m.unsupported == 1 and m.decrypted == 0 and not sink.records


async def test_group_data_is_never_decrypted() -> None:
    m, sink, events, _ = messenger(HASHTAG)

    await m.handle(reception(group_frame(HASHTAG, b"x", payload_type=PayloadType.GRP_DATA)))

    assert not events and not sink.records
    assert (m.decrypted, m.unknown, m.undecryptable) == (0, 0, 0)


async def test_a_message_with_no_separator_has_no_sender() -> None:
    m, sink, _events, _ = messenger(HASHTAG)

    await m.handle(reception(group_frame(HASHTAG, b"just words")))

    [record] = sink.records
    assert record.unverified_sender_name is None and record.text == b"just words"


# --- 4.3 Posting ------------------------------------------------------------


async def test_a_post_decrypts_under_the_channel_key_as_the_identity() -> None:
    entity = Entity("dev-companion")
    m, sink, events, submit = messenger(HASHTAG, entities=(entity,))

    post = m.post(2, entity, "hello", actor="dev-operator")
    resolved = await post.resolution

    [submission] = submit.submissions
    assert submission.priority is PriorityClass.MESSAGE
    packet = decode_packet(submission.packet)
    assert not isinstance(packet, DecodeFailure)
    assert packet.header.route_type is RouteType.FLOOD
    envelope = parse_payload(PayloadType.GRP_TXT, packet.payload)
    assert isinstance(envelope, GroupEnvelope) and envelope.channel_hash == HASHTAG.channel_hash
    match, plaintext = mac_then_decrypt(HASHTAG.key.secret, envelope.mac, envelope.ciphertext)
    assert match.matched and plaintext is not None
    body = parse_group_text_body(plaintext)
    assert not isinstance(body, DecodeFailure)
    assert (body.unverified_sender_name, body.body) == ("dev-companion", "hello")
    assert body.message.txt_type is TextType.PLAIN and body.message.attempt == 0

    assert [r.outcome for r in sink.records] == [
        ChannelOutcome.AWAITING,
        ChannelOutcome.TRANSMITTED,
    ]
    assert {r.ref for r in sink.records} == {post.post_id}
    assert sink.records[0].entity_public_key == entity.identity.public_key
    assert resolved.outcome is ChannelOutcome.TRANSMITTED
    submitted = next(e for e in events if isinstance(e, ChannelPostSubmitted))
    assert submitted.actor == "dev-operator" and submitted.entity_name == "dev-companion"
    assert any(isinstance(e, ChannelPostResolved) for e in events)


async def test_two_identical_posts_in_one_second_carry_different_timestamps() -> None:
    entity = Entity("dev-companion")
    m, _sink, _events, _submit = messenger(HASHTAG, entities=(entity,))

    first = m.post(2, entity, "same")
    second = m.post(2, entity, "same")
    await asyncio.gather(first.resolution, second.resolution)

    assert second.record.wire_timestamp == first.record.wire_timestamp + 1


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("channel", ChannelNotLoadedError),
        ("identity", IdentityNotLoadedError),
        ("separator", SenderNameSeparatorError),
        ("length", ChannelTextTooLongError),
        ("gate", TransmitDisabledError),
    ],
)
async def test_every_refusal_records_nothing_and_submits_nothing(case: str, error: type) -> None:
    loaded = Entity("dev-companion")
    split = Entity("a: b")
    entities = (loaded, split)
    m, sink, events, submit = messenger(HASHTAG, entities=entities, gate=case != "gate")
    stranger = Entity("stranger")
    arguments = {
        "channel": (99, loaded, "hi"),
        "identity": (2, stranger, "hi"),
        "separator": (2, split, "hi"),
        "length": (2, loaded, "x" * 150),
        "gate": (2, loaded, "hi"),
    }[case]

    with pytest.raises(error):
        m.post(*arguments)

    assert not submit.submissions and not sink.records
    assert [type(e) for e in events] == [ChannelPostRefused]
    assert m.posts_refused == 1


def test_the_length_refusal_states_the_limit_the_name_and_the_overage() -> None:
    entity = Entity("dev-companion")
    m, _sink, _events, _submit = messenger(HASHTAG, entities=(entity,))

    with pytest.raises(ChannelTextTooLongError) as refused:
        m.post(2, entity, "x" * 150)

    message = str(refused.value)
    assert f"{MAX_CHANNEL_TEXT_LEN} bytes" in message
    assert "name" in message and "separator" in message
    assert "5 over" in message  # 15 + 150 = 165


def test_exactly_160_bytes_is_accepted() -> None:
    entity = Entity("dev-companion")
    m, _sink, _events, submit = messenger(HASHTAG, entities=(entity,))

    async def go() -> None:
        post = m.post(2, entity, "x" * 145)
        await post.resolution

    asyncio.run(go())
    assert len(submit.submissions) == 1


async def test_a_budget_drop_resolves_not_transmitted_with_the_reason_and_no_retry() -> None:
    entity = Entity("dev-companion")
    submit = RecordingSubmit(TxResult.DROPPED)
    m, sink, _events, _ = messenger(HASHTAG, entities=(entity,), submit=submit)

    resolved = await m.post(2, entity, "hello").resolution

    assert resolved.outcome is ChannelOutcome.NOT_TRANSMITTED
    assert resolved.outcome_reason == "deadline_expired"
    assert len(submit.submissions) == 1
    assert sink.records[-1].outcome is ChannelOutcome.NOT_TRANSMITTED
    assert len(m.registry) == 0


# --- 4.4 The repeat registry ------------------------------------------------


def echo_of(packet: bytes, *, hops: int) -> bytes:
    """The same payload as a repeater would forward it: a longer flood path,
    appended at the width the origin chose (`Mesh.cpp:349`)."""
    decoded = decode_packet(packet)
    assert not isinstance(decoded, DecodeFailure)
    return encode_packet(
        Packet(
            header=decoded.header,
            transport_codes=None,
            hop_count=hops,
            hash_size=decoded.hash_size,
            path=bytes(range(hops * decoded.hash_size)),
            payload=decoded.payload,
        )
    )


@pytest.mark.parametrize("size", [1, 2, 3])
async def test_a_post_floods_at_the_configured_path_hash_size(size: int) -> None:
    entity = Entity("dev-companion")
    m, _sink, _events, submit = messenger(HASHTAG, entities=(entity,), path_hash_size=size)

    await m.post(2, entity, "hello").resolution

    decoded = decode_packet(submit.submissions[0].packet)
    assert not isinstance(decoded, DecodeFailure)
    assert decoded.route_type is RouteType.FLOOD
    assert decoded.payload_type is PayloadType.GRP_TXT
    assert decoded.hash_size == size
    assert decoded.hop_count == 0
    assert decoded.path == b""


@pytest.mark.parametrize("size", [1, 3])
async def test_three_copies_of_our_post_count_three_repeats_and_record_nothing_inbound(
    size: int,
) -> None:
    entity = Entity("dev-companion")
    m, sink, events, submit = messenger(HASHTAG, entities=(entity,), path_hash_size=size)
    post = m.post(2, entity, "hello")
    await post.resolution

    bus = NetworkBus(logger=RecordingLogger())
    subscription = bus.subscribe("channels")
    pipeline = IngressPipeline(bus=bus, dedup=DedupCache(), logger=RecordingLogger())
    pipeline.observers.append(m.observe)
    raw = submit.submissions[0].packet
    for index, hops in enumerate((1, 2, 2)):
        pipeline.ingest(
            reception(echo_of(raw, hops=hops), at=START + dt.timedelta(seconds=index + 1))
        )
    assert pipeline.duplicates == 2
    while not subscription.queue.empty():
        await m.handle(subscription.queue.get_nowait())

    heard = [e for e in events if isinstance(e, ChannelRepeatHeard)]
    assert [e.repeats_heard for e in heard] == [1, 2, 3]
    assert [e.duplicate for e in heard] == [False, True, True]
    assert m.repeats_heard == 3
    assert not [r for r in sink.records if r.inbound]
    assert sink.records[-1].ref == post.post_id and sink.records[-1].repeats_heard == 3
    assert m.decrypted == 0


async def test_a_message_claiming_a_local_name_that_we_did_not_post_is_received() -> None:
    entity = Entity("dev-companion")
    m, sink, events, _ = messenger(HASHTAG, entities=(entity,))

    await m.handle(reception(group_frame(HASHTAG, b"dev-companion: not us")))

    [record] = sink.records
    assert record.inbound and record.unverified_sender_name == "dev-companion"
    assert not any(isinstance(e, ChannelRepeatHeard) for e in events)


def _record(ref: str) -> ChannelMessageRecord:
    return ChannelMessageRecord(
        channel_id=1,
        direction="out",
        ref=ref,
        text=b"",
        wire_timestamp=0,
        handled_at=START,
        outcome=ChannelOutcome.TRANSMITTED,
    )


def test_the_registry_evicts_past_capacity_and_age() -> None:
    registry = RepeatRegistry(capacity=2, ttl_seconds=3600)
    registry.remember(b"a", _record("a"), "c", START)
    registry.remember(b"b", _record("b"), "c", START + dt.timedelta(seconds=1))
    registry.remember(b"c", _record("c"), "c", START + dt.timedelta(seconds=2))

    assert registry.get(b"a", START + dt.timedelta(seconds=3)) is None
    assert registry.get(b"b", START + dt.timedelta(seconds=3)) is not None

    late = START + dt.timedelta(seconds=3602)
    assert registry.get(b"b", late) is None
    assert registry.get(b"c", late) is not None
    assert len(registry) == 1


# --- Every copy's path (change `channel-message-path-copy`) -------------------


def routed(frame: bytes, path: bytes, *, hash_size: int) -> bytes:
    """`frame`'s payload as it would arrive over `path`, hashes `hash_size` wide."""
    decoded = decode_packet(frame)
    assert not isinstance(decoded, DecodeFailure)
    return encode_packet(
        Packet(
            header=decoded.header,
            transport_codes=None,
            hop_count=len(path) // hash_size,
            hash_size=hash_size,
            path=path,
            payload=decoded.payload,
        )
    )


def ingress(m: ChannelMessenger) -> tuple[IngressPipeline, asyncio.Queue[RxRecord]]:
    bus = NetworkBus(logger=RecordingLogger())
    subscription = bus.subscribe("channels")
    pipeline = IngressPipeline(bus=bus, dedup=DedupCache(), logger=RecordingLogger())
    pipeline.observers.append(m.observe)
    return pipeline, subscription.queue


async def drain(m: ChannelMessenger, queue: asyncio.Queue[RxRecord]) -> None:
    while not queue.empty():
        await m.handle(queue.get_nowait())


A3F1_28C0_9E4B = bytes.fromhex("a3f128c09e4b")


async def test_a_received_message_records_its_path_and_hash_size() -> None:
    m, sink, _events, _ = messenger(HASHTAG)

    await m.handle(reception(routed(group_frame(HASHTAG, b"al: hi"), A3F1_28C0_9E4B, hash_size=2)))

    [record] = sink.records
    assert record.paths == (A3F1_28C0_9E4B,)
    assert record.path_hash_size == 2 and record.hop_count == 3


async def test_a_post_records_no_paths() -> None:
    entity = Entity("dev-companion")
    m, sink, _events, _ = messenger(HASHTAG, entities=(entity,))

    await m.post(2, entity, "hello").resolution

    assert sink.records and all(r.paths is None for r in sink.records)
    assert all(r.path_hash_size is None for r in sink.records)


async def test_duplicates_add_their_paths_in_arrival_order_to_the_same_record() -> None:
    m, sink, _events, _ = messenger(HASHTAG)
    pipeline, queue = ingress(m)
    frame = group_frame(HASHTAG, b"al: hi")

    pipeline.ingest(reception(routed(frame, A3F1_28C0_9E4B, hash_size=2), packet_id="first"))
    await drain(m, queue)
    pipeline.ingest(reception(routed(frame, b"", hash_size=2), packet_id="second"))
    pipeline.ingest(reception(routed(frame, bytes.fromhex("a3f15d02"), hash_size=2)))

    assert pipeline.duplicates == 2
    assert {r.ref for r in sink.records} == {"first"}
    last = sink.records[-1]
    assert last.paths == (A3F1_28C0_9E4B, b"", bytes.fromhex("a3f15d02"))
    assert last.hop_count == 3 and last.snr_db == 7.5 and last.rssi_dbm == -60


async def test_a_duplicate_heard_before_the_message_is_decrypted_is_not_lost() -> None:
    """The observer is told synchronously; the bus handler runs later."""
    m, sink, _events, _ = messenger(HASHTAG)
    pipeline, queue = ingress(m)
    frame = group_frame(HASHTAG, b"al: hi")

    pipeline.ingest(reception(routed(frame, b"\x0a", hash_size=1), packet_id="first"))
    pipeline.ingest(reception(routed(frame, b"\x0a\xff", hash_size=1)))
    assert not sink.records
    await drain(m, queue)

    [record] = sink.records
    assert record.ref == "first" and record.paths == (b"\x0a", b"\x0a\xff")


async def test_copies_beyond_the_bound_are_ignored() -> None:
    m, sink, _events, _ = messenger(HASHTAG)
    pipeline, queue = ingress(m)
    frame = group_frame(HASHTAG, b"al: hi")

    pipeline.ingest(reception(routed(frame, b"\x00", hash_size=1)))
    await drain(m, queue)
    for hop in range(1, MAX_PATHS + 5):
        pipeline.ingest(reception(routed(frame, bytes([hop, hop]), hash_size=1)))

    assert len(sink.records[-1].paths or ()) == MAX_PATHS
    assert len(sink.records) == MAX_PATHS  # the first record, then one per attached copy


async def test_a_duplicate_after_the_message_is_forgotten_is_not_attached() -> None:
    clock = TickingClock()
    m, sink, _events, _ = messenger(HASHTAG, clock=clock)
    pipeline, queue = ingress(m)
    frame = group_frame(HASHTAG, b"al: hi")

    pipeline.ingest(reception(routed(frame, b"\x01", hash_size=1)))
    await drain(m, queue)
    clock.advance(3601)
    pipeline.ingest(reception(routed(frame, b"\x02", hash_size=1)))

    assert pipeline.duplicates == 1
    assert [r.paths for r in sink.records] == [(b"\x01",)]


async def test_our_own_posts_heard_back_gain_no_paths() -> None:
    entity = Entity("dev-companion")
    m, sink, _events, submit = messenger(HASHTAG, entities=(entity,))
    await m.post(2, entity, "hello").resolution
    pipeline, queue = ingress(m)

    raw = submit.submissions[0].packet
    pipeline.ingest(reception(echo_of(raw, hops=1)))
    pipeline.ingest(reception(echo_of(raw, hops=2)))
    await drain(m, queue)

    assert m.repeats_heard == 2 and len(m.heard) == 0
    assert all(r.paths is None for r in sink.records)


def test_the_heard_registry_evicts_past_capacity_and_age() -> None:
    registry = HeardRegistry(capacity=2, ttl_seconds=3600)
    registry.first_copy(b"a", b"", START)
    registry.first_copy(b"b", b"", START + dt.timedelta(seconds=1))
    registry.first_copy(b"c", b"", START + dt.timedelta(seconds=2))
    assert registry.first_copy(b"c", b"\x09", START).paths == [b""]  # an existing entry stands

    assert registry.get(b"a", START + dt.timedelta(seconds=3)) is None
    late = START + dt.timedelta(seconds=3602)
    assert registry.get(b"b", late) is None
    assert registry.get(b"c", late) is not None


def test_a_raising_sink_does_not_stop_the_next() -> None:
    class Broken:
        def offer(self, record: ChannelMessageRecord) -> bool:
            raise RuntimeError("broken")

    m, sink, _events, _ = messenger(HASHTAG)
    m._records.insert(0, Broken())

    asyncio.run(m.handle(reception(group_frame(HASHTAG, b"eve: hi"))))

    assert len(sink.records) == 1 and m.records_refused == 1


def test_replacing_the_set_changes_what_is_decrypted() -> None:
    m, sink, _events, _ = messenger()
    frame = group_frame(HASHTAG, b"frank: hi")

    asyncio.run(m.handle(reception(frame)))
    m.replace_channels(ChannelSet(channels=(HASHTAG,)))
    asyncio.run(m.handle(reception(frame)))

    assert m.unknown == 1 and m.decrypted == 1 and len(sink.records) == 1


def test_build_channel_packet_returns_the_payload_dedup_keys_on() -> None:
    packet, payload = build_channel_packet(HASHTAG, b"\x00" * 5 + b"x: y", path_hash_size=3)
    decoded = decode_packet(packet)
    assert not isinstance(decoded, DecodeFailure) and decoded.payload == payload


async def test_a_post_after_a_rename_carries_the_new_sender_name() -> None:
    """An identity's name is on the air here too, not only in its adverts:
    it is the sender name of every channel post (change web-delete-and-rename).

    The messenger was handed this very object at construction, which is what
    makes one in-place rename reach the post path with nothing re-wired.
    """
    entity = Entity("dev-companion")
    m, _sink, _events, submit = messenger(HASHTAG, entities=(entity,))

    entity.name = "dev-companion-2"
    await m.post(2, entity, "hello").resolution

    [submission] = submit.submissions
    packet = decode_packet(submission.packet)
    assert not isinstance(packet, DecodeFailure)
    envelope = parse_payload(PayloadType.GRP_TXT, packet.payload)
    assert isinstance(envelope, GroupEnvelope)
    _match, plaintext = mac_then_decrypt(HASHTAG.key.secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    body = parse_group_text_body(plaintext)
    assert not isinstance(body, DecodeFailure)
    assert body.unverified_sender_name == "dev-companion-2"


def test_the_post_path_reads_the_sender_name_with_no_await_between_the_two_reads() -> None:
    """`check_sender_name(entity.name)` and `build_group_text_body(..., entity.name, ...)`
    both read a name a rename can change in place, so a suspension between them
    would let one post validate one name and send another (design, Risks)."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(ChannelMessenger.post))
    tree = ast.parse(source)
    checked: int | None = None
    built: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "check_sender_name":
                checked = node.lineno
            elif node.func.id == "build_group_text_body":
                built = node.lineno
    assert checked is not None and built is not None and checked < built
    suspensions = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and checked < node.lineno < built
    ]
    assert suspensions == [], (
        "an await was added between the two reads of entity.name; read the name "
        f"into a local first (lines {suspensions})"
    )
