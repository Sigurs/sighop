"""The live packet feed (design D4, D5, `web-dashboard`).

This is the one place in milestone 8 where getting it wrong would let a browser
slow the radio, so the tests are mostly about what the feed may *not* do:

* it never awaits and never raises on the reception path;
* a browser that cannot keep up loses its own oldest records and is told how
  many, while the platform's handling of those records is untouched;
* a browser that stops reading entirely is eventually closed, and its event says
  what it managed to deliver and what it lost.

The rest is about honesty: the recorded history is painted first and the
boundary between it and the present is marked, an undecodable frame arrives with
its bytes and its reason rather than being dropped a second time, and a
suppressed transmission is in the feed because a gated run is not a silent one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sighop.db.packetlog import rx_row
from sighop.db.repositories import PacketLogRow
from sighop.net.bus import (
    IngressPipeline,
    NetworkBus,
    PriorityClass,
    Submission,
    TxOutcome,
    TxResult,
)
from sighop.net.dedup import DedupCache
from sighop.net.paths import PathStore
from sighop.net.rx import decode_event
from sighop.protocol.packet import PayloadType, RouteType
from sighop.radio.modem import EU868_NARROW, RxEvent, RxMeta, UnparsedEvent
from sighop.web.app import allowed_hosts, create_app
from sighop.web.feed import DEFAULT_CONNECTION_QUEUE, FeedHub
from sighop.web.serialize import HISTORY, LIVE, logged_packet, rx_record, tx_record
from tests.test_web_state import RecordingLogger
from tests.webfixtures import authenticator, signed_client, stub_state

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)
HOSTS = allowed_hosts("127.0.0.1", 8080)

# A real reception from the corpus. Fabricated bytes would decode as a
# structural failure, and a failed frame is deliberately never deduplicated —
# which would make the duplicate test below quietly test nothing.
FRAME = bytes.fromhex(
    "1e0054[redacted]8ecd77dfc078d4386ea8326f26e124e257869087027731a53dc31db"
    "9c0ecc0ced37f0a645a90e11bf324210277fe"
)


def _reception(raw: bytes = FRAME, *, at: dt.datetime = NOW):
    return decode_event(
        RxEvent(
            packet=raw,
            rx_meta=RxMeta(snr_db=12.5, rssi_dbm=-30),
            received_at=at,
        )
    )


def _unparsed(at: dt.datetime = NOW):
    return decode_event(UnparsedEvent(raw=b"\xff\xfe", reason="not a KISS frame", received_at=at))


def _submission(*, origin: str = "advert") -> Submission:
    return Submission(
        packet=b"\x00" * 40,
        priority=PriorityClass.ADVERT,
        entity_id="entity-1",
        entity_name="panel",
        deadline=NOW + dt.timedelta(seconds=5),
        origin=origin,
    )


def _outcome(result: TxResult = TxResult.TRANSMITTED) -> TxOutcome:
    return TxOutcome(
        result=result,
        packet_id="pkt-tx-1",
        airtime_ms=120.5,
        queue_wait_ms=3.25,
        attempts=1,
        reason="transmit disabled" if result is TxResult.SUPPRESSED else "",
    )


# --- 10.1 One subscription, many connections --------------------------------


async def test_the_bus_reports_one_web_subscriber_with_ten_connections_open() -> None:
    """10.1, design D4: an operator's second tab is not a change in the platform."""
    bus = NetworkBus(logger=RecordingLogger())
    hub = FeedHub(logger=RecordingLogger())
    hub.subscribe(bus)

    for _ in range(10):
        hub.connect(at=NOW)

    assert hub.open_connections == 10
    web = [stats for stats in bus.subscriber_stats if stats.name == "web-feed"]
    assert len(web) == 1, "the bus grew a subscriber per browser tab"

    hub.close()
    assert hub.open_connections == 0
    await bus.aclose()


