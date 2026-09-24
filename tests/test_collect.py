"""The repeater collector against a scripted repeater (repeater-metrics 4.2-4.6).

No Postgres: storage is `tests/collectfixtures.py`'s, and the far end is a
`FakeRepeater` answering the way `simple_repeater/MyMesh.cpp` does.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.db.repositories import CollectionSettings, PollOutcome, PollRecord
from sighop.net.bus import PriorityClass, TxResult
from sighop.net.collect import is_eligible
from sighop.net.paths import LearnedPath, PathKey
from sighop.net.rx import decode_event
from sighop.protocol.crypto import SharedSecretCache
from sighop.protocol.packet import PayloadType, RouteType
from sighop.protocol.payloads import NeighbourEntry, NodeType
from sighop.radio.modem import RxEvent, RxMeta
from tests.collectfixtures import STATS, FakeRepeater, repeater_contact, rig
from tests.test_dm import START, zero_hop_route_to

# --- 4.2 Eligibility --------------------------------------------------------


def _eligible(**kwargs: object) -> bool:
    repeater = FakeRepeater()
    contact = repeater_contact(repeater.identity, **kwargs)  # type: ignore[arg-type]
    return is_eligible(
        contact, selected=frozenset({repeater.identity.public_key}), recent_days=3, now=START
    )


def test_a_selected_repeater_heard_yesterday_is_eligible() -> None:
    assert _eligible(last_heard=START - dt.timedelta(days=1))


def test_a_selected_repeater_silent_for_a_week_is_not() -> None:
    assert not _eligible(last_heard=START - dt.timedelta(days=7))


def test_a_contact_that_is_not_a_repeater_is_not() -> None:
    assert not _eligible(node_type=NodeType.CHAT)
    assert not _eligible(node_type=NodeType.ROOM_SERVER)


def test_an_unselected_repeater_is_not() -> None:
    repeater = FakeRepeater()
    assert not is_eligible(repeater.contact, selected=frozenset(), recent_days=3, now=START)


async def test_a_silent_repeater_gets_no_packet_and_no_record() -> None:
    repeater = FakeRepeater()
    r = rig(
        repeater=repeater,
        contact=repeater_contact(repeater.identity, last_heard=START - dt.timedelta(days=7)),
    )
    await r.collector.tick()
    assert r.repeater.submissions == []
    assert r.polls.polls == []
    assert r.settings.cycles[-1]["polled"] == 0


async def test_an_unselected_repeater_gets_no_packet() -> None:
    r = rig(selected=False)
    await r.collector.tick()
    assert r.repeater.submissions == []


# --- 4.3 The exchange -------------------------------------------------------


def _only_poll(polls: list[PollRecord]) -> PollRecord:
    assert len(polls) == 1, polls
    return polls[0]


async def test_a_complete_poll_logs_in_blank_asks_status_and_neighbours() -> None:
    repeater = FakeRepeater(
        neighbours=[NeighbourEntry(bytes([i]) * 32, 60 * i, 4 * i) for i in range(3)]
    )
    r = rig(repeater=repeater)
    await r.collector.tick()

    poll = _only_poll(r.polls.polls)
    assert poll.outcome is PollOutcome.SUCCEEDED
    assert poll.stats == STATS
    assert poll.neighbours_total == 3
    assert [n.prefix for n in poll.neighbours] == [bytes([i]) * 6 for i in range(3)]
    assert [step for step, _, _ in repeater.requests] == ["login", "status", "neighbours"]
    assert all(s.priority is PriorityClass.ADVERT for s in repeater.submissions)
    assert r.settings.cycles[-1]["polled"] == 1
    assert r.settings.cycles[-1]["succeeded"] == 1


async def test_a_flooded_login_is_answered_by_path_return_and_the_rest_goes_direct() -> None:
    """Flood answered by path return: the route is learned and used next."""
    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    await r.collector.tick()

    routes = [route for _, route, _ in repeater.requests]
    assert routes == [RouteType.FLOOD, RouteType.DIRECT, RouteType.DIRECT]
    learned = r.paths.lookup_public_key(repeater.identity.public_key)
    assert learned is not None and learned.path == b"\x77"
    assert _only_poll(r.polls.polls).route == "FLOOD → DIRECT h1"


async def test_a_known_route_goes_direct_from_the_login() -> None:
    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    zero_hop_route_to(r.paths, repeater.identity.public_key)
    await r.collector.tick()
    assert [route for _, route, _ in repeater.requests] == [RouteType.DIRECT] * 3
    assert _only_poll(r.polls.polls).outcome is PollOutcome.SUCCEEDED


async def test_a_node_hash_route_is_not_trusted_and_the_login_floods() -> None:
    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    r.paths._insert(
        PathKey.for_node_hash(repeater.identity.node_hash),
        LearnedPath(
            path=b"\x01",
            hash_size=1,
            hop_count=1,
            snr_db=1.0,
            confirmed_at=START,
            packet_id="seed",
        ),
    )
    await r.collector.tick()
    assert repeater.requests[0][1] is RouteType.FLOOD


async def test_twenty_five_neighbours_take_three_pages() -> None:
    repeater = FakeRepeater(neighbours=[NeighbourEntry(bytes([i]) * 32, i, i) for i in range(25)])
    r = rig(repeater=repeater)
    await r.collector.tick()
    offsets = [offset for step, _, offset in repeater.requests if step == "neighbours"]
    assert offsets == [0, 11, 22]
    poll = _only_poll(r.polls.polls)
    assert poll.outcome is PollOutcome.SUCCEEDED
    assert len(poll.neighbours) == 25


async def test_a_guest_password_leaves_the_login_unanswered() -> None:
    repeater = FakeRepeater(guest_password_set=True)
    r = rig(repeater=repeater)
    await r.collector.tick()
    poll = _only_poll(r.polls.polls)
    assert poll.outcome is PollOutcome.LOGIN_UNANSWERED
    assert [step for step, _, _ in repeater.requests] == ["login"]


async def test_an_unanswered_status_ends_the_poll_before_neighbours() -> None:
    repeater = FakeRepeater(silent={"status"})
    r = rig(repeater=repeater)
    await r.collector.tick()
    poll = _only_poll(r.polls.polls)
    assert poll.outcome is PollOutcome.STATUS_UNANSWERED
    assert poll.stats is None
    assert [step for step, _, _ in repeater.requests] == ["login", "status"]


async def test_neighbours_cut_short_keep_the_status_and_the_first_page() -> None:
    repeater = FakeRepeater(
        neighbours=[NeighbourEntry(bytes([i]) * 32, i, i) for i in range(25)],
        silent={"neighbours:11"},
    )
    r = rig(repeater=repeater)
    await r.collector.tick()
    poll = _only_poll(r.polls.polls)
    assert poll.outcome is PollOutcome.NEIGHBOURS_INCOMPLETE
    assert poll.stats == STATS
    assert len(poll.neighbours) == 11
    assert poll.neighbours_total == 25


async def test_timestamps_to_one_repeater_strictly_increase_across_polls() -> None:
    r = rig(interval_minutes=5)
    await r.collector.tick()
    first = dict(r.collector._last_sent)
    r.clock._now = START  # the clock going nowhere must not repeat a timestamp
    r.settings.update(last_cycle_started_at=None)
    r.collector._last_started = None
    await r.collector.tick()
    assert r.collector._last_sent[r.repeater.identity.public_key] > max(first.values())
    assert [p.outcome for p in r.polls.polls] == [PollOutcome.SUCCEEDED] * 2


# --- 4.4 Matching -----------------------------------------------------------


async def test_a_late_answer_to_an_earlier_poll_is_unmatched_and_records_nothing() -> None:
    repeater = FakeRepeater(silent=set())
    r = rig(repeater=repeater)
    zero_hop_route_to(r.paths, repeater.identity.public_key)

    def hold_status(step: str) -> None:
        if step == "status":
            repeater.hold_next = True

    repeater.on_request = hold_status
    await r.collector.tick()
    assert _only_poll(r.polls.polls).outcome is PollOutcome.STATUS_UNANSWERED

    target, record = repeater.delayed.pop()
    before = r.collector.responses_unmatched
    await target.handle(record)
    assert r.collector.responses_unmatched == before + 1
    assert len(r.polls.polls) == 1


async def test_a_response_from_another_repeater_is_not_accepted() -> None:
    """Matched to the outstanding repeater's secret only, never a trial."""
    stranger = FakeRepeater()
    r = rig()
    stranger.collector = r.collector
    r.contacts._insert(stranger.contact)
    # A stray RESPONSE addressed to our identity, with nothing outstanding.
    secret = SharedSecretCache().get(stranger.identity, r.entity.identity.public_key)
    packet = stranger._datagram(r.entity, secret, PayloadType.RESPONSE, bytes(16), flood=False)
    record = decode_event(
        RxEvent(packet=packet, rx_meta=RxMeta(snr_db=1.0, rssi_dbm=-1), received_at=START),
        received_at=START,
    )
    await r.collector.handle(record)
    assert r.collector.responses_unmatched == 1
    assert r.collector.responses_matched == 0


