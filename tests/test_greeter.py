"""The greeter's four gates and its once-ever rule (`greeter-bot`, group 8).

Every gate is asserted separately, because an operator has to be able to tell
which one is silencing the bot — a greeter that greets nobody must be
distinguishable from a mesh that has gone quiet and from a greeter that is
broken. Three properties are asserted rather than assumed:

* the greeting record is written **before** the transmission (design D6), so a
  crash between them costs an un-sent greeting rather than a duplicate;
* an **unacknowledged** greeting is not a delivered one — it is retried under a
  cooldown and a bounded attempt count (design D7, finding 1 from the live
  exercise), because a greeting the peer could not read is not a greeting;
* a peer is **introduced to** before it is messaged, at the distance its advert
  arrived from (finding 2), because a direct message is only decryptable by a
  node that already holds the sender's public key.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.bots import drivers as bot_drivers
from sighop.bots.base import AdvertEvent, BotSuppressed, SuppressionReason
from sighop.bots.greeter import DEFAULT_GREETING, GreeterBot, greeted_key
from sighop.bots.runtime import BotWorker
from sighop.net.contacts import Contact
from sighop.net.dm import MAX_TEXT_LEN, SendResult
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, WireText
from tests.botfixtures import MemoryBotState, MemoryBotStorage, RecordingSender, bot_record

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def contact_for(
    name: str = "stranger", node_type: NodeType | int = NodeType.CHAT
) -> Contact:
    return Contact(
        public_key=generate_identity().public_key,
        name=WireText.from_bytes(name.encode()),
        node_type=node_type,
        advert_verified=True,
    )


class FakeClock:
    """Time an assertion can move. The retry cooldown is minutes wide, and a
    test that waited one out is a test nobody runs."""

    def __init__(self, start: dt.datetime = NOW) -> None:
        self._now = start

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, minutes: float) -> None:
        self._now += dt.timedelta(minutes=minutes)

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - unused here
        self._now += dt.timedelta(seconds=seconds)


class RecordingAnnouncer:
    """Stands in for the advert scheduler, and records what reach was asked for."""

    def __init__(self, *, sent: bool = True) -> None:
        self.calls: list[bool] = []
        self.sent = sent

    async def __call__(self, flood: bool) -> bool:
        self.calls.append(flood)
        return self.sent


def greeter(
    *,
    mode: str = "active",
    storage: MemoryBotStorage | None = None,
    sender: RecordingSender | None = None,
    route_known: bool = True,
    config: dict | None = None,
    clock: FakeClock | None = None,
    announcer: RecordingAnnouncer | None = None,
) -> BotWorker:
    settings = {**bot_drivers.default_config("greeter"), **(config or {})}
    return BotWorker(
        record=bot_record(driver="greeter", mode=mode, config=settings),
        driver=GreeterBot(config=settings),
        storage=storage or MemoryBotStorage(),
        send_message=sender or RecordingSender(),
        route_known=lambda contact: route_known,
        lookup=lambda key: None,
        announce_advert=announcer or RecordingAnnouncer(),
        clock=clock or FakeClock(),
    )


def rebind(bot: BotWorker, **changes: object) -> BotWorker:
    """A second worker over the same bot row and the same storage.

    What a restart is, at this level: the record is durable and the counters are
    not, so continuing means a fresh worker reading the same rows.
    """
    fresh = greeter(**changes)  # type: ignore[arg-type]
    fresh.record = bot.record
    fresh.state.bot_id = bot.record.id
    return fresh


def advert(
    contact: Contact | None = None,
    *,
    created: bool = True,
    hop_count: int | None = 0,
    snr_db: float | None = 6.0,
) -> AdvertEvent:
    return AdvertEvent(
        contact=contact or contact_for(),
        created=created,
        hop_count=hop_count,
        snr_db=snr_db,
        packet_id="packet-1",
        received_at=NOW,
    )


# --- 8.1 What "new" means ---------------------------------------------------


async def test_a_first_advert_from_an_unknown_node_is_greeted() -> None:
    sender = RecordingSender()
    bot = greeter(sender=sender)
    contact = contact_for()

    await bot.dispatch(advert(contact))

    assert len(sender.sent) == 1
    assert sender.sent[0][0] is contact
    assert sender.sent[0][1] == DEFAULT_GREETING


async def test_a_node_known_but_never_greeted_is_greeted() -> None:
    """8.1, design D7 revised: known is not greeted.

    `created` is false — this contact was already in the table — and it still
    has an unsent welcome. Requiring `created` would refuse it forever, because
    the one advert that would have qualified arrived before this greeter
    existed.
    """
    sender = RecordingSender()
    bot = greeter(sender=sender)
    contact = contact_for()

    await bot.dispatch(advert(contact, created=False))

    assert len(sender.sent) == 1
    assert sender.sent[0][0] is contact


async def test_a_contact_an_operator_added_is_greeted_when_later_heard() -> None:
    """8.1: a pasted public key is a node nobody has said anything to.

    The contact exists because an operator typed it; nothing has been sent to
    it, and its first verified advert is the first chance to.
    """
    sender = RecordingSender()
    bot = greeter(sender=sender)

    await bot.dispatch(advert(created=False))

    assert len(sender.sent) == 1


async def test_a_contact_skipped_once_is_still_greeted_later() -> None:
    """8.1: the case the `created` gate silently lost.

    A contact heard while the rate limit was exhausted was never greeted and
    would never be created again, so the old gate refused it forever. The record
    gate does not: nothing was written, so it is still owed a greeting.
    """
    sender = RecordingSender()
    storage = MemoryBotStorage()
    bot = greeter(sender=sender, storage=storage, config={"rate_per_hour": 0.0, "burst": 0})
    contact = contact_for()

    await bot.dispatch(advert(contact))
    assert sender.sent == []
    assert storage.bot_state.rows == {}, "a refused action wrote no record"

    generous = greeter(sender=sender, storage=storage)
    generous.record = bot.record
    generous.state.bot_id = bot.record.id
    await generous.dispatch(advert(contact, created=False))

    assert len(sender.sent) == 1


async def test_a_second_advert_after_a_greeting_is_suppressed() -> None:
    """8.1: the record is the gate, and one greeting is what it permits."""
    sender = RecordingSender()
    bot = greeter(sender=sender)
    contact = contact_for()

    await bot.dispatch(advert(contact))
    await bot.dispatch(advert(contact, created=False))

    assert len(sender.sent) == 1
    assert bot.counters.suppressions == {str(SuppressionReason.ALREADY_GREETED): 1}


async def test_a_greeting_record_survives_the_contact_being_re_created() -> None:
    """8.1 / 8.7: the record is per bot; the contact table is the platform's
    memory of the mesh, and losing a contact does not re-open the gate."""
    storage = MemoryBotStorage()
    sender = RecordingSender()
    bot = greeter(storage=storage, sender=sender)
    contact = contact_for()

    await bot.dispatch(advert(contact))
    assert len(sender.sent) == 1

    # The contact row was removed and the same key heard again, so this advert
    # *created* it — which decides nothing.
    await bot.dispatch(advert(contact, created=True))

    assert len(sender.sent) == 1, "no second greeting"
    assert bot.counters.suppressions[str(SuppressionReason.ALREADY_GREETED)] == 1


async def test_a_seeded_contact_is_not_greeted() -> None:
    """8.10: what `sighop bot create` writes for every contact already known.

    Marked `seeded` rather than sent, and it suppresses exactly as a sent
    greeting does — the distinction exists for the operator reading the record,
    not for this decision.
    """
    from sighop.bots.greeter import SEEDED

    storage = MemoryBotStorage()
    sender = RecordingSender()
    bot = greeter(storage=storage, sender=sender)
    contact = contact_for()
    storage.bot_state.rows[(bot.record.id, greeted_key(contact.public_key))] = {
        "outcome": SEEDED
    }

    await bot.dispatch(advert(contact))

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.ALREADY_GREETED): 1}


async def test_a_contact_heard_after_the_seed_is_greeted() -> None:
    """8.10: seeding bounds the backlog, it does not stop the greeter."""
    from sighop.bots.greeter import SEEDED

    storage = MemoryBotStorage()
    sender = RecordingSender()
    bot = greeter(storage=storage, sender=sender)
    seeded, fresh = contact_for("old"), contact_for("new")
    storage.bot_state.rows[(bot.record.id, greeted_key(seeded.public_key))] = {
        "outcome": SEEDED
    }

    await bot.dispatch(advert(seeded))
    await bot.dispatch(advert(fresh))

    assert [contact for contact, _ in sender.sent] == [fresh]


async def test_releasing_a_seeded_contact_makes_it_eligible_again() -> None:
    """8.10 / 10.6: what `sighop bot greeted <peer> --clear` buys, at the driver.

    One contact, released deliberately — which is the granularity a decision to
    message a stranger deserves.
    """
    from sighop.bots.greeter import SEEDED

    storage = MemoryBotStorage()
    sender = RecordingSender()
    bot = greeter(storage=storage, sender=sender)
    contact = contact_for()
    key = (bot.record.id, greeted_key(contact.public_key))
    storage.bot_state.rows[key] = {"outcome": SEEDED}

    await bot.dispatch(advert(contact))
    assert sender.sent == []

    del storage.bot_state.rows[key]
    await bot.dispatch(advert(contact, created=False))

    assert len(sender.sent) == 1


# --- 8.2 The hop gate -------------------------------------------------------


@pytest.mark.parametrize("hop_count", [0, 1])
async def test_a_nearby_advert_passes_the_hop_gate(hop_count: int) -> None:
    """8.2: a default of 1 greets what we hear directly and one repeater out."""
    sender = RecordingSender()
    bot = greeter(sender=sender)

    await bot.dispatch(advert(hop_count=hop_count))

    assert len(sender.sent) == 1


async def test_an_advert_beyond_the_hop_limit_is_suppressed_with_both_numbers() -> None:
    """8.2: the reception's hop count and the limit, so the bound is actionable."""
    events: list = []
    sender = RecordingSender()
    bot = greeter(sender=sender)
    bot.on_event = events.append

    await bot.dispatch(advert(hop_count=4))

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.TOO_MANY_HOPS): 1}
    suppressed = events[0]
    assert "4 hops" in suppressed.detail
    assert "limit 1" in suppressed.detail


