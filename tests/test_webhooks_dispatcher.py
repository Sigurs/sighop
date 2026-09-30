"""Delivery off the reception path (webhook-notifications tasks 5.2, 5.3).

A fake repository, transport and sleep: the scenarios are about which webhook
gets which attempt when, and a real clock or network would only make that slower
and flakier to say.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from sighop.db.engine import DatabaseError, Failed, Outcome, Succeeded
from sighop.db.repositories import OpenedWebhook, WebhookRecord
from sighop.geo import Place
from sighop.geo.places import Located
from sighop.net.contacts import Contact, ContactObservation, ContactStore
from sighop.protocol.crypto import VerifiedAdvert, sign_advert, verify_advert
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, build_appdata
from sighop.webhooks.dispatcher import (
    MAX_ATTEMPTS,
    WebhookDispatcher,
    enabled_summary,
    send_sample,
)
from sighop.webhooks.events import Position, WebhookEvent, sample_event
from sighop.webhooks.transport import AttemptOutcome, AttemptResult, post
from sighop.webhooks.triggers import Trigger
from tests.botfixtures import advert_record, verified_advert

NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)
SECRET = bytes(32)
SECRET_PATH = "/api/webhooks/42/s3cr3t-token"
SECRET_QUERY = "wait=true&token=hunter2"

OK = AttemptResult(AttemptOutcome.DELIVERED, status=204)
BUSY = AttemptResult(AttemptOutcome.RETRYABLE, status=503, reason="server error")
MISSING = AttemptResult(AttemptOutcome.FINAL, status=404, reason="rejected")


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))

    def named(self, name: str) -> list[dict[str, object]]:
        return [fields for _, event, fields in self.events if event == name]


def _record(
    name: str,
    *,
    triggers: tuple[str, ...] = ("new_repeater", "new_companion"),
    max_hops: int | None = None,
    enabled: bool = True,
    format: str = "json",
) -> WebhookRecord:
    return WebhookRecord(
        id=uuid.uuid4(),
        name=name,
        url_host=f"https://{name}.example",
        format=format,
        triggers=triggers,
        max_hops=max_hops,
        enabled=enabled,
        created_at=NOW,
    )


def _url(record: WebhookRecord) -> str:
    return f"{record.url_host}{SECRET_PATH}?{SECRET_QUERY}"


@dataclass
class FakeRepository:
    records: list[WebhookRecord] = field(default_factory=list)
    fail_reads: bool = False
    unsealable: set[str] = field(default_factory=set)
    fail_outcome_writes: bool = False
    deliveries: list[tuple[uuid.UUID, dt.datetime]] = field(default_factory=list)
    failures: list[tuple[uuid.UUID, dt.datetime, str]] = field(default_factory=list)
    reads: int = 0

    async def list_enabled(self, secret: bytes) -> Outcome[list[OpenedWebhook]]:
        self.reads += 1
        if self.fail_reads:
            return Failed(operation="list_enabled_webhooks", error=DatabaseError("unreachable"))
        return Succeeded(
            [
                OpenedWebhook(record=record, url=None, error="did not authenticate")
                if record.name in self.unsealable
                else OpenedWebhook(record=record, url=_url(record))
                for record in self.records
                if record.enabled
            ]
        )

    async def record_delivery(self, webhook_id: object, at: dt.datetime) -> Outcome[bool]:
        if self.fail_outcome_writes:
            raise RuntimeError("database went away")
        self.deliveries.append((webhook_id, at))  # type: ignore[arg-type]
        return Succeeded(True)

    async def record_failure(
        self, webhook_id: object, at: dt.datetime, reason: str
    ) -> Outcome[bool]:
        if self.fail_outcome_writes:
            return Failed(operation="record_webhook_failure", error=DatabaseError("unreachable"))
        self.failures.append((webhook_id, at, reason))  # type: ignore[arg-type]
        return Succeeded(True)


@dataclass
class FakeTransport:
    """Scripted results per host; the default answer is 204."""

    script: dict[str, list[AttemptResult]] = field(default_factory=dict)
    calls: list[tuple[str, bytes, str, float]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, url: str, body: bytes, content_type: str, timeout: float) -> AttemptResult:
        with self.lock:
            self.calls.append((url, body, content_type, timeout))
            for host, results in self.script.items():
                if url.startswith(host):
                    return results.pop(0) if len(results) > 1 else results[0]
        return OK

    def hosts(self) -> list[str]:
        return [url.split("/api/")[0] for url, *_ in self.calls]


@dataclass
class FakeSleep:
    delays: list[float] = field(default_factory=list)
    block: Callable[[float], bool] = lambda delay: False
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.block(delay):
            await self.gate.wait()


def _dispatcher(
    repository: FakeRepository,
    transport: FakeTransport | None = None,
    sleep: FakeSleep | None = None,
    logger: RecordingLogger | None = None,
    **kwargs: object,
) -> WebhookDispatcher:
    return WebhookDispatcher(
        repository=repository,
        secret=SECRET,
        logger=logger or RecordingLogger(),
        clock=lambda: NOW,
        sleep=sleep or FakeSleep(),
        transport=transport or FakeTransport(),
        **kwargs,  # type: ignore[arg-type]
    )


def _event(trigger: Trigger = Trigger.NEW_REPEATER, *, hop_count: int | None = 1) -> WebhookEvent:
    event = sample_event(trigger, NOW)
    return WebhookEvent(
        event_id=str(uuid.uuid4()),
        trigger=event.trigger,
        occurred_at=NOW,
        public_key=generate_identity().public_key,
        name="Hilltop",
        node_type=event.node_type,
        position=None,
        hop_count=hop_count,
        snr_db=3.0,
        rssi_dbm=-99,
        received_at=NOW,
    )


async def _deliver(dispatcher: WebhookDispatcher, *events: WebhookEvent) -> None:
    for event in events:
        await dispatcher.dispatch(event)
    await dispatcher.wait_idle()


# --- Triggers from the contact store ------------------------------------------


async def test_a_first_sighting_through_the_store_is_offered_without_any_io() -> None:
    repository = FakeRepository(records=[_record("dev-a")])
    transport = FakeTransport()
    dispatcher = _dispatcher(repository, transport)
    store = ContactStore()
    store.add_observation_listener(dispatcher.on_observation)
    identity = generate_identity()

    await store.handle(advert_record(verified_advert(identity, node_type=NodeType.REPEATER)))
    await store.handle(
        advert_record(verified_advert(identity, node_type=NodeType.REPEATER), packet_id="copy")
    )

    assert dispatcher.pending == 1, "the flood copy raised nothing"
    assert transport.calls == [] and repository.reads == 0, "offering touched neither"

    runner = asyncio.create_task(dispatcher.run())
    while dispatcher.delivered == 0:
        await asyncio.sleep(0.001)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    body = json.loads(transport.calls[0][1])
    assert body["event"] == "new_repeater"
    assert body["node"]["public_key"] == identity.public_key.hex()


async def test_the_delivered_path_names_a_known_repeater_hop() -> None:
    from sighop.protocol.payloads import WireText

    repository = FakeRepository(records=[_record("dev-a")])
    transport = FakeTransport()
    store = ContactStore()
    repeater = generate_identity()
    store.restore(
        [
            Contact(
                public_key=repeater.public_key,
                name=WireText.from_bytes(b"Hilltop"),
                node_type=NodeType.REPEATER,
                advert_verified=True,
            )
        ]
    )
    dispatcher = _dispatcher(repository, transport, contacts=store)
    store.add_observation_listener(dispatcher.on_observation)

    await store.handle(
        advert_record(
            verified_advert(generate_identity(), node_type=NodeType.CHAT),
            hash_size=2,
            path=repeater.public_key[:2],
        )
    )

    runner = asyncio.create_task(dispatcher.run())
    while dispatcher.delivered == 0:
        await asyncio.sleep(0.001)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    body = json.loads(transport.calls[0][1])
    assert body["event"] == "new_companion"
    assert body["reception"]["path"] == [
        {"hash": repeater.public_key[:2].hex(), "name": "Hilltop", "matches": 1}
    ]


@pytest.mark.parametrize("node_type", [NodeType.ROOM_SERVER, NodeType.SENSOR])
async def test_room_servers_and_sensors_raise_nothing(node_type: NodeType) -> None:
    dispatcher = _dispatcher(FakeRepository())
    contact = Contact(public_key=bytes(32), node_type=node_type, advert_verified=True)
    record = advert_record(verified_advert(generate_identity(), node_type=node_type))

    dispatcher.on_observation(ContactObservation(contact=contact, created=True), record)

    assert dispatcher.pending == 0


async def test_the_listener_never_raises() -> None:
    logger = RecordingLogger()

    def broken_clock() -> dt.datetime:
        raise RuntimeError("clock fell off the wall")

    dispatcher = WebhookDispatcher(
        repository=FakeRepository(), secret=SECRET, logger=logger, clock=broken_clock
    )
    contact = Contact(public_key=bytes(32), node_type=NodeType.CHAT, advert_verified=True)
    record = advert_record(verified_advert(generate_identity()))

    dispatcher.on_observation(ContactObservation(contact=contact, created=True), record)

    assert logger.named("webhook_event_failed")


# --- Which webhooks an event reaches ------------------------------------------


async def test_only_subscribed_webhooks_receive_an_event() -> None:
    companion = _record("dev-companion", triggers=("new_companion",))
    repeater = _record("dev-repeater", triggers=("new_repeater",))
    transport = FakeTransport()
    dispatcher = _dispatcher(FakeRepository(records=[companion, repeater]), transport)

    await _deliver(dispatcher, _event(Trigger.NEW_COMPANION))

    assert transport.hosts() == [companion.url_host]


@pytest.mark.parametrize(
    ("hop_count", "max_hops", "delivered"),
    [
        (3, 1, False),
        (1, 1, True),
        (0, 0, True),
        (None, 1, False),
        (None, None, True),
        (7, None, True),
    ],
)
async def test_the_hop_filter(hop_count: int | None, max_hops: int | None, delivered: bool) -> None:
    transport = FakeTransport()
    dispatcher = _dispatcher(
        FakeRepository(records=[_record("dev-a", max_hops=max_hops)]), transport
    )

    await _deliver(dispatcher, _event(hop_count=hop_count))

    assert bool(transport.calls) is delivered


async def test_a_disabled_webhook_receives_nothing() -> None:
    transport = FakeTransport()
    dispatcher = _dispatcher(FakeRepository(records=[_record("dev-off", enabled=False)]), transport)

    await _deliver(dispatcher, _event())

    assert transport.calls == []


async def test_an_unsealable_webhook_is_skipped_and_reported_while_others_deliver() -> None:
    good, bad = _record("dev-good"), _record("dev-bad")
    logger = RecordingLogger()
    transport = FakeTransport()
    dispatcher = _dispatcher(
        FakeRepository(records=[good, bad], unsealable={"dev-bad"}), transport, logger=logger
    )

    await _deliver(dispatcher, _event())

    assert transport.hosts() == [good.url_host]
    [reported] = logger.named("webhook_url_unsealable")
    assert reported["webhook_name"] == "dev-bad"
    assert "authenticate" in str(reported["error"])


# --- Queue ------------------------------------------------------------------


async def test_a_full_queue_drops_the_oldest_and_keeps_the_newest() -> None:
    logger = RecordingLogger()
    dispatcher = _dispatcher(FakeRepository(), logger=logger, queue_capacity=2)
    first, second, third = _event(), _event(), _event()

    for event in (first, second, third):
        dispatcher.offer(event)

    assert dispatcher.dropped == 1
    assert list(dispatcher._queue) == [second, third]
    [dropped] = logger.named("webhook_dropped")
    assert dropped["event_id"] == first.event_id


async def test_pending_events_at_shutdown_are_counted_as_dropped() -> None:
    dispatcher = _dispatcher(FakeRepository(), max_in_flight=0)
    runner = asyncio.create_task(dispatcher.run())
    dispatcher.offer(_event())
    dispatcher.offer(_event())
    await asyncio.sleep(0)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)

    assert dispatcher.dropped == 2


# --- Retries ----------------------------------------------------------------


async def test_a_transient_server_error_is_retried_and_recorded_as_delivered() -> None:
    hook = _record("dev-a")
    repository = FakeRepository(records=[hook])
    transport = FakeTransport(script={hook.url_host: [BUSY, OK]})
    sleep = FakeSleep()
    dispatcher = _dispatcher(repository, transport, sleep)

    await _deliver(dispatcher, _event())

    assert len(transport.calls) == 2
    assert sleep.delays == [2.0]
    assert dispatcher.delivered == 1 and dispatcher.failed == 0
    assert repository.deliveries == [(hook.id, NOW)]
    event_ids = {json.loads(body)["event_id"] for _, body, _, _ in transport.calls}
    assert len(event_ids) == 1, "a retry carries the same event identifier"


@pytest.mark.parametrize(("retry_after", "expected"), [(2.0, 2.0), (30.0, 30.0), (None, 2.0)])
async def test_a_rate_limit_waits_at_least_retry_after(
    retry_after: float | None, expected: float
) -> None:
    hook = _record("dev-a")
    limited = AttemptResult(
        AttemptOutcome.RETRYABLE, status=429, reason="rate limited", retry_after=retry_after
    )
    sleep = FakeSleep()
    dispatcher = _dispatcher(
        FakeRepository(records=[hook]), FakeTransport(script={hook.url_host: [limited, OK]}), sleep
    )

    await _deliver(dispatcher, _event())

    assert sleep.delays == [expected]
    assert dispatcher.delivered == 1


async def test_a_rejected_payload_is_not_retried_and_the_status_is_recorded() -> None:
    hook = _record("dev-a")
    repository = FakeRepository(records=[hook])
    transport = FakeTransport(script={hook.url_host: [MISSING]})
    sleep = FakeSleep()
    dispatcher = _dispatcher(repository, transport, sleep)

    await _deliver(dispatcher, _event())

    assert len(transport.calls) == 1 and sleep.delays == []
    assert dispatcher.failed == 1
    assert repository.failures == [(hook.id, NOW, "HTTP 404")]


async def test_exhausted_retries_abandon_the_event_and_record_why() -> None:
    hook = _record("dev-a")
    repository = FakeRepository(records=[hook])
    logger = RecordingLogger()
    sleep = FakeSleep()
    dispatcher = _dispatcher(
        repository, FakeTransport(script={hook.url_host: [BUSY]}), sleep, logger
    )

    await _deliver(dispatcher, _event())

    assert sleep.delays == [2.0, 10.0, 60.0]
    assert dispatcher.failed == 1 and dispatcher.delivered == 0
    assert repository.failures == [(hook.id, NOW, f"HTTP 503 after {MAX_ATTEMPTS} attempts")]
    [abandoned] = logger.named("webhook_abandoned")
    assert abandoned["attempts"] == MAX_ATTEMPTS
    assert len(logger.named("webhook_attempt_failed")) == MAX_ATTEMPTS - 1


async def test_a_webhook_in_backoff_does_not_delay_another() -> None:
    slow, fast = _record("dev-slow"), _record("dev-fast")
    sleep = FakeSleep(block=lambda delay: True)
    dispatcher = _dispatcher(
        FakeRepository(records=[slow, fast]),
        FakeTransport(script={slow.url_host: [BUSY]}),
        sleep,
    )

    await dispatcher.dispatch(_event())
    for _ in range(200):
        if dispatcher.delivered:
            break
        await asyncio.sleep(0.005)

    assert dispatcher.delivered == 1, "the fast webhook was delivered while the slow one waits"
    sleep.gate.set()
    await dispatcher.wait_idle()
    assert dispatcher.failed == 1


async def test_a_failed_outcome_write_does_not_affect_delivery() -> None:
    hook = _record("dev-a")
    repository = FakeRepository(records=[hook], fail_outcome_writes=True)
    logger = RecordingLogger()
    dispatcher = _dispatcher(repository, logger=logger)

    await _deliver(dispatcher, _event())

    assert dispatcher.delivered == 1
    assert dispatcher.outcome_record_failures == 1
    assert logger.named("webhook_outcome_record_failed")


# --- Live configuration -----------------------------------------------------


async def test_a_webhook_disabled_between_events_receives_only_the_first() -> None:
    hook = _record("dev-a")
    repository = FakeRepository(records=[hook])
    transport = FakeTransport()
    dispatcher = _dispatcher(repository, transport)

    await _deliver(dispatcher, _event())
    repository.records = [_record("dev-a", enabled=False)]
    await _deliver(dispatcher, _event())

    assert len(transport.calls) == 1
    assert repository.reads == 2, "configuration is read per event"


async def test_an_unreadable_configuration_falls_back_to_the_last_good_read() -> None:
    hook = _record("dev-a")
    repository = FakeRepository(records=[hook])
    logger = RecordingLogger()
    transport = FakeTransport()
    dispatcher = _dispatcher(repository, transport, logger=logger)

    await _deliver(dispatcher, _event())
    repository.fail_reads = True
    await _deliver(dispatcher, _event())

    assert len(transport.calls) == 2
    assert dispatcher.config_read_failures == 1
    [failed] = logger.named("webhook_config_read_failed")
    assert failed["using_last_good"] == 1


# --- What the logs carry -----------------------------------------------------


async def test_no_log_event_carries_a_url_path_or_query() -> None:
    delivered, retried, rejected = _record("dev-ok"), _record("dev-retry"), _record("dev-no")
    logger = RecordingLogger()
    dispatcher = _dispatcher(
        FakeRepository(records=[delivered, retried, rejected], unsealable=set()),
        FakeTransport(script={retried.url_host: [BUSY], rejected.url_host: [MISSING]}),
        logger=logger,
    )

    await _deliver(dispatcher, _event())
    await send_sample(
        OpenedWebhook(record=rejected, url=_url(rejected)),
        Trigger.NEW_REPEATER,
        logger=logger,
        transport=FakeTransport(script={rejected.url_host: [MISSING]}),
    )

    assert {name for _, name, _ in logger.events} >= {
        "webhook_delivered",
        "webhook_attempt_failed",
        "webhook_abandoned",
        "webhook_test_sent",
    }
    rendered = repr(logger.events)
    for secret in (SECRET_PATH, "s3cr3t", "hunter2", "wait=true"):
        assert secret not in rendered
    assert all(
        fields.get("url_host") in {None, delivered.url_host, retried.url_host, rejected.url_host}
        for _, _, fields in logger.events
    )


def test_status_segment_and_summary() -> None:
    dispatcher = _dispatcher(FakeRepository())
    dispatcher.delivered, dispatcher.failed, dispatcher.dropped = 3, 1, 2
    assert dispatcher.status_segment() == "wh ok=3 fail=1 drop=2"
    assert enabled_summary([]) == "0 enabled"
    assert (
        enabled_summary(
            [
                _record("a", triggers=("new_companion",)),
                _record("b", triggers=("new_companion", "new_repeater")),
            ]
        )
        == "2 enabled (new_repeater, new_companion)"
    )


# --- 5.3 Sample sends --------------------------------------------------------


async def test_a_sample_is_one_attempt_ignoring_enabled_and_hop_limit() -> None:
    hook = _record("dev-off", enabled=False, max_hops=0, format="discord")
    transport = FakeTransport()

    result = await send_sample(
        OpenedWebhook(record=hook, url=_url(hook)), Trigger.NEW_REPEATER, transport=transport
    )

    assert result.delivered and result.status == 204
    [(_, body, _, _)] = transport.calls
    assert json.loads(body)["embeds"][0]["title"].startswith("[test] ")


async def test_a_sample_to_a_404_reports_it_without_retrying() -> None:
    hook = _record("dev-a")
    transport = FakeTransport(script={hook.url_host: [MISSING]})

    result = await send_sample(
        OpenedWebhook(record=hook, url=_url(hook)), Trigger.NEW_COMPANION, transport=transport
    )

    assert not result.delivered and result.summary == "HTTP 404"
    assert len(transport.calls) == 1


async def test_a_sample_to_a_retryable_failure_is_still_one_attempt() -> None:
    hook = _record("dev-a")
    transport = FakeTransport(script={hook.url_host: [BUSY, OK]})

    result = await send_sample(
        OpenedWebhook(record=hook, url=_url(hook)), Trigger.NEW_COMPANION, transport=transport
    )

    assert result.status == 503 and len(transport.calls) == 1


async def test_a_sample_to_an_unresolvable_host_reports_why() -> None:
    hook = _record("dev-a")
    result = await send_sample(
        OpenedWebhook(record=hook, url="http://sighop-webhook-test.invalid/hook"),
        Trigger.NEW_REPEATER,
        transport=post,
        timeout=5.0,
    )

    assert not result.delivered
    assert result.status is None
    assert result.reason, "the failure says why"


# --- webhook-place-names 4.1-4.3: the place is named on the way out -----------

STOCKHOLM = Place(neighborhood="Gamla Stan", city="Stockholm", country="Sweden", country_code="SE")


@dataclass
class FakePlaces:
    place: Place | None = STOCKHOLM
    error: Exception | None = None
    calls: list[Located | None] = field(default_factory=list)

    async def name(self, position: Located | None) -> Place | None:
        self.calls.append(position)
        if self.error is not None:
            raise self.error
        return self.place


def _located(trigger: Trigger = Trigger.NEW_REPEATER) -> WebhookEvent:
    return dataclasses.replace(_event(trigger), position=Position(59.329460, 18.068580))


async def test_every_webhook_and_retry_names_the_same_place() -> None:
    plain, discord = _record("dev-json"), _record("dev-discord", format="discord")
    transport = FakeTransport(script={plain.url_host: [BUSY, OK]})
    places = FakePlaces()
    dispatcher = _dispatcher(FakeRepository(records=[plain, discord]), transport, places=places)

    await _deliver(dispatcher, _located())

    assert len(places.calls) == 1, "named once per event"
    bodies = [json.loads(body) for url, body, *_ in transport.calls]
    json_places = [b["node"]["position"]["place"] for b in bodies if "node" in b]
    assert len(json_places) == 2, "the first attempt and its retry"
    assert all(
        p
        == {
            "neighborhood": "Gamla Stan",
            "city": "Stockholm",
            "country": "Sweden",
            "country_code": "SE",
        }
        for p in json_places
    )
    [embed] = [b["embeds"][0] for b in bodies if "embeds" in b]
    [location] = [f["value"] for f in embed["fields"] if f["name"] == "Location"]
    assert location.startswith("Gamla Stan, Stockholm, Sweden\n[59.329460, 18.068580]")


async def test_an_event_without_a_position_is_never_named() -> None:
    places = FakePlaces()
    transport = FakeTransport()
    dispatcher = _dispatcher(FakeRepository(records=[_record("dev-a")]), transport, places=places)

    await _deliver(dispatcher, _event())

    assert places.calls == []
    assert json.loads(transport.calls[0][1])["node"]["position"] is None


@pytest.mark.parametrize("places", [FakePlaces(place=None), FakePlaces(error=RuntimeError("x"))])
async def test_a_position_that_cannot_be_named_is_still_delivered(places: FakePlaces) -> None:
    transport = FakeTransport()
    dispatcher = _dispatcher(FakeRepository(records=[_record("dev-a")]), transport, places=places)

    await _deliver(dispatcher, _located())

    assert dispatcher.delivered == 1
    position = json.loads(transport.calls[0][1])["node"]["position"]
    assert position["latitude"] == 59.32946 and position["place"] is None


async def test_the_listener_names_nothing_until_the_event_leaves_the_queue() -> None:
    identity = generate_identity()
    appdata = build_appdata(
        NodeType.REPEATER, name="Hilltop", latitude=59_329_460, longitude=18_068_580
    )
    verified = verify_advert(sign_advert(identity, 1_700_000_000, appdata))
    assert isinstance(verified, VerifiedAdvert)
    places = FakePlaces()
    transport = FakeTransport()
    dispatcher = _dispatcher(FakeRepository(records=[_record("dev-a")]), transport, places=places)
    store = ContactStore()
    store.add_observation_listener(dispatcher.on_observation)

    await store.handle(advert_record(verified))

    assert dispatcher.pending == 1 and places.calls == [], "offering named nothing"
    runner = asyncio.create_task(dispatcher.run())
    while dispatcher.delivered == 0:
        await asyncio.sleep(0.001)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    assert places.calls == [Position(59.32946, 18.06858)]
    assert json.loads(transport.calls[0][1])["node"]["position"]["place"]["city"] == "Stockholm"


async def test_a_discord_sample_shows_its_place_above_the_link() -> None:
    hook = _record("dev-a", format="discord")
    transport = FakeTransport()

    await send_sample(
        OpenedWebhook(record=hook, url=_url(hook)),
        Trigger.NEW_REPEATER,
        transport=transport,
        places=FakePlaces(),
    )

    [(_, body, _, _)] = transport.calls
    fields = json.loads(body)["embeds"][0]["fields"]
    [location] = [f["value"] for f in fields if f["name"] == "Location"]
    assert location == (
        "Gamla Stan, Stockholm, Sweden\n"
        "[59.329460, 18.068580]"
        "(https://www.google.com/maps/search/?api=1&query=59.329460,18.068580)"
    )