# --- 4.5 The loop -----------------------------------------------------------


async def test_cycles_run_on_the_default_interval() -> None:
    r = rig()
    await r.collector.tick()
    assert len(r.polls.polls) == 1
    r.clock.advance(59 * 60)
    await r.collector.tick()
    assert len(r.polls.polls) == 1, "not due before the hour"
    started = r.settings.current.last_cycle_started_at
    assert started is not None
    r.clock._now = started + dt.timedelta(minutes=60)
    await r.collector.tick()
    assert len(r.polls.polls) == 2


async def test_a_changed_interval_applies_at_the_next_tick() -> None:
    r = rig()
    await r.collector.tick()
    started = r.settings.current.last_cycle_started_at
    assert started is not None
    r.settings.update(interval_minutes=15)
    r.clock._now = started + dt.timedelta(minutes=15)
    await r.collector.tick()
    assert len(r.polls.polls) == 2


async def test_a_long_cycle_delays_the_next_rather_than_overlapping() -> None:
    """The next cycle is due from when the last *started*, but a tick never
    starts one while another runs: cycles run inside the tick."""
    r = rig(interval_minutes=5)
    r.repeater.on_request = lambda step: r.clock.advance(6 * 60)  # every step is slow
    await r.collector.tick()
    finished = r.settings.current.last_cycle_finished_at
    started = r.settings.current.last_cycle_started_at
    assert finished is not None and started is not None
    assert finished - started > dt.timedelta(minutes=5)
    await r.collector.tick()  # already due: starts now, after the first finished
    second_start = r.settings.cycles[-2]["started_at"]
    assert isinstance(second_start, dt.datetime) and second_start >= finished


