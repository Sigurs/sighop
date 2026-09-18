"""The plugin seam and the host (`bot-runtime`, tasks 6.1-6.4, 7.1-7.7).

The properties asserted here are the ones a driver author cannot be trusted to
uphold and a reviewer cannot see by reading one file:

* a driver's only route to the radio is its context, so the mode and the rate
  limit are not checks anybody can forget;
* driver work never runs on the reception path, and a slow, overflowing or
  raising driver costs its own bot and nothing else;
* observe mode produces **zero** submissions, asserted at the seam a submission
  would have crossed rather than by reading the mode back.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest

from sighop.bots import drivers as bot_drivers
from sighop.bots.base import (
    AdvertEvent,
    Bot,
    BotContext,
    BotDispatchDropped,
    BotFailed,
    BotSendResult,
    BotStateHandle,
    BotWouldAct,
    SuppressionReason,
    UnknownDriverError,
)
from sighop.bots.runtime import BotHost, BotWorker, TokenBucket
from sighop.net.contacts import Contact, ContactStore
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from tests.botfixtures import (
    MemoryBotState,
    MemoryBotStorage,
    RecordingSender,
    advert_record,
    bot_record,
    verified_advert,
)

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


class FakeClock:
    """Time an assertion can move. The rate limit refills against a clock, and
    a test that waited out an hour would be a test nobody runs."""

    def __init__(self, start: dt.datetime = NOW) -> None:
        self._now = start

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


class SpyDriver:
    """A driver that records what it was handed and does what it was told."""

    driver_name = "spy"

    def __init__(self, *, send: str | None = None, raises: bool = False) -> None:
        self.adverts: list[AdvertEvent] = []
        self.messages: list = []
        self.contexts: list[BotContext] = []
        self.send = send
        self.raises = raises
        self.gate: asyncio.Event | None = None

    @staticmethod
    def default_config() -> dict[str, object]:
        return {}

    @staticmethod
    def validate_config(key: str, value: str) -> object:
        return value

    def limits(self) -> dict[str, object]:
        return {}

    def counters(self) -> dict[str, int]:
        return {"seen": len(self.adverts)}

    async def on_advert(self, context: BotContext, event: AdvertEvent) -> None:
        self.adverts.append(event)
        self.contexts.append(context)
        if self.gate is not None:
            await self.gate.wait()
        if self.raises:
            raise RuntimeError("the driver did something unwise")
        if self.send is not None:
            await context.send(event.contact, self.send)

    async def on_direct_message(self, context: BotContext, event) -> None:
        self.messages.append(event)


def contact_for(name: str = "peer") -> Contact:
    from sighop.protocol.payloads import WireText

    return Contact(
        public_key=generate_identity().public_key,
        name=WireText.from_bytes(name.encode()),
        node_type=NodeType.CHAT,
        advert_verified=True,
    )


def worker(
    driver: object | None = None,
    *,
    mode: str = "active",
    storage: MemoryBotStorage | None = None,
    sender: object | None = None,
    route_known: bool = True,
    clock: FakeClock | None = None,
    capacity: int = 4,
    config: dict | None = None,
    events: list | None = None,
) -> BotWorker:
    return BotWorker(
        record=bot_record(driver="spy", mode=mode, config=config or {}),
        driver=driver or SpyDriver(),  # type: ignore[arg-type]
        storage=storage or MemoryBotStorage(),
        send_message=sender or RecordingSender(),  # type: ignore[arg-type]
        route_known=lambda contact: route_known,
        lookup=lambda key: None,
        clock=clock or FakeClock(),
        capacity=capacity,
        on_event=None if events is None else events.append,
    )


def advert_event(contact: Contact | None = None, **kwargs: object) -> AdvertEvent:
    return AdvertEvent(
        contact=contact or contact_for(),
        created=bool(kwargs.get("created", True)),
        hop_count=kwargs.get("hop_count", 0),  # type: ignore[arg-type]
        snr_db=kwargs.get("snr_db", 6.0),  # type: ignore[arg-type]
        packet_id=str(kwargs.get("packet_id", "packet-1")),
        received_at=NOW,
    )


# --- 6.1 The protocol -------------------------------------------------------


def test_the_protocol_has_two_handlers_and_no_channel_hook() -> None:
    """6.1: §7 sketches three handlers; the third is absent by operator decision.

    Milestone 7 left it out because nothing decrypted `GRP_TXT`; change
    `channel-messaging` decrypts it, and the hook stays absent because channel
    senders are unauthenticated and a channel post floods the mesh. Its absence
    is asserted, and the module says why.
    """
    members = set(Bot.__protocol_attrs__)  # type: ignore[attr-defined]

    assert "on_advert" in members
    assert "on_direct_message" in members
    assert "on_channel_message" not in members

    from sighop.bots import base

    assert "on_channel_message" in base.__doc__ or ""
    assert "GRP_TXT" in base.__doc__


def test_a_driver_satisfies_the_protocol_structurally() -> None:
    """6.1: drivers are in-tree classes, not subclasses of a base."""
    from sighop.bots.greeter import GreeterBot

    assert isinstance(GreeterBot(), Bot)
    assert isinstance(SpyDriver(), Bot)


# --- 6.2 What a context exposes ---------------------------------------------


def test_nothing_reachable_from_a_context_is_the_radio_or_the_database() -> None:
    """6.2: the seam is capability-based, and this is what that means.

    A context holds a handful of callables and one state handle. Not the
    scheduler, not the bus, not the modem, not a session — so there is no
    attribute walk that reaches the radio, and the mode and the rate limit
    cannot be stepped around by a driver that goes looking.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from sighop.db.engine import Database
    from sighop.net.bus import NetworkBus
    from sighop.net.dm import DirectMessenger
    from sighop.net.paths import PathStore
    from sighop.net.tx import TxScheduler
    from sighop.radio.modem import Modem

    forbidden = (TxScheduler, NetworkBus, Modem, Database, AsyncSession, DirectMessenger, PathStore)
    context = worker().context()

    reachable = [getattr(context, name) for name in context.__slots__]
    reachable += [getattr(context.state, name) for name in context.state.__slots__]
    for value in reachable:
        assert not isinstance(value, forbidden), f"{value!r} is reachable from a context"

    assert set(context.__slots__) == {
        "bot_name",
        "mode",
        "state",
        "_send",
        "_can_send",
        "_lookup",
        "_suppress",
        "_degraded",
        "_announce",
        "_now",
    }