async def test_the_hop_limit_is_configurable_and_reported_as_a_limit() -> None:
    """8.2: shown alongside the bot's other limits, which is where an operator
    who has just read a decision log goes to change it."""
    bot = greeter(config={"max_hops": 3})

    assert bot.limits()["max_hops"] == 3
    await bot.dispatch(advert(hop_count=3))
    assert bot.counters.actions == 1


# --- 8.3 The node-type gate -------------------------------------------------


async def test_a_repeater_is_suppressed_with_its_type_named() -> None:
    """8.3, design D9: a welcome message no human will read costs everyone."""
    events: list = []
    sender = RecordingSender()
    bot = greeter(sender=sender)
    bot.on_event = events.append

    await bot.dispatch(advert(contact_for("hilltop", NodeType.REPEATER)))

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.NODE_TYPE): 1}
    assert events[0].detail == "REPEATER"


async def test_a_room_server_is_suppressed_unless_it_is_configured_in() -> None:
    """8.3: default-restrictive, because the alternative spends others' airtime."""
    sender = RecordingSender()
    bot = greeter(sender=sender)
    await bot.dispatch(advert(contact_for("lounge", NodeType.ROOM_SERVER)))
    assert sender.sent == []

    permissive = greeter(
        sender=(other := RecordingSender()),
        config={"node_types": [int(NodeType.CHAT), int(NodeType.ROOM_SERVER)]},
    )
    await permissive.dispatch(advert(contact_for("lounge", NodeType.ROOM_SERVER)))
    assert len(other.sent) == 1