async def test_disabling_mid_cycle_stops_before_the_next_repeater() -> None:
    first, second = FakeRepeater(), FakeRepeater()
    r = rig(repeater=first)
    while second.identity.node_hash == first.identity.node_hash:
        second = FakeRepeater()
    r.contacts._insert(second.contact)
    r.targets.keys.add(second.identity.public_key)
    second.collector = r.collector
    second.path_bodies = first.path_bodies
    r.collector.submit = _route_to(first, second)
    polled: list[bytes] = []

    def disable(step: str) -> None:
        if step == "login":
            r.settings.update(enabled=False)

    first.on_request = disable
    second.on_request = disable
    await r.collector.tick()
    polled = [p.public_key for p in r.polls.polls]
    assert len(polled) == 1, "the repeater already started finishes; no other is polled"


def _route_to(*repeaters: FakeRepeater):
    """One scheduler in front of several fakes: each packet to its addressee."""

    def submit(submission):
        record = decode_event(
            RxEvent(packet=submission.packet, rx_meta=RxMeta(snr_db=1.0, rssi_dbm=-1)),
            received_at=START,
        )
        dest = record.outcome.payload.dest_hash
        addressee = next(r for r in repeaters if r.identity.node_hash == dest)
        return addressee(submission)

    return submit


async def test_transmission_disabled_sends_and_records_nothing() -> None:
    r = rig()
    r.transmit[0] = False
    await r.collector.tick()
    assert r.repeater.submissions == []
    assert r.polls.polls == []
    assert "transmission is disabled" in str(r.settings.cycles[-1]["note"])
    r.transmit[0] = True
    await r.collector.tick()
    assert r.polls.polls == [], "retried at the next interval, not the next tick"
    r.clock.advance(60 * 60)
    await r.collector.tick()
    assert len(r.polls.polls) == 1