def test_an_offer_reaches_every_open_connection() -> None:
    """10.1: fan-out, in the connection's own bounded queue."""
    hub = FeedHub()
    first, second = hub.connect(at=NOW), hub.connect(at=NOW)

    hub.on_reception(_reception(), False)

    assert len(first.records) == 1
    assert len(second.records) == 1
    assert first.records[0]["direction"] == "rx"


def test_the_offer_never_raises_when_a_connection_is_closed() -> None:
    """10.1: the contact sink's contract, on the path a browser attaches to."""
    hub = FeedHub()
    connection = hub.connect(at=NOW)
    connection.close()

    hub.on_reception(_reception(), False)

    assert not connection.records, "a closed connection took a record anyway"


def test_a_duplicate_reaches_the_feed_even_though_the_bus_never_sees_one() -> None:
    """10.1: the pipeline's observer, and the reason it exists.

    Duplicates are dropped before fan-out — that is right, a duplicate is a copy
    the platform has decided not to act on twice. A *display* is not an actor,
    and a feed that showed a busy mesh as quiet would disagree with the dedup
    counters on the same screen.
    """
    bus = NetworkBus(logger=RecordingLogger())
    hub = FeedHub()
    pipeline = IngressPipeline(
        bus=bus,
        dedup=DedupCache(),
        paths=PathStore(),
        logger=RecordingLogger(),
        radio=EU868_NARROW,
        observer=hub.on_reception,
    )
    connection = hub.connect(at=NOW)

    first = _reception()
    pipeline.ingest(first)
    pipeline.ingest(_reception(at=NOW + dt.timedelta(seconds=1)))

    rows = list(connection.records)
    assert len(rows) == 2, "the duplicate did not reach the feed"
    assert [row["duplicate"] for row in rows] == [False, True]
    assert pipeline.duplicates == 1


def test_an_observer_that_raises_does_not_reach_the_reception_path() -> None:
    """10.1: a broken watcher is a broken watcher, not a broken reception."""

    def explode(record: object, duplicate: bool) -> None:
        raise RuntimeError("the display is on fire")

    bus = NetworkBus(logger=RecordingLogger())
    logger = RecordingLogger()
    pipeline = IngressPipeline(
        bus=bus, dedup=DedupCache(), paths=PathStore(), logger=logger, observer=explode
    )

    assert pipeline.ingest(_reception()) is True
    assert pipeline.delivered == 1
    assert logger.named("rx_observer_error"), "the failure was swallowed silently"


# --- 10.2 A browser that cannot keep up -------------------------------------


def test_a_slow_connection_loses_its_oldest_records_and_counts_them() -> None:
    """10.2: the drop is this connection's, and it is counted."""
    hub = FeedHub(capacity=4)
    connection = hub.connect(at=NOW)

    for index in range(10):
        connection.offer({"packet_id": f"p{index}", "source": LIVE})

    assert len(connection.records) == 4
    assert [row["packet_id"] for row in connection.records] == ["p6", "p7", "p8", "p9"]
    assert connection.dropped == 6
    assert connection.incomplete is True


def test_the_drop_count_is_what_the_browser_is_told() -> None:
    """10.2: "feed incomplete, N dropped" rather than a quiet mesh."""
    hub = FeedHub(capacity=2)
    connection = hub.connect(at=NOW)
    for index in range(5):
        connection.offer({"packet_id": f"p{index}"})

    status = connection.status()
    assert status["kind"] == "status"
    assert status["dropped"] == 3
    assert status["incomplete"] is True


def test_one_slow_connection_does_not_affect_another() -> None:
    """10.2: a bounded queue *per* connection, so tabs are independent."""
    hub = FeedHub(capacity=2)
    slow, fast = hub.connect(at=NOW), hub.connect(at=NOW)

    for index in range(5):
        hub._fan_out({"packet_id": f"p{index}"})
        if index % 2 == 0:
            fast.records.clear()  # the fast one is draining as it goes
            fast.delivered += 1

    assert slow.dropped == 3
    assert fast.dropped == 0