# --- 8.4 The offered signal floor -------------------------------------------


async def test_the_signal_floor_is_null_by_default_and_suppresses_below_it() -> None:
    """8.4, design D8: offered for the operator who wants to try, and honest
    about what it does not do."""
    assert bot_drivers.default_config("greeter")["min_snr_db"] is None

    sender = RecordingSender()
    bot = greeter(sender=sender, config={"min_snr_db": 0.0})

    await bot.dispatch(advert(snr_db=-8.0))

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.LOW_SNR): 1}


def test_the_module_states_that_neither_gate_identifies_a_ducted_path() -> None:
    """8.4: reviewed by assertion. The limitation is the point of the gate's
    documentation, and a docstring that lost it would make the gate look like a
    distance guarantee."""
    from sighop.bots import greeter as module

    assert module.__doc__ is not None
    assert "duct" in module.__doc__
    assert "tropospheric" in module.__doc__
    assert "min_snr_db" in module.__doc__


# --- 8.5 Never flooded ------------------------------------------------------


async def test_a_contact_with_no_known_route_is_suppressed_and_never_flooded() -> None:
    """8.5, design D10: an unsolicited direct message is the last packet that
    should be shouted across the whole mesh."""
    sender = RecordingSender()
    storage = MemoryBotStorage()
    bot = greeter(sender=sender, storage=storage, route_known=False)

    await bot.dispatch(advert())

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.NO_ROUTE): 1}
    assert storage.bot_state.rows == {}, "and no record was burned on it"


