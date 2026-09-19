"""Advert stubs and policy (milestone 3, `advert-policy`, design D15/D16).

An advert's cost lands on other people's networks, so most of these tests are
about *not* transmitting: the floor that refuses a fast interval, the override
that expires by itself, the gap that stops two local entities bursting together,
and the stagger that stops startup from being a burst of its own.

The adverts built here are real and signed, and are verified through the same
`protocol/` verifier the RX path uses — a stub that emitted adverts no MeshCore
node would accept would prove nothing about the budget's load.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from sighop.net.adverts import (
    DAY,
    FLOOD_INTERVAL_FLOOR_SECONDS,
    HOUR,
    MAX_OVERRIDE_SECONDS,
    AdvertPolicyError,
    AdvertScheduler,
    build_advert_packet,
    configure_interval,
)
from sighop.net.bus import PriorityClass, Submission, TxHandle, TxOutcome, TxResult
from sighop.protocol.crypto import VerifiedAdvert, verify_advert
from sighop.protocol.identity import generate_identity
from sighop.protocol.packet import PayloadType, RouteType
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.payloads import NodeType, parse_advert
from sighop.protocol.result import DecodeFailure
from tests.test_tx import ManualClock, RecordingLogger


class CollectingSink:
    """Stands in for the bus: records submissions, resolves them immediately."""

    def __init__(self) -> None:
        self.submissions: list[Submission] = []

    def __call__(self, submission: Submission) -> TxHandle:
        self.submissions.append(submission)
        handle = TxHandle(submission)
        handle.resolve(
            TxOutcome(
                result=TxResult.SUPPRESSED,
                packet_id="stub",
                airtime_ms=0.0,
                queue_wait_ms=0.0,
                attempts=1,
            )
        )
        return handle


def scheduler(
    clock: ManualClock,
    *,
    sink: CollectingSink | None = None,
    logger: RecordingLogger | None = None,
    seed: int = 7,
    **kwargs: object,
) -> AdvertScheduler:
    return AdvertScheduler(
        submit=sink or CollectingSink(),
        clock=clock,
        rng=random.Random(seed),
        logger=logger or RecordingLogger(),
        **kwargs,  # type: ignore[arg-type]
    )


# --- The floor -------------------------------------------------------------


def test_the_default_flood_interval_is_the_24_hour_floor() -> None:
    clock = ManualClock()
    sched = scheduler(clock)

    stub = sched.add_stub("skogen")

    assert stub.flood_interval_seconds == FLOOD_INTERVAL_FLOOR_SECONDS == DAY


def test_an_interval_below_the_floor_is_refused() -> None:
    clock = ManualClock()
    sched = scheduler(clock)

    with pytest.raises(AdvertPolicyError, match="floor"):
        sched.add_stub("hasty", flood_interval_seconds=HOUR)

    assert sched.stubs == []


def test_reconfiguring_below_the_floor_is_refused() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")

    with pytest.raises(AdvertPolicyError, match="floor"):
        configure_interval(stub, 6 * HOUR)

    assert configure_interval(stub, 48 * HOUR).flood_interval_seconds == 48 * HOUR


def test_zero_hop_adverts_are_disabled_by_default() -> None:
    clock = ManualClock()
    sched = scheduler(clock)

    stub = sched.add_stub("skogen")

    assert stub.zero_hop_interval_seconds == 0.0


# --- Stagger and jitter ----------------------------------------------------


async def test_startup_schedules_nothing_immediately() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    for index in range(4):
        sched.add_stub(f"stub-{index}")

    sched.tick()

    assert sink.submissions == []


def test_first_adverts_are_staggered_across_the_interval() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stubs = [sched.add_stub(f"stub-{index}") for index in range(5)]

    offsets = [
        (stub.next_flood_at - clock.now()).total_seconds()
        for stub in stubs
        if stub.next_flood_at is not None
    ]

    assert len(offsets) == 5
    assert all(0 <= offset <= FLOOD_INTERVAL_FLOOR_SECONDS for offset in offsets)
    assert len(set(offsets)) == 5, "every entity drew its own offset"


async def test_jitter_differs_per_entity_at_the_same_interval() -> None:
    clock = ManualClock()
    sched = scheduler(clock, min_entity_gap_seconds=0.0)
    first = sched.add_stub("a")
    second = sched.add_stub("b")

    # Force both due, then let them reschedule from the same base interval.
    first.next_flood_at = clock.now()
    second.next_flood_at = clock.now()
    sched.tick()

    assert first.next_flood_at != second.next_flood_at
    for stub in (first, second):
        assert stub.next_flood_at is not None
        gap = (stub.next_flood_at - clock.now()).total_seconds()
        assert 0.75 * DAY <= gap <= 1.25 * DAY


# --- The inter-entity gap --------------------------------------------------


async def test_two_entities_falling_due_together_are_spaced_apart() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    logger = RecordingLogger()
    sched = scheduler(clock, sink=sink, logger=logger, min_entity_gap_seconds=600.0)
    first = sched.add_stub("a")
    second = sched.add_stub("b")
    first.next_flood_at = clock.now()
    second.next_flood_at = clock.now()

    sched.tick()

    assert len(sink.submissions) == 1
    assert sched.deferrals == 1
    assert logger.of("advert_deferred_for_entity_gap")
    assert second.next_flood_at == clock.now() + dt.timedelta(seconds=600)


async def test_the_deferred_entity_adverts_once_the_gap_has_passed() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink, min_entity_gap_seconds=600.0)
    first = sched.add_stub("a")
    second = sched.add_stub("b")
    first.next_flood_at = clock.now()
    second.next_flood_at = clock.now()
    sched.tick()

    clock.advance(601)
    sched.tick()

    assert len(sink.submissions) == 2
    assert {s.entity_name for s in sink.submissions} == {"a", "b"}


# --- Overrides -------------------------------------------------------------


def test_an_override_without_an_expiry_gets_the_one_hour_default() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")

    override = sched.set_override(stub, interval_seconds=300)

    assert override.expires_at == clock.now() + dt.timedelta(seconds=HOUR)


def test_an_override_beyond_24_hours_is_refused() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")

    with pytest.raises(AdvertPolicyError, match="no permanent override"):
        sched.set_override(stub, interval_seconds=300, expires_in=MAX_OVERRIDE_SECONDS + 1)


async def test_an_active_override_shortens_the_interval() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink, min_entity_gap_seconds=0.0)
    stub = sched.add_stub("skogen")
    sched.set_override(stub, interval_seconds=300, expires_in=HOUR)

    for _ in range(6):
        clock.advance(400)
        sched.tick()

    assert len(sink.submissions) >= 4, "the override did not take effect"


async def test_an_override_reverts_to_the_floor_when_it_expires() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    sched = scheduler(clock, logger=logger, min_entity_gap_seconds=0.0)
    stub = sched.add_stub("skogen")
    sched.set_override(stub, interval_seconds=300, expires_in=HOUR)

    clock.advance(HOUR + 1)
    sched.tick()

    assert stub.override is None
    assert logger.of("advert_override_expired")
    assert stub.next_flood_at is not None
    gap = (stub.next_flood_at - clock.now()).total_seconds()
    assert gap >= 0.75 * DAY, "reverted to something faster than the floor"


def test_an_active_override_is_visible_for_the_status_line() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")
    sched.set_override(stub, interval_seconds=300)

    assert sched.active_overrides() == [stub]
    summary = sched.as_json()
    assert summary["active_overrides"] == 1
    assert summary["stubs"][0]["override_interval_seconds"] == 300


# --- The adverts themselves ------------------------------------------------


def test_a_stub_advert_verifies_through_the_real_verifier() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen", node_type=NodeType.ROOM_SERVER)

    raw = build_advert_packet(stub, int(clock.now().timestamp()))
    packet = decode_packet(raw)
    assert not isinstance(packet, DecodeFailure), packet
    advert = parse_advert(packet.payload)
    assert not isinstance(advert, DecodeFailure), advert
    verification = verify_advert(advert)

    assert isinstance(verification, VerifiedAdvert)
    assert verification.appdata.name is not None
    assert verification.appdata.name.text == "skogen"
    assert verification.appdata.node_type is NodeType.ROOM_SERVER
    assert verification.public_key == stub.identity.public_key


def test_an_advert_is_a_flood_packet_with_no_path() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")

    packet = decode_packet(build_advert_packet(stub, int(clock.now().timestamp())))

    assert not isinstance(packet, DecodeFailure)
    assert packet.route_type is RouteType.FLOOD
    assert packet.payload_type is PayloadType.ADVERT
    assert packet.hop_count == 0
    assert packet.path == b""


async def test_adverts_are_submitted_as_class_three_with_a_deadline() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink, min_entity_gap_seconds=0.0)
    stub = sched.add_stub("skogen")
    stub.next_flood_at = clock.now()

    sched.tick()

    (submission,) = sink.submissions
    assert submission.priority is PriorityClass.ADVERT
    assert submission.origin == "advert"
    assert submission.entity_type == "stub"
    assert submission.deadline > clock.now()
    assert submission.deadline <= clock.now() + dt.timedelta(seconds=300)


async def test_a_dropped_advert_waits_for_its_next_scheduled_time() -> None:
    """No retry outside the schedule: the next advert is already booked."""
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink, min_entity_gap_seconds=0.0)
    stub = sched.add_stub("skogen")
    stub.next_flood_at = clock.now()
    sched.tick()

    clock.advance(3600)
    sched.tick()

    assert len(sink.submissions) == 1
    assert stub.adverts_sent == 1


# --- Stubs -----------------------------------------------------------------


def test_stub_identities_never_collide_on_node_hash() -> None:
    """DESIGN.md §3: self-inflicted ambiguity between our own entities."""
    clock = ManualClock()
    sched = scheduler(clock)

    stubs = [sched.add_stub(f"stub-{index}") for index in range(12)]

    hashes = [stub.node_hash for stub in stubs]
    assert len(set(hashes)) == len(hashes)


def test_stubs_are_marked_ephemeral_wherever_they_are_rendered() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")

    assert stub.as_json()["ephemeral"] is True
    assert stub.as_json()["entity_type"] == "stub"


# --- Persistent identities and the one-shot zero-hop advert (milestone 4) ---


async def test_a_loaded_identity_adverts_under_the_same_rules_as_a_stub() -> None:
    from sighop.protocol.identity import generate_identity

    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    identity = generate_identity()

    stub = sched.add_identity("skogen", identity, keyfile="/tmp/skogen.json")

    assert stub.persistent is True
    assert stub.identity is identity
    assert stub.flood_interval_seconds == FLOOD_INTERVAL_FLOOR_SECONDS
    assert stub.next_flood_at is not None, "a loaded identity was not staggered"
    assert stub.as_json()["ephemeral"] is False
    assert stub.as_json()["entity_type"] == "entity"

    # Its adverts are signed with the stored key and verify as any other do.
    stub.next_flood_at = clock.now()
    sched.tick()
    payload = decode_packet(sink.submissions[0].packet).payload
    parsed = parse_advert(payload)
    assert not isinstance(parsed, DecodeFailure)
    verified = verify_advert(parsed)
    assert isinstance(verified, VerifiedAdvert)
    assert verified.public_key == identity.public_key


async def test_a_loaded_identity_and_a_stub_share_the_inter_entity_gap() -> None:
    from sighop.protocol.identity import generate_identity

    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    loaded = sched.add_identity("persistent-one", generate_identity())
    stub = sched.add_stub("ephemeral-one")
    loaded.next_flood_at = stub.next_flood_at = clock.now()

    sched.tick()

    assert len(sink.submissions) == 1, "two entities advertised in one tick"
    assert sched.deferrals == 1


def test_an_identity_colliding_with_a_registered_entity_is_refused() -> None:
    from sighop.protocol.identity import generate_identity

    clock = ManualClock()
    sched = scheduler(clock)
    first = sched.add_stub("skogen")
    while True:
        twin = generate_identity()
        if twin.node_hash == first.node_hash:
            break

    with pytest.raises(AdvertPolicyError, match=f"0x{first.node_hash:02x}"):
        sched.add_identity("twin", twin)


async def test_a_one_shot_zero_hop_advert_is_direct_class_three_and_charged() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    stub = sched.add_stub("skogen")
    scheduled_before = stub.next_flood_at
    adverts_before = stub.adverts_sent

    sched.request_zero_hop(stub)

    assert len(sink.submissions) == 1
    submission = sink.submissions[0]
    packet = decode_packet(submission.packet)
    assert submission.priority is PriorityClass.ADVERT
    assert submission.origin == "advert_zero_hop"
    # DIRECT rather than FLOOD: it reaches direct neighbours and stops there.
    assert packet.route_type is RouteType.DIRECT
    assert packet.payload_type is PayloadType.ADVERT
    assert packet.hop_count == 0 and packet.path == b""
    # And it changes nothing about the schedule it was not part of.
    assert stub.next_flood_at == scheduled_before
    assert stub.adverts_sent == adverts_before
    assert stub.zero_hop_interval_seconds == 0.0
    assert sched.last_global_flood_at is None, "a zero-hop advert consumed the flood gap"


async def test_a_one_shot_zero_hop_advert_verifies_like_any_other() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    stub = sched.add_stub("skogen")

    sched.request_zero_hop(stub)

    payload = decode_packet(sink.submissions[0].packet).payload
    parsed = parse_advert(payload)
    assert not isinstance(parsed, DecodeFailure)
    verified = verify_advert(parsed)
    assert isinstance(verified, VerifiedAdvert)
    assert verified.appdata.name is not None
    assert verified.appdata.name.text == "skogen"


# --- The inter-entity gap, readable (web-advert-now) ------------------------


def test_the_gap_remaining_is_zero_before_any_flood() -> None:
    clock = ManualClock()
    sched = scheduler(clock, min_entity_gap_seconds=600.0)
    sched.add_stub("skogen")

    assert sched.flood_gap_remaining(clock.now()) == 0.0


async def test_the_gap_remaining_counts_down_from_a_scheduled_flood() -> None:
    clock = ManualClock()
    sched = scheduler(clock, min_entity_gap_seconds=600.0)
    stub = sched.add_stub("skogen")
    stub.next_flood_at = clock.now()
    sched.tick()

    clock.advance(250)

    assert sched.flood_gap_remaining(clock.now()) == 350.0
    clock.advance(400)
    assert sched.flood_gap_remaining(clock.now()) == 0.0


async def test_the_gap_remaining_counts_a_requested_flood() -> None:
    clock = ManualClock()
    sched = scheduler(clock, min_entity_gap_seconds=600.0)
    stub = sched.add_stub("skogen")
    sched.request_flood(stub)

    clock.advance(100)

    assert sched.flood_gap_remaining(clock.now()) == 500.0


async def test_a_zero_hop_request_alone_leaves_the_gap_at_zero() -> None:
    clock = ManualClock()
    sched = scheduler(clock, min_entity_gap_seconds=600.0)
    stub = sched.add_stub("skogen")
    sched.request_zero_hop(stub)

    assert sched.flood_gap_remaining(clock.now()) == 0.0


# --- A single flood advert on explicit request ------------------------------


async def test_a_requested_flood_is_one_class_three_submission_with_a_deadline() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    stub = sched.add_stub("skogen")

    sched.request_flood(stub)

    (submission,) = sink.submissions
    packet = decode_packet(submission.packet)
    assert not isinstance(packet, DecodeFailure)
    assert packet.route_type is RouteType.FLOOD
    assert submission.priority is PriorityClass.ADVERT
    assert submission.origin == "advert_flood_requested"
    assert clock.now() < submission.deadline <= clock.now() + dt.timedelta(seconds=300)
    assert stub.adverts_sent == 1
    assert stub.last_flood_at == clock.now()


async def test_a_requested_flood_moves_the_schedule_one_jittered_interval_out() -> None:
    clock = ManualClock()
    sched = scheduler(clock, seed=11)
    stub = sched.add_stub("skogen")
    stub.next_flood_at = clock.now() + dt.timedelta(hours=3)

    clock.advance(60)
    sched.request_flood(stub)

    # The same draws the scheduler made: the stagger, then the jitter.
    replay = random.Random(11)
    replay.uniform(0.0, DAY)
    expected = DAY + replay.uniform(-0.25 * DAY, 0.25 * DAY)
    assert stub.next_flood_at == clock.now() + dt.timedelta(seconds=expected)
    assert stub.next_flood_at > clock.now() + dt.timedelta(hours=3), (
        "the scheduled flood was left in place behind the requested one"
    )


async def test_another_entity_due_inside_the_gap_of_a_requested_flood_is_deferred() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    logger = RecordingLogger()
    sched = scheduler(clock, sink=sink, logger=logger, min_entity_gap_seconds=600.0)
    first = sched.add_stub("a")
    second = sched.add_stub("b")
    sched.request_flood(first)

    clock.advance(120)
    second.next_flood_at = clock.now()
    sched.tick()

    assert len(sink.submissions) == 1
    assert logger.of("advert_deferred_for_entity_gap")[0]["entity_name"] == "b"
    assert second.next_flood_at == clock.now() + dt.timedelta(seconds=480)


class DroppingSink(CollectingSink):
    """A bus whose every submission expires on its deadline."""

    def __call__(self, submission: Submission) -> TxHandle:
        self.submissions.append(submission)
        handle = TxHandle(submission)
        handle.resolve(
            TxOutcome(
                result=TxResult.DROPPED,
                packet_id="stub",
                airtime_ms=0.0,
                queue_wait_ms=0.0,
                attempts=0,
            )
        )
        return handle


async def test_a_dropped_requested_flood_is_not_resubmitted_before_its_schedule() -> None:
    clock = ManualClock()
    sink = DroppingSink()
    sched = scheduler(clock, sink=sink, min_entity_gap_seconds=0.0)
    stub = sched.add_stub("skogen")
    sched.request_flood(stub)
    assert stub.next_flood_at is not None
    booked = stub.next_flood_at

    for _ in range(12):
        clock.advance(3600)
        if clock.now() >= booked:
            break
        sched.tick()

    assert len(sink.submissions) == 1


async def test_a_requested_flood_changes_no_override_and_no_interval() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    plain = sched.add_stub("plain")
    overridden = sched.add_stub("fast")
    override = sched.set_override(overridden, 2 * HOUR, expires_in=HOUR)

    sched.request_flood(plain)
    sched.request_flood(overridden)

    assert plain.override is None
    assert plain.flood_interval_seconds == FLOOD_INTERVAL_FLOOR_SECONDS
    assert overridden.override is override
    assert overridden.flood_interval_seconds == FLOOD_INTERVAL_FLOOR_SECONDS


# --- Renaming a loaded identity ----------------------------------------------
#
# A rename has to reach the air, because the name *is* on the air: it travels
# in the appdata of every advert. What it must not reach is the schedule.


def test_a_rename_changes_the_name_the_next_advert_carries() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")

    assert sched.rename(stub.identity.public_key, "skogen-2") is True

    advert = parse_advert(decode_packet(build_advert_packet(stub, 1)).payload)
    assert not isinstance(advert, DecodeFailure)
    verification = verify_advert(advert)
    assert isinstance(verification, VerifiedAdvert)
    assert verification.appdata.name is not None
    assert verification.appdata.name.text == "skogen-2"
    assert verification.public_key == stub.identity.public_key


def test_a_rename_mutates_the_stub_every_consumer_already_holds() -> None:
    """runtime.py hands `adverts.stubs` to the messengers, the rooms and the
    bots, so the rename has to land on the object rather than replace it."""
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")
    held = sched.stubs[0]

    sched.rename(stub.identity.public_key, "skogen-2")

    assert sched.stubs[0] is held, "the stub was replaced rather than renamed"
    assert held.name == "skogen-2"
    assert stub.name == "skogen-2"


def test_a_rename_leaves_the_schedule_exactly_as_it_was() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_stub("skogen")
    sched.set_override(stub, interval_seconds=HOUR)
    before = (stub.next_flood_at, stub.last_flood_at, stub.adverts_sent, stub.override)

    sched.rename(stub.identity.public_key, "skogen-2")

    assert (
        stub.next_flood_at,
        stub.last_flood_at,
        stub.adverts_sent,
        stub.override,
    ) == before


def test_a_rename_submits_nothing() -> None:
    clock = ManualClock()
    sink = CollectingSink()
    sched = scheduler(clock, sink=sink)
    stub = sched.add_stub("skogen")

    sched.rename(stub.identity.public_key, "skogen-2")

    assert sink.submissions == [], "a rename put a packet on the air"


def test_the_entity_id_follows_a_rename_when_it_was_the_name() -> None:
    """`_adopt_entity` passes no id, so `entity_id` *is* the name (design D2)."""
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_identity("skogen", generate_identity())
    assert stub.entity_id == "skogen"

    sched.rename(stub.identity.public_key, "skogen-2")

    assert stub.entity_id == "skogen-2"


def test_an_explicit_entity_id_survives_a_rename() -> None:
    """One that was never the name is an id in its own right, and is kept."""
    clock = ManualClock()
    sched = scheduler(clock)
    stub = sched.add_identity("skogen", generate_identity(), entity_id="stable-id")

    sched.rename(stub.identity.public_key, "skogen-2")

    assert stub.entity_id == "stable-id"
    assert stub.name == "skogen-2"


def test_renaming_an_identity_this_run_does_not_hold_reports_so() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    sched.add_stub("skogen")

    assert sched.rename(generate_identity().public_key, "nobody") is False


def test_a_name_another_loaded_identity_holds_is_reported_as_a_clash() -> None:
    """A keyfile identity is loaded and not stored, so the entity store's own
    duplicate check cannot see this case."""
    clock = ManualClock()
    sched = scheduler(clock)
    first = sched.add_stub("skogen")
    second = sched.add_stub("greeter")

    clash = sched.name_clash(first.identity.public_key, "greeter")

    assert clash is second
    assert sched.name_clash(first.identity.public_key, "skogen") is None, (
        "an identity clashes with itself"
    )
    assert sched.name_clash(first.identity.public_key, "unused") is None