def test_the_platforms_own_handling_of_a_dropped_record_is_unaffected() -> None:
    """10.2: the record was dropped *for a browser*, not by the platform."""
    bus = NetworkBus(logger=RecordingLogger())
    hub = FeedHub(capacity=1)
    pipeline = IngressPipeline(
        bus=bus,
        dedup=DedupCache(),
        paths=PathStore(),
        logger=RecordingLogger(),
        observer=hub.on_reception,
    )
    connection = hub.connect(at=NOW)

    delivered = 0
    for index in range(20):
        if pipeline.ingest(_reception(at=NOW + dt.timedelta(seconds=index))):
            delivered += 1

    assert connection.dropped > 0, "the connection never fell behind; nothing was tested"
    assert pipeline.delivered == delivered
    assert pipeline.delivered + pipeline.duplicates == 20


# --- 10.4 Serialisation -----------------------------------------------------


def test_a_reception_serialises_its_decoded_fields() -> None:
    """10.4, design D5: built from the typed record, not from a rendered line."""
    row = rx_record(_reception())

    assert row["direction"] == "rx"
    assert row["source"] == LIVE
    assert row["packet_id"]
    assert row["route_type"]
    assert row["payload_type"]
    assert row["size_bytes"] == len(FRAME)
    assert row["snr_db"] == 12.5
    assert row["rssi_dbm"] == -30
    assert row["duplicate"] is False
    assert row["at"].startswith("2026-09-06T12:00")


def test_an_unparsed_frame_carries_its_bytes_and_its_reason() -> None:
    """10.4, §4.1: a frame nobody could decode must not become invisible."""
    row = rx_record(_unparsed())

    assert row["outcome"] == "modem_unparsed"
    assert row["reason"] == "not a KISS frame"
    assert row["raw"] == "fffe", "the evidence was dropped on the way to the browser"


DISCOVER_RESP_FRAME = bytes([RouteType.DIRECT | (PayloadType.CONTROL << 2), 0x00]) + bytes.fromhex(
    "922f9a7d3916[redacted]"
)
"""DIRECT CONTROL, zero hops: a corpus node discovery response."""


def test_a_discovery_response_carries_its_outcome_and_summary() -> None:
    """decode-control-discovery: the detail column says what the frame is, and
    that the key it names is a claim."""
    row = rx_record(_reception(DISCOVER_RESP_FRAME))

    assert row["outcome"] == "discover_response"
    assert row["reason"] is None
    assert "tag=9a7d3916" in row["summary"]
    assert "(unauthenticated)" in row["summary"]


def test_a_recorded_discovery_response_keeps_its_outcome_but_no_summary() -> None:
    row = logged_packet(rx_row(_reception(DISCOVER_RESP_FRAME)))

    assert row["outcome"] == "discover_response"
    assert row["summary"] is None


def test_a_frame_without_a_summary_serialises_none() -> None:
    assert rx_record(_reception())["summary"] is None


def test_a_suppressed_transmission_is_in_the_feed_with_its_outcome() -> None:
    """10.4: a gated run resolves everything as suppressed and is not silent."""
    row = tx_record(_submission(), _outcome(TxResult.SUPPRESSED), at=NOW)

    assert row["direction"] == "tx"
    assert row["outcome"] == "suppressed"
    assert row["transmitted"] is False
    assert row["reason"] == "transmit disabled"
    assert row["priority"] == int(PriorityClass.ADVERT)
    assert row["airtime_ms"] == 120.5


def test_a_transmission_is_distinguishable_from_a_reception_at_a_glance() -> None:
    """10.4, `web-dashboard`: direction is a field, not something to infer."""
    received = rx_record(_reception())
    transmitted = tx_record(_submission(), _outcome(), at=NOW)

    assert received["direction"] != transmitted["direction"]
    assert transmitted["entity_name"] == "panel"
    assert transmitted["origin"] == "advert"