# --- 8.6 The record goes first ----------------------------------------------


async def test_the_greeting_record_is_written_before_the_transmission() -> None:
    """8.6: asserted by looking at storage from inside the send."""
    seen: list[bool] = []
    storage = MemoryBotStorage()
    contact = contact_for()

    async def send(target: Contact, text: str, grace: float = 0.0):
        seen.append(greeted_key(contact.public_key) in
                    {key for _, key in storage.bot_state.rows})
        return await RecordingSender()(target, text)

    bot = greeter(storage=storage, sender=send)  # type: ignore[arg-type]
    await bot.dispatch(advert(contact))

    assert seen == [True], "the record had already landed when the send began"


async def test_a_failed_record_write_produces_no_transmission() -> None:
    """8.6: the write must have *landed*, not merely been attempted."""
    sender = RecordingSender()
    storage = MemoryBotStorage(bot_state=MemoryBotState(fail_writes=True))
    bot = greeter(storage=storage, sender=sender)

    await bot.dispatch(advert())

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.STATE_WRITE_FAILED): 1}


async def test_a_degraded_database_suppresses_with_that_reason() -> None:
    """8.6's stated consequence: while storage is degraded the greeter greets
    nobody, and says why rather than going quiet."""
    events: list = []
    sender = RecordingSender()
    storage = MemoryBotStorage(degraded=True)
    bot = greeter(storage=storage, sender=sender)
    bot.on_event = events.append

    await bot.dispatch(advert())

    assert sender.sent == []
    assert bot.counters.suppressions == {str(SuppressionReason.STORAGE_DEGRADED): 1}
    assert "recorded before it is sent" in events[0].detail


# --- 8.7 The outcome is recorded, and never retried -------------------------