# --- 6.3 The advert event ---------------------------------------------------


async def test_the_advert_event_carries_what_the_reception_established() -> None:
    """6.3: the contact, whether it was new, the hop count and the signal."""
    host = BotHost(workers=[])
    spy = SpyDriver()
    host.add(worker(spy))
    store = ContactStore(on_observation=host.on_observation)
    identity = generate_identity()

    await store.handle(
        advert_record(verified_advert(identity, name="stranger"), hop_count=2, snr_db=-4.0)
    )
    await host.workers[0].dispatch(host.workers[0]._queue.get_nowait())

    assert len(spy.adverts) == 1
    event = spy.adverts[0]
    assert event.contact.public_key == identity.public_key
    assert event.created is True
    assert event.hop_count == 2
    assert event.snr_db == -4.0
    assert event.node_type is NodeType.CHAT


async def test_an_unverified_advert_produces_no_driver_invocation() -> None:
    """6.3: enforced by the type, not by a check anybody has to remember.

    An advert whose signature failed produces no `VerifiedAdvert`, so the store
    records no observation, so no event exists to hand a driver. The reception
    is still handled: nothing is dropped, only nothing is *acted on*.
    """
    from sighop.net.rx import AdvertOutcome, RxRecord
    from sighop.protocol.crypto import AdvertVerificationFailure

    host = BotHost(workers=[])
    spy = SpyDriver()
    host.add(worker(spy))
    store = ContactStore(on_observation=host.on_observation)

    await store.handle(
        RxRecord(
            packet_id="bad",
            received_at=NOW,
            raw=b"\x00",
            snr_db=1.0,
            rssi_dbm=-90,
            packet=None,
            outcome=AdvertOutcome(
                verification=AdvertVerificationFailure(reason="bad_signature", node_hash=0x42)
            ),
        )
    )

    assert host.workers[0].pending == 0
    assert spy.adverts == []
    assert len(store) == 0


# --- 6.4 The registry -------------------------------------------------------


def test_an_unknown_driver_is_refused_by_listing_what_exists() -> None:
    """6.4, design D14: no entry points and no import by string."""
    with pytest.raises(UnknownDriverError) as excinfo:
        bot_drivers.lookup("weather")

    assert "weather" in str(excinfo.value)
    assert "greeter" in str(excinfo.value), "the refusal names the drivers that exist"
    assert bot_drivers.driver_names() == ("greeter",)