def test_a_recorded_packet_serialises_into_the_same_shape() -> None:
    """10.4 / 10.5: one list in the browser, with `source` saying which side."""
    row = logged_packet(
        PacketLogRow(
            packet_id="pkt-1",
            direction="rx",
            at=NOW,
            outcome="duplicate",
            route_type="FLOOD",
            payload_type="ADVERT",
            path_bytes=b"\xbe\xd0",
            hop_count=2,
            size_bytes=120,
            snr_db=12.0,
            rssi_dbm=-31,
        )
    )

    assert row["source"] == HISTORY
    assert row["duplicate"] is True
    assert row["path"] == "bed0"
    assert set(rx_record(_reception())) <= set(row) | {"hash_size", "src_hash"}, (
        "a history row and a live row must be the same shape in the browser"
    )


def test_every_serialised_value_is_json_safe() -> None:
    """10.4: nothing on this path may raise, including at encoding time."""
    import json

    for row in (
        rx_record(_reception()),
        rx_record(_unparsed()),
        tx_record(_submission(), _outcome(), at=NOW),
    ):
        json.loads(json.dumps(row))


# --- 10.3 / 10.5 The socket, and what it sends first ------------------------


def _open_feed(client: TestClient):
    """The panel's own socket, with the host the guard was configured for.

    `TestClient.websocket_connect` sends `Host: testserver` regardless of the
    base URL, and the rebinding guard refuses a host it was not configured to
    serve — which is exactly what it is for (design D9). A browser also sends
    `Origin` on every handshake, and milestone 9's guard refuses one that does
    not name a served host.
    """
    return client.websocket_connect(
        "/feed", headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"}
    )


def _app(state: object | None = None, feed: FeedHub | None = None) -> tuple[FastAPI, FeedHub]:
    hub = feed or FeedHub()
    panel = state or stub_state()
    return (
        create_app(panel, auth=authenticator(), feed=hub, hosts=HOSTS, logger=RecordingLogger()),  # type: ignore[arg-type]
        hub,
    )


def test_the_socket_paints_history_then_marks_the_boundary_then_streams() -> None:
    """10.3 / 10.5: the record sequence, driven with no browser present."""
    app, hub = _app()

    with (
        signed_client(app, base_url="http://127.0.0.1:8080") as client,
        _open_feed(client) as socket,
    ):
        history = socket.receive_json()
        boundary = socket.receive_json()
        status = socket.receive_json()

        assert history["kind"] == "history"
        assert boundary["kind"] == "boundary"
        assert status["kind"] == "status"

        hub.on_reception(_reception(), False)
        live = socket.receive_json()
        assert live["kind"] == "records"
        assert live["records"][0]["source"] == LIVE


def test_with_no_readable_history_the_feed_starts_empty_and_says_why() -> None:
    """10.5, `web-dashboard`: not a mesh that has been quiet — no history at all."""
    app, _hub = _app()

    with (
        signed_client(app, base_url="http://127.0.0.1:8080") as client,
        _open_feed(client) as socket,
    ):
        history = socket.receive_json()

    assert history["records"] == []
    assert "could not be read" in history["note"]
    assert "only records from now on" in history["note"]


def test_a_transmission_reaches_a_connected_browser() -> None:
    """10.3: both directions, over the socket the panel actually opens."""
    app, hub = _app()

    with (
        signed_client(app, base_url="http://127.0.0.1:8080") as client,
        _open_feed(client) as socket,
    ):
        socket.receive_json()  # history
        socket.receive_json()  # boundary
        socket.receive_json()  # status

        hub.on_transmission(_submission(), _outcome(), at=NOW)
        message = socket.receive_json()

    assert message["records"][0]["direction"] == "tx"
    assert message["records"][0]["outcome"] == "transmitted"