async def test_an_acknowledged_greeting_settles_the_contact_for_good() -> None:
    """8.7: the one outcome that proves the greeting was read."""
    storage = MemoryBotStorage()
    sender = RecordingSender(result=SendResult.ACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(storage=storage, sender=sender, clock=clock)
    contact = contact_for()

    await bot.dispatch(advert(contact))
    key = (bot.record.id, greeted_key(contact.public_key))
    assert storage.bot_state.rows[key]["outcome"] == "acknowledged"
    assert storage.bot_state.rows[key]["acknowledged"] is True

    # However long we wait, and however often it adverts.
    clock.advance(minutes=600)
    await bot.dispatch(advert(contact))

    assert len(sender.sent) == 1
    assert bot.counters.suppressions[str(SuppressionReason.ALREADY_GREETED)] == 1


# --- 8.7 An unacknowledged greeting is not a delivered one ------------------
#
# Finding 1 from the live exercise. The first run recorded an unacknowledged
# greeting as settled, and the peer — which had never been able to read it —
# was never greeted again. These are the tests that would have caught that.


async def test_an_unacknowledged_greeting_is_retried_after_the_cooldown() -> None:
    storage = MemoryBotStorage()
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(storage=storage, sender=sender, clock=clock)
    contact = contact_for()

    await bot.dispatch(advert(contact))
    key = (bot.record.id, greeted_key(contact.public_key))
    assert storage.bot_state.rows[key]["outcome"] == "unacknowledged"
    assert storage.bot_state.rows[key]["acknowledged"] is False
    assert storage.bot_state.rows[key]["attempts"] == 1

    clock.advance(minutes=15)
    await bot.dispatch(advert(contact))

    assert len(sender.sent) == 2, "the peer that could not read the first one gets another"
    assert storage.bot_state.rows[key]["attempts"] == 2
    assert bot.driver.counters()["greetings_retried"] == 1


async def test_an_advert_inside_the_cooldown_is_suppressed_with_the_time_left() -> None:
    """A node adverting every few minutes must not turn one silence into a burst."""
    events: list = []
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(sender=sender, clock=clock)
    bot.on_event = events.append
    contact = contact_for()

    await bot.dispatch(advert(contact))
    clock.advance(minutes=4)
    await bot.dispatch(advert(contact))
    clock.advance(minutes=1)
    await bot.dispatch(advert(contact))

    assert len(sender.sent) == 1
    assert bot.counters.suppressions == {str(SuppressionReason.GREETING_COOLDOWN): 2}
    suppressed = [event for event in events if isinstance(event, BotSuppressed)]
    assert "attempt 1 unanswered" in suppressed[-1].detail
    assert "retry in 10m" in suppressed[-1].detail


async def test_the_attempts_are_bounded_and_then_the_contact_is_left_alone() -> None:
    """Three greetings, then silence — a node that can never acknowledge must
    not be messaged forever."""
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(sender=sender, clock=clock)
    contact = contact_for()

    for _ in range(6):
        await bot.dispatch(advert(contact))
        clock.advance(minutes=15)

    assert len(sender.sent) == 3, "greeting_attempts, and no more"
    assert bot.counters.suppressions == {str(SuppressionReason.GREETING_EXHAUSTED): 3}


async def test_an_exhausted_contact_says_how_many_attempts_went_unanswered() -> None:
    events: list = []
    clock = FakeClock()
    bot = greeter(
        sender=RecordingSender(result=SendResult.UNACKNOWLEDGED),
        clock=clock,
        config={"greeting_attempts": 1},
    )
    bot.on_event = events.append
    contact = contact_for()

    await bot.dispatch(advert(contact))
    clock.advance(minutes=60)
    await bot.dispatch(advert(contact))

    suppressed = [event for event in events if isinstance(event, BotSuppressed)]
    assert suppressed[-1].reason is SuppressionReason.GREETING_EXHAUSTED
    assert "1 attempts, none acknowledged" in suppressed[-1].detail


async def test_a_retry_survives_a_restart_because_the_record_is_durable() -> None:
    """The attempt count and the timestamp are both in the record, so a restart
    neither forgets an attempt nor re-opens the cooldown."""
    storage = MemoryBotStorage()
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(storage=storage, sender=sender, clock=clock)
    contact = contact_for()

    await bot.dispatch(advert(contact))
    assert len(sender.sent) == 1

    restarted = rebind(bot, storage=storage, sender=sender, clock=clock)
    await restarted.dispatch(advert(contact))
    assert len(sender.sent) == 1, "still inside the cooldown after a restart"

    clock.advance(minutes=15)
    await restarted.dispatch(advert(contact))
    assert len(sender.sent) == 2


async def test_an_observed_greeting_is_recorded_and_active_does_not_re_greet() -> None:
    """8.7: switching a bot to active does not cause a contact it already
    decided about to be greeted."""
    storage = MemoryBotStorage()
    sender = RecordingSender()
    contact = contact_for()
    observing = greeter(mode="observe", storage=storage, sender=sender)

    await observing.dispatch(advert(contact))
    key = (observing.record.id, greeted_key(contact.public_key))
    assert storage.bot_state.rows[key]["outcome"] == "would-have-greeted"
    assert sender.sent == []

    active = greeter(mode="active", storage=storage, sender=sender)
    active.record = observing.record  # the same bot, in the same database
    active.state.bot_id = observing.record.id
    await active.dispatch(advert(contact))

    assert sender.sent == [], "the record it wrote while observing still holds"


# --- Introducing ourselves first --------------------------------------------
#
# Finding 2 from the live exercise, and the reason the first greeting was never
# acknowledged: a direct message is decrypted with a secret derived from the
# *sender's* public key, so a peer that has never heard our advert cannot read a
# word of it. The greeting looked exactly like a peer out of range.
#
# The two distances cost very differently, and that asymmetry is the policy: a
# zero-hop advert is local and cheap enough to spend up front, a flood is
# repeated by every repeater in the mesh and is spent only once silence has
# proved it necessary.


async def test_a_direct_neighbour_is_adverted_to_before_the_first_greeting() -> None:
    announcer = RecordingAnnouncer()
    sender = RecordingSender()
    bot = greeter(sender=sender, announcer=announcer)

    await bot.dispatch(advert(hop_count=0))

    assert announcer.calls == [False], "zero-hop: it is a direct neighbour"
    assert len(sender.sent) == 1
    assert bot.counters.announces == 1


async def test_a_one_hop_peer_is_greeted_bare_the_first_time() -> None:
    """No flood on a guess: it may already hold our key from an earlier advert,
    and a flood is repeated by every repeater in the mesh."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender()
    bot = greeter(sender=sender, announcer=announcer, config={"max_hops": 2})

    await bot.dispatch(advert(hop_count=1))

    assert announcer.calls == [], "nothing was spent introducing ourselves"
    assert len(sender.sent) == 1
    assert bot.counters.announces == 0


async def test_a_bare_greeting_that_goes_unanswered_escalates_immediately() -> None:
    """The bet that the peer already held our key has lost, and waiting fifteen
    minutes would only buy the same silence for the same reason. Flood advert
    and greet again, in the same reaction, while the peer is demonstrably awake."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender(
        results=[SendResult.UNACKNOWLEDGED, SendResult.ACKNOWLEDGED]
    )
    bot = greeter(sender=sender, announcer=announcer, config={"max_hops": 2})

    await bot.dispatch(advert(hop_count=1))

    assert announcer.calls == [True], "a flood, because zero-hop cannot reach it"
    assert len(sender.sent) == 2, "bare, then introduced"
    assert bot.driver.counters()["greetings_acknowledged"] == 1