@pytest.mark.parametrize("result", [TxResult.DROPPED, TxResult.SUPPRESSED])
async def test_a_login_the_scheduler_did_not_send_is_recorded_not_sent(result: TxResult) -> None:
    repeater = FakeRepeater(submit_results=[result])
    r = rig(repeater=repeater)
    await r.collector.tick()
    poll = _only_poll(r.polls.polls)
    assert poll.outcome is PollOutcome.NOT_SENT
    assert poll.reason in {"deadline_expired", "transmit disabled"}


async def test_no_identity_skips_the_cycle_with_the_reason() -> None:
    r = rig()
    r.settings.update(entity_id=None)
    await r.collector.tick()
    assert r.repeater.submissions == []
    assert "no login identity" in str(r.settings.cycles[-1]["note"])


async def test_a_room_claimed_identity_is_not_used() -> None:
    r = rig()
    r.collector.claim_for_room(r.entity.entity_id)
    await r.collector.tick()
    assert r.repeater.submissions == []
    assert "serving a room" in str(r.settings.cycles[-1]["note"])


async def test_an_unreadable_settings_row_keeps_the_last_good_settings() -> None:
    r = rig()
    await r.collector.tick()
    r.settings.fail = True
    r.clock.advance(61 * 60)
    await r.collector.tick()
    assert len(r.polls.polls) == 2


async def test_unreadable_settings_before_any_good_read_do_nothing() -> None:
    r = rig()
    r.settings.fail = True
    await r.collector.tick()
    assert r.repeater.submissions == []


# --- 4.6 Retention ----------------------------------------------------------


def _old(r, days: float) -> PollRecord:
    return PollRecord(
        public_key=r.repeater.identity.public_key,
        started_at=r.clock.now() - dt.timedelta(days=days),
        outcome=PollOutcome.SUCCEEDED,
        route="FLOOD",
    )


async def test_pruning_deletes_old_polls_and_keeps_newer_ones() -> None:
    r = rig()
    r.polls.polls.extend([_old(r, 31), _old(r, 29)])
    await r.collector.tick()
    kept = [p.started_at for p in r.polls.polls]
    assert r.clock.now() - min(kept) < dt.timedelta(days=30)
    assert len(kept) == 2, "the 29-day-old poll and the new one"


async def test_pruning_runs_daily_while_disabled() -> None:
    r = rig(enabled=False)
    await r.collector.tick()
    r.polls.polls.append(_old(r, 31))
    r.clock.advance(3600)
    await r.collector.tick()
    assert len(r.polls.polls) == 1, "not yet: pruned less than a day ago"
    r.clock.advance(24 * 3600)
    await r.collector.tick()
    assert r.polls.polls == []


async def test_retention_follows_the_settings() -> None:
    r = rig(enabled=False)
    r.settings.update(retention_days=7)
    r.polls.polls.extend([_old(r, 8), _old(r, 6)])
    await r.collector.tick()
    assert len(r.polls.polls) == 1


def test_defaults_match_the_spec() -> None:
    settings = CollectionSettings()
    assert (settings.interval_minutes, settings.recent_days, settings.retention_days) == (60, 3, 30)


# --- Flooded answers: settle, then tell the repeater our route --------------