def test_the_drop_count_reaches_the_browser() -> None:
    """10.2 / 10.3: the incomplete state is shown, not swallowed."""
    hub = FeedHub(capacity=2)
    app, _hub = _app(feed=hub)

    with (
        signed_client(app, base_url="http://127.0.0.1:8080") as client,
        _open_feed(client) as socket,
    ):
        socket.receive_json()  # history
        socket.receive_json()  # boundary
        first = socket.receive_json()
        assert first["incomplete"] is False

        connection = hub.connections[0]
        for index in range(6):
            connection.offer({"packet_id": f"p{index}"})

        records = socket.receive_json()
        assert records["kind"] == "records"
        status = socket.receive_json()

    assert status["kind"] == "status"
    assert status["incomplete"] is True
    assert status["dropped"] == 4


# --- 10.6 A connection that stops reading -----------------------------------


def test_a_closed_connection_emits_one_event_with_its_counts() -> None:
    """10.6 / 8.4, §9: one event per closed connection, and what it carried."""
    logger = RecordingLogger()
    hub = FeedHub()
    state = stub_state()
    app = create_app(state, auth=authenticator(), feed=hub, hosts=HOSTS, logger=logger)

    with (
        signed_client(app, base_url="http://127.0.0.1:8080") as client,
        _open_feed(client) as socket,
    ):
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        hub.on_reception(_reception(), False)
        socket.receive_json()

    events = logger.named("web_feed_closed")
    assert len(events) == 1, "a closed connection did not emit exactly one event"
    delivered = events[0]["delivered"]
    assert isinstance(delivered, int) and delivered >= 1
    assert events[0]["dropped"] == 0
    assert events[0]["incomplete"] is False
    assert isinstance(events[0]["duration_ms"], float)


def test_a_stalled_connection_changes_no_platform_count() -> None:
    """10.6: the assertion the whole design of the hub exists for.

    The connection is never drained at all — the shape a browser that has
    stopped reading has — and the pipeline's own numbers are compared against a
    run with no feed attached.
    """

    def counts(*, watched: bool) -> dict[str, int]:
        bus = NetworkBus(logger=RecordingLogger())
        hub = FeedHub(capacity=2)
        pipeline = IngressPipeline(
            bus=bus,
            dedup=DedupCache(),
            paths=PathStore(),
            logger=RecordingLogger(),
            observer=hub.on_reception if watched else None,
        )
        if watched:
            hub.connect(at=NOW)  # opened, never drained
        for index in range(30):
            pipeline.ingest(_reception(at=NOW + dt.timedelta(seconds=index)))
        return {
            "delivered": pipeline.delivered,
            "duplicates": pipeline.duplicates,
            "paths": pipeline.paths.destination_count,
            "considered": pipeline.dedup.stats.considered,
        }

    assert counts(watched=True) == counts(watched=False)


async def test_a_send_to_a_browser_that_will_not_read_is_bounded() -> None:
    """10.6: a browser cannot hold a task open for the life of the process.

    `_send` is the only place the feed writes to a socket, so the bound belongs
    there and is asserted there. A test that starved a real kernel buffer would
    take ten seconds to prove the same thing less clearly.
    """
    from sighop.web.app import FEED_SEND_TIMEOUT_SECONDS, _send

    assert FEED_SEND_TIMEOUT_SECONDS > 0, "there is no bound on the send at all"

    class NeverReads:
        sent = 0

        async def send_json(self, message: dict[str, object]) -> None:
            await asyncio.Event().wait()

    socket = NeverReads()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            _send(socket, {"kind": "records"}),  # type: ignore[arg-type]
            timeout=0.05,
        )
    assert socket.sent == 0


def test_the_connection_queue_default_is_recorded_as_a_guess() -> None:
    """10.2: the design's first open question, marked in the code that answers it.

    The bound is a number nobody has measured yet — it is answerable only from a
    live session against real advert volume — and the live exercise is where it
    gets settled. Saying so beside the constant is what stops it from being read
    later as a considered figure.
    """
    from sighop.web import feed as feed_module

    assert DEFAULT_CONNECTION_QUEUE > 0
    source = Path(feed_module.__file__).read_text()
    assert "never awaits and never raises" in source
    assert "guess until a session runs" in source
    assert "open question" in source