async def test_the_escalated_greeting_settles_the_contact() -> None:
    """The whole point of escalating: the second one is readable, so it is acked,
    so the contact is closed rather than left owing a retry."""
    sender = RecordingSender(
        results=[SendResult.UNACKNOWLEDGED, SendResult.ACKNOWLEDGED]
    )
    storage = MemoryBotStorage()
    bot = greeter(sender=sender, storage=storage, config={"max_hops": 2})
    contact = contact_for()

    await bot.dispatch(advert(contact, hop_count=1))
    record = storage.bot_state.rows[(bot.record.id, greeted_key(contact.public_key))]

    assert record["outcome"] == "acknowledged"
    assert record["acknowledged"] is True
    assert record["attempts"] == 2


async def test_the_escalation_happens_once_and_then_the_cooldown_takes_over() -> None:
    """A second silence means something the escalation cannot fix — out of range,
    asleep, ignoring us — and the answer to that is to wait, not keep sending."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(
        sender=sender, announcer=announcer, clock=clock, config={"max_hops": 2}
    )
    contact = contact_for()

    await bot.dispatch(advert(contact, hop_count=1))

    assert len(sender.sent) == 2, "one escalation, not a loop"
    assert announcer.calls == [True]

    await bot.dispatch(advert(contact, hop_count=1))
    assert len(sender.sent) == 2, "inside the cooldown"

    clock.advance(minutes=15)
    await bot.dispatch(advert(contact, hop_count=1))
    assert len(sender.sent) == 3, "the third attempt, spaced by the cooldown"
    assert announcer.calls == [True, True], "it floods every attempt after the first"


async def test_the_escalation_stops_at_the_attempt_ceiling() -> None:
    """`greeting_attempts=1` means one greeting, so there is nothing to escalate
    into."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    bot = greeter(
        sender=sender,
        announcer=announcer,
        config={"max_hops": 2, "greeting_attempts": 1},
    )

    await bot.dispatch(advert(hop_count=1))

    assert len(sender.sent) == 1
    assert announcer.calls == []