def test_a_default_configuration_carries_the_runtimes_own_limits() -> None:
    """6.4: the two reserved keys are the runtime's, and a new bot has them."""
    config = bot_drivers.default_config("greeter")

    assert float(config["rate_per_hour"]) > 0  # type: ignore[arg-type]
    assert int(config["burst"]) >= 1  # type: ignore[call-overload]
    assert "greeting" in config


# --- 7.1 / 7.2 Dispatch -----------------------------------------------------


async def test_a_slow_driver_delays_neither_the_offer_nor_the_next_reception() -> None:
    """7.1: the handler runs in the worker, never in the bus subscriber."""
    spy = SpyDriver()
    spy.gate = asyncio.Event()
    host = BotHost(workers=[])
    host.add(worker(spy))
    host.start()
    try:
        store = ContactStore(on_observation=host.on_observation)
        for index in range(3):
            # Each of these would block for as long as the driver does if the
            # handler ran here. `wait_for` is the assertion.
            await asyncio.wait_for(
                store.handle(
                    advert_record(
                        verified_advert(generate_identity(), name=f"peer-{index}"),
                        packet_id=f"p{index}",
                    )
                ),
                timeout=1.0,
            )
        assert len(store) == 3, "every reception was recorded while the driver hung"
    finally:
        spy.gate.set()
        await host.stop()


async def test_an_overflowing_queue_drops_the_oldest_counts_it_and_keeps_running() -> None:
    """7.2, design D12: `net/bus.py`'s policy, not a second one."""
    events: list = []
    spy = SpyDriver()
    hung = worker(spy, capacity=2, events=events)

    first, second, third = advert_event(), advert_event(), advert_event()
    hung.offer(first)
    hung.offer(second)
    hung.offer(third)

    assert hung.counters.dropped == 1
    assert hung.pending == 2
    assert hung._queue.get_nowait() is second, "the oldest went, not the newest"
    dropped = [event for event in events if isinstance(event, BotDispatchDropped)]
    assert dropped and dropped[0].dropped == 1

    # And the bot keeps taking work afterwards.
    hung.offer(advert_event())
    assert hung.pending == 2


# --- 7.3 Isolation ----------------------------------------------------------


async def test_a_raising_driver_is_counted_reported_and_costs_no_other_bot() -> None:
    """7.3: a driver is in-process and may do anything; it may not stop the run."""
    events: list = []
    exploding = worker(SpyDriver(raises=True), events=events)
    quiet_driver = SpyDriver()
    quiet = worker(quiet_driver, events=events)
    host = BotHost(workers=[exploding, quiet])

    event = advert_event()
    for bot in host.workers:
        bot.offer(event)
    for bot in host.workers:
        await bot.dispatch(bot._queue.get_nowait())

    assert exploding.counters.failures == 1
    assert quiet.counters.failures == 0
    assert quiet_driver.adverts == [event], "the second bot saw the same advert"

    failure = next(item for item in events if isinstance(item, BotFailed))
    assert failure.bot_name == exploding.name
    assert "unwise" in failure.error
    assert exploding.as_json()["bot_driver_failures"] == 1


async def test_a_cancelled_dispatch_is_a_shutdown_and_not_a_driver_failure() -> None:
    """7.3: swallowing `CancelledError` here is how a task survives its own stop."""
    spy = SpyDriver()
    spy.gate = asyncio.Event()
    bot = worker(spy)
    bot.offer(advert_event())
    bot.start()
    await asyncio.sleep(0)

    await bot.stop()

    assert bot.counters.failures == 0


# --- 7.4 The rate limit -----------------------------------------------------


def test_the_bucket_refuses_after_its_burst_and_refills_over_time() -> None:
    """7.4, design D11: one bucket, refilled from a rate, on a clock."""
    clock = FakeClock()
    bucket = TokenBucket(rate_per_hour=3600.0, burst=2)

    assert bucket.spend(clock.now()) is True
    assert bucket.spend(clock.now()) is True
    assert bucket.check(clock.now()) is False
    assert bucket.spend(clock.now()) is False

    clock.advance(1.0)
    assert bucket.spend(clock.now()) is True, "one per second at 3600/h"
    clock.advance(3600.0)
    assert bucket.tokens <= 2 or bucket.check(clock.now())


