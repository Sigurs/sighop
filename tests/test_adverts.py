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
    assert summary["stubs"][0]["override_interval_seconds"] == 300  # type: ignore[index]


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
    verified = verify_advert(parse_advert(payload))
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