async def test_a_greeting_buys_extra_listening_rather_than_extra_transmissions() -> None:
    """An acknowledgement returning over a longer path than the one we sent on is
    late, not absent — and believing it absent costs a flood advert."""
    sender = RecordingSender()
    bot = greeter(sender=sender, config={"ack_grace_seconds": 45})

    await bot.dispatch(advert(hop_count=0))

    assert sender.graces == [45.0]


async def test_a_late_acknowledgement_inside_the_grace_stops_the_escalation() -> None:
    """The grace window exists to prevent exactly this flood."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender(result=SendResult.ACKNOWLEDGED)
    bot = greeter(sender=sender, announcer=announcer, config={"max_hops": 2})

    await bot.dispatch(advert(hop_count=1))

    assert announcer.calls == [], "the acknowledgement arrived, so nothing was flooded"
    assert len(sender.sent) == 1


async def test_a_direct_neighbour_is_adverted_to_on_every_attempt() -> None:
    """Cheap enough to repeat: it stops at direct neighbours and costs the mesh
    nothing."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender(result=SendResult.UNACKNOWLEDGED)
    clock = FakeClock()
    bot = greeter(sender=sender, announcer=announcer, clock=clock)
    contact = contact_for()

    await bot.dispatch(advert(contact, hop_count=0))
    clock.advance(minutes=15)
    await bot.dispatch(advert(contact, hop_count=0))

    assert announcer.calls == [False, False]


async def test_the_advert_is_awaited_before_the_greeting_is_composed() -> None:
    """An advert is class 3 and a message is class 2, so a send issued without
    waiting would overtake the advert that makes it readable."""
    order: list[str] = []
    announcer = RecordingAnnouncer()

    async def announce(flood: bool) -> bool:
        order.append("advert")
        return await announcer(flood)

    async def send(contact: Contact, text: str, grace: float = 0.0):
        order.append("greeting")
        return await RecordingSender()(contact, text)

    bot = greeter(sender=send, announcer=announce)  # type: ignore[arg-type]
    await bot.dispatch(advert(hop_count=0))

    assert order == ["advert", "greeting"]


async def test_an_observe_mode_bot_adverts_nothing() -> None:
    """An advert is a transmission, and a dry run that put one on the air would
    not be a dry run."""
    announcer = RecordingAnnouncer()
    sender = RecordingSender()
    bot = greeter(mode="observe", sender=sender, announcer=announcer)

    await bot.dispatch(advert(hop_count=0))

    assert announcer.calls == []
    assert sender.sent == []
    assert bot.counters.announces == 0


# --- 8.8 The greeting text --------------------------------------------------


def test_an_over_long_greeting_is_refused_with_the_limit_and_the_length() -> None:
    """8.8, design D14: refused when it is typed, not at 3am when it matters."""
    from sighop.bots.base import BotConfigError

    too_long = "x" * (MAX_TEXT_LEN + 1)

    with pytest.raises(BotConfigError) as excinfo:
        GreeterBot.validate_config("greeting", too_long)

    assert str(MAX_TEXT_LEN) in str(excinfo.value)
    assert str(len(too_long.encode())) in str(excinfo.value)
    assert "never truncated" in str(excinfo.value)