async def test_a_burst_of_adverts_exhausts_the_limit_and_the_refusals_are_counted() -> None:
    """7.4: §7's warned-about case — a burst of adverts after an outage."""
    clock = FakeClock()
    sender = RecordingSender()
    bot = worker(
        SpyDriver(send="hello"),
        sender=sender,
        clock=clock,
        config={"rate_per_hour": 1.0, "burst": 2},
    )

    for index in range(5):
        await bot.dispatch(advert_event(packet_id=f"p{index}"))

    assert len(sender.sent) == 2, "the burst, and no more"
    assert bot.counters.suppressions[str(SuppressionReason.RATE_LIMITED)] == 3
    assert bot.counters.actions == 2


async def test_the_limit_is_spent_before_anything_is_composed() -> None:
    """7.4: a refused action reaches neither the messenger nor the scheduler."""
    sender = RecordingSender()
    bot = worker(SpyDriver(send="hello"), sender=sender, config={"rate_per_hour": 0.0, "burst": 0})

    await bot.dispatch(advert_event())

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.RATE_LIMITED): 1}


async def test_a_contact_with_no_known_route_is_refused_before_the_limit() -> None:
    """7.4 / 8.5: a greeting is never flooded, and never costs a token either."""
    sender = RecordingSender()
    bot = worker(SpyDriver(send="hello"), sender=sender, route_known=False)

    await bot.dispatch(advert_event())

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.NO_ROUTE): 1}
    assert bot.bucket.tokens == float(bot.bucket.burst), "a refusal consumed nothing"


# --- 7.5 Observe mode -------------------------------------------------------


async def test_an_observe_mode_bot_that_decided_to_send_submits_nothing() -> None:
    """7.5, design D4: the whole decision path runs and the radio is untouched."""
    events: list = []
    sender = RecordingSender()
    bot = worker(SpyDriver(send="hello"), mode="observe", sender=sender, events=events)

    await bot.dispatch(advert_event())

    assert sender.sent == [], "zero submissions reached the scheduler"
    assert bot.counters.observations == 1
    assert bot.counters.actions == 0
    would = next(event for event in events if isinstance(event, BotWouldAct))
    assert would.text == "hello"


async def test_observe_mode_still_spends_from_the_limit() -> None:
    """7.5: so an observed run's counters are what an active run would have done.

    A bucket that only drained when transmitting would make the dry run
    optimistic about exactly the burst the limit exists for — and the dry run is
    what the operator decides the limit from.
    """
    bot = worker(SpyDriver(send="hello"), mode="observe", config={"rate_per_hour": 0.0, "burst": 1})

    await bot.dispatch(advert_event())
    await bot.dispatch(advert_event())

    assert bot.counters.observations == 1
    assert bot.counters.suppressions == {str(SuppressionReason.RATE_LIMITED): 1}


# --- 7.6 The ordinary outbound path -----------------------------------------


async def test_an_active_bot_sends_at_the_originated_traffic_priority_class() -> None:
    """7.6: `PriorityClass.MESSAGE`, and no retry policy of its own.

    Asserted through a real `DirectMessenger` rather than a stand-in, because
    "the same path an operator-sent message uses" is a claim about that object.
    """
    from sighop.net.bus import PriorityClass
    from sighop.net.paths import PathStore
    from tests.test_dm import Entity, RecordingSubmit, messenger, zero_hop_route_to

    peer = generate_identity()
    contacts = ContactStore()
    contact = contacts.add_public_key(peer.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, peer.public_key)
    submit = RecordingSubmit()
    entity = Entity("greeter-bot")
    dm = messenger(entity, contacts=contacts, paths=paths, submit=submit)

    async def send(target: Contact, text: str, grace: float = 0.0):
        return await dm.send(entity, target, text, allow_flood=False)

    bot = worker(SpyDriver(send="hello"), sender=send)
    await bot.dispatch(advert_event(contact))

    assert submit.submissions, "the message reached the scheduler"
    assert submit.submissions[0].priority is PriorityClass.MESSAGE
    assert submit.submissions[0].origin == "direct_message"
    # Four attempts is `MAX_ATTEMPT + 1` from `net/dm.py` and not a bot policy:
    # the bot introduced no retry loop of its own.
    from sighop.net.dm import MAX_ATTEMPT

    assert len(submit.submissions) <= MAX_ATTEMPT + 1