def test_the_settle_time_has_a_floor_and_grows_with_the_packet() -> None:
    from sighop.net.collect import FLOOD_SETTLE_FLOOR_MS, flood_settle_ms
    from sighop.radio.modem import EU868_NARROW

    assert flood_settle_ms(10, EU868_NARROW) == FLOOD_SETTLE_FLOOR_MS
    assert flood_settle_ms(150, EU868_NARROW) > flood_settle_ms(76, EU868_NARROW) > 3000


async def test_a_flooded_answer_is_followed_by_a_path_return_and_then_direct_answers() -> None:
    """The live failure: a repeater with no route to us floods every answer, and
    the next request went out into the re-floods. A stock client returns the
    route the flood took; the repeater then answers direct."""
    repeater = FakeRepeater(neighbours=[NeighbourEntry(bytes([i]) * 32, i, i) for i in range(3)])
    r = rig(repeater=repeater)
    zero_hop_route_to(r.paths, repeater.identity.public_key)
    await r.collector.tick()

    assert _only_poll(r.polls.polls).outcome is PollOutcome.SUCCEEDED
    assert repeater.answered_by_flood == [True, False, False], "only the login was flooded"
    assert repeater.path_returns == [b"\x77"], "the route the flood took, not reversed"
    assert r.collector.path_returns == 1


async def test_nothing_is_sent_until_the_re_floods_have_died_down() -> None:
    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    zero_hop_route_to(r.paths, repeater.identity.public_key)
    await r.collector.tick()

    origins = [origin for origin, _ in repeater.submitted_at]
    assert origins[:3] == ["repeater_login", "repeater_path_return", "repeater_status"]
    login_at, returned_at = repeater.submitted_at[0][1], repeater.submitted_at[1][1]
    assert returned_at - login_at >= dt.timedelta(seconds=3)


def test_the_relay_clear_time_is_nothing_zero_hop_and_grows_per_relay() -> None:
    from sighop.net.collect import relay_clear_ms
    from sighop.radio.modem import EU868_NARROW

    one = relay_clear_ms(25, 1, EU868_NARROW)
    assert relay_clear_ms(25, 0, EU868_NARROW) == 0
    assert relay_clear_ms(25, 3, EU868_NARROW) == pytest.approx(3 * one)
    assert one > 2.5 * 400, "a relay's worst-case delay plus its own send"


async def test_the_request_after_a_path_return_waits_for_the_relay_to_forward_it() -> None:
    """The live failure: a status request sent under a second after a one-hop
    path return reached the relay while it was still forwarding the return."""
    from sighop.net.collect import relay_clear_ms
    from sighop.radio.modem import EU868_NARROW

    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    await r.collector.tick()

    origins = [origin for origin, _ in repeater.submitted_at]
    assert origins[1:3] == ["repeater_path_return", "repeater_status"]
    returned_at, status_at = repeater.submitted_at[1][1], repeater.submitted_at[2][1]
    assert repeater.path_returns == [b"\x77"], "returned down a one-hop route"
    assert status_at - returned_at >= dt.timedelta(
        milliseconds=relay_clear_ms(10, 1, EU868_NARROW)  # no path return is smaller
    )


async def test_a_flooded_login_answered_by_path_return_also_returns_our_route() -> None:
    """No route known: the login floods, its answer rides a flooded path return,
    and the repeater — which forgot our route on the flooded login — is told it."""
    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    await r.collector.tick()

    assert repeater.path_returns == [b"\x77"]
    assert repeater.answered_by_flood == [True, False, False]


async def test_a_repeater_that_kept_our_route_needs_no_path_return_next_poll() -> None:
    repeater = FakeRepeater()
    r = rig(repeater=repeater)
    zero_hop_route_to(r.paths, repeater.identity.public_key)
    await r.collector.tick()
    r.clock.advance(61 * 60)
    await r.collector.tick()

    assert [p.outcome for p in r.polls.polls] == [PollOutcome.SUCCEEDED] * 2
    assert repeater.path_returns == [b"\x77"], "one path return, in the first poll only"
    assert repeater.answered_by_flood[3:] == [False, False, False]