def test_a_greeting_is_measured_in_bytes_and_not_characters() -> None:
    """8.8: the limit is the firmware's `MAX_TEXT_LEN`, which counts bytes."""
    from sighop.bots.base import BotConfigError

    # 80 characters, 160 bytes: exactly at the limit.
    assert GreeterBot.validate_config("greeting", "å" * 80) == "å" * 80
    with pytest.raises(BotConfigError):
        GreeterBot.validate_config("greeting", "å" * 81)


async def test_a_sent_greeting_is_exactly_the_configured_text() -> None:
    """8.8: never truncated and never split at send time."""
    text = "welcome to the mesh, from a node that heard you first"
    sender = RecordingSender()
    bot = greeter(sender=sender, config={"greeting": text})

    await bot.dispatch(advert())

    assert sender.sent[0][1] == text


# --- 8.9 Counters -----------------------------------------------------------


async def test_one_advert_into_each_gate_moves_each_counter() -> None:
    """8.9: a greeter that is greeting nobody says which bound is doing it."""
    sender = RecordingSender()
    storage = MemoryBotStorage()
    bot = greeter(sender=sender, storage=storage, config={"min_snr_db": 0.0})

    await bot.dispatch(advert(hop_count=9))
    await bot.dispatch(advert(contact_for("hilltop", NodeType.REPEATER)))
    await bot.dispatch(advert(snr_db=-20.0))
    greeted = contact_for()
    await bot.dispatch(advert(greeted))
    await bot.dispatch(advert(greeted))

    assert bot.counters.suppressions == {
        str(SuppressionReason.TOO_MANY_HOPS): 1,
        str(SuppressionReason.NODE_TYPE): 1,
        str(SuppressionReason.LOW_SNR): 1,
        str(SuppressionReason.ALREADY_GREETED): 1,
    }
    assert bot.counters.actions == 1
    assert bot.driver.counters() == {
        "greetings_sent": 1,
        "greetings_acknowledged": 1,
        "greetings_observed": 0,
        "greetings_retried": 0,
    }


async def test_an_observed_greeting_is_counted_as_an_observation_not_an_action() -> None:
    """8.9 / 7.5: the two must never merge, or a dry run reads as a live one."""
    bot = greeter(mode="observe")

    await bot.dispatch(advert())

    assert bot.counters.observations == 1
    assert bot.counters.actions == 0
    assert bot.driver.counters()["greetings_observed"] == 1


# --- Configuration validation ------------------------------------------------


def test_the_driver_refuses_a_setting_it_does_not_have_by_listing_what_it_takes() -> None:
    from sighop.bots.base import BotConfigError

    with pytest.raises(BotConfigError) as excinfo:
        GreeterBot.validate_config("colour", "blue")

    assert "greeting" in str(excinfo.value)
    assert "max_hops" in str(excinfo.value)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("max_hops", "3", 3),
        ("node_types", "CHAT,ROOM_SERVER", [1, 3]),
        ("node_types", "1", [1]),
        ("min_snr_db", "-4.5", -4.5),
        ("min_snr_db", "none", None),
    ],
)
def test_a_configured_value_is_parsed_into_what_the_driver_reads(
    key: str, value: str, expected: object
) -> None:
    assert GreeterBot.validate_config(key, value) == expected


@pytest.mark.parametrize(
    ("key", "value"),
    [("max_hops", "many"), ("max_hops", "-1"), ("node_types", ""), ("node_types", "SATELLITE")],
)
def test_a_value_the_driver_cannot_use_is_refused(key: str, value: str) -> None:
    from sighop.bots.base import BotConfigError

    with pytest.raises(BotConfigError):
        GreeterBot.validate_config(key, value)


async def test_the_greeter_answers_a_direct_message_with_silence() -> None:
    """One hello, then listening: a driver that replied would turn one
    unsolicited message into a conversation nobody asked for."""
    from sighop.bots.base import DirectMessageEvent

    sender = RecordingSender()
    bot = greeter(sender=sender)

    await bot.dispatch(
        DirectMessageEvent(
            contact=contact_for(),
            text=WireText.from_bytes(b"hello?"),
            timestamp=0,
            packet_id="p",
        )
    )

    assert sender.sent == []
    assert bot.counters.failures == 0