async def test_a_send_that_never_reached_the_air_is_reported_as_refused() -> None:
    """7.6: the existing transmit gate refuses exactly as it does for anyone."""
    from sighop.net.dm import SendResult

    sender = RecordingSender(result=SendResult.DROPPED)
    bot = worker(SpyDriver(), sender=sender)
    outcome = await bot._send(contact_for(), "hello")

    assert outcome.result is BotSendResult.REFUSED
    assert bot.counters.actions == 1, "it was attempted, and the attempt is counted"


# --- 7.7 Durable state ------------------------------------------------------


async def test_two_bots_writing_one_key_read_back_their_own_value() -> None:
    """7.7: the isolation is the schema's and the handle carries the bot id."""
    store = MemoryBotState()
    first = BotStateHandle(bot_id=uuid.uuid4(), store=store)
    second = BotStateHandle(bot_id=uuid.uuid4(), store=store)

    assert await first.set("greeted:aa", {"n": 1}) is True
    assert await second.set("greeted:aa", {"n": 2}) is True

    assert await first.get("greeted:aa") == {"n": 1}
    assert await second.get("greeted:aa") == {"n": 2}


async def test_a_value_survives_a_restart_of_the_worker() -> None:
    """7.7: state is restored before the driver's first event, because it is
    read from storage rather than held in the worker at all."""
    storage = MemoryBotStorage()
    record = bot_record(driver="spy")
    before = BotWorker(
        record=record,
        driver=SpyDriver(),
        storage=storage,
        send_message=RecordingSender(),
        route_known=lambda contact: True,
        lookup=lambda key: None,
    )
    await before.state.set("greeted:aa", {"outcome": "acknowledged"})

    after = BotWorker(
        record=record,
        driver=SpyDriver(),
        storage=storage,
        send_message=RecordingSender(),
        route_known=lambda contact: True,
        lookup=lambda key: None,
    )

    assert await after.state.get("greeted:aa") == {"outcome": "acknowledged"}


async def test_a_write_that_cannot_be_persisted_reports_failure() -> None:
    """7.7: never presented to the driver as having succeeded."""
    storage = MemoryBotStorage(bot_state=MemoryBotState(fail_writes=True))
    bot = worker(SpyDriver(), storage=storage)

    assert await bot.state.set("greeted:aa", {}) is False
    assert bot.state.write_failures == 1


# --- The host's two entry points --------------------------------------------


async def test_a_direct_message_reaches_only_the_bot_it_was_addressed_to() -> None:
    """`bot-runtime`: adverts fan out to every bot; a direct message does not."""
    from sighop.net.dm import MessageReceived
    from sighop.protocol.payloads import TextMessageBody, TextType, WireText
    from tests.test_dm import Entity

    mine, theirs = Entity("mine"), Entity("theirs")
    first, second = worker(SpyDriver()), worker(SpyDriver())
    first.entity, second.entity = mine, theirs
    host = BotHost(workers=[first, second])

    host.on_message(
        MessageReceived(
            entity_name="mine",
            contact=contact_for(),
            body=TextMessageBody(
                timestamp=0, txt_type=TextType.PLAIN, attempt=0, text=WireText.from_bytes(b"hi")
            ),
            packet_id="p",
            candidates_tried=1,
            acknowledged=True,
            entity=mine,
        )
    )

    assert first.pending == 1
    assert second.pending == 0


async def test_a_message_reaching_a_driver_carries_the_claimed_sender_and_text() -> None:
    from sighop.net.dm import MessageReceived
    from sighop.protocol.payloads import TextMessageBody, TextType, WireText
    from tests.test_dm import Entity

    entity = Entity("mine")
    spy = SpyDriver()
    bot = worker(spy)
    bot.entity = entity
    host = BotHost(workers=[bot])
    sender = contact_for("caller")

    host.on_message(
        MessageReceived(
            entity_name="mine",
            contact=sender,
            body=TextMessageBody(
                timestamp=7, txt_type=TextType.PLAIN, attempt=0, text=WireText.from_bytes(b"ping")
            ),
            packet_id="p",
            candidates_tried=1,
            acknowledged=True,
            entity=entity,
        )
    )
    await bot.dispatch(bot._queue.get_nowait())

    assert len(spy.messages) == 1
    assert spy.messages[0].contact is sender
    assert spy.messages[0].text.text == "ping"
    assert spy.messages[0].timestamp == 7


async def test_a_host_with_no_bots_does_nothing_with_an_observation() -> None:
    host = BotHost(workers=[])
    store = ContactStore(on_observation=host.on_observation)

    await store.handle(advert_record(verified_advert(generate_identity())))

    assert len(store) == 1
