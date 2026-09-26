"""Repeater collection storage: settings, selection, and poll history (repeater-metrics 2.3)."""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from dataclasses import replace

import pytest

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Outcome, Succeeded
from sighop.db.repositories import (
    CollectionSettings,
    CollectionSettingsError,
    EntityRepository,
    PollOutcome,
    PollRecord,
    RepeaterCollectionRepository,
    RepeaterPollRepository,
    RepeaterTargetRepository,
    RoomRepository,
    UnknownIdentityError,
)
from sighop.passwords import hash_password
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NeighbourEntry, NodeType, RepeaterStats

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
SECRET = base64.b64decode(generate_secret_key())
REPEATER = b"\x42" * 32
OTHER = b"\x43" * 32

STATS = RepeaterStats(
    batt_milli_volts=4012,
    curr_tx_queue_len=1,
    noise_floor=-110,
    last_rssi=-87,
    n_packets_recv=1000,
    n_packets_sent=900,
    total_air_time_secs=3600,
    total_up_time_secs=86400,
    n_sent_flood=1,
    n_sent_direct=2,
    n_recv_flood=3,
    n_recv_direct=4,
    err_events=0,
    last_snr=-26,
    n_direct_dups=5,
    n_flood_dups=6,
    total_rx_air_time_secs=7200,
    n_recv_errors=None,
)


async def _identity(
    database: Database, name: str, node_type: NodeType = NodeType.CHAT
) -> uuid.UUID:
    outcome = await EntityRepository(database=database).store(
        name=name, identity=generate_identity(), secret=SECRET, node_type=node_type
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value.id


def _value[T](outcome: Outcome[T]) -> T:
    assert isinstance(outcome, Succeeded), outcome
    return outcome.value


# --- Settings ---------------------------------------------------------------


async def test_a_fresh_database_reads_the_defaults(database: Database) -> None:
    settings = _value(await RepeaterCollectionRepository(database=database).get())
    assert settings == CollectionSettings()
    assert (settings.enabled, settings.entity_id) == (False, None)
    assert (settings.interval_minutes, settings.recent_days, settings.retention_days) == (60, 3, 30)


async def test_valid_settings_are_stored_and_read_back(database: Database) -> None:
    identity = await _identity(database, "collector")
    repository = RepeaterCollectionRepository(database=database)
    saved = _value(
        await repository.save(
            enabled=True, entity_id=identity, interval_minutes=30, recent_days=2, retention_days=14
        )
    )
    assert _value(await repository.get()) == saved
    assert (saved.enabled, saved.entity_id, saved.interval_minutes) == (True, identity, 30)


async def test_keeping_forever_is_stored_and_read_back_as_no_bound(database: Database) -> None:
    repository = RepeaterCollectionRepository(database=database)
    saved = _value(
        await repository.save(
            enabled=False, entity_id=None, interval_minutes=60, recent_days=3, retention_days=None
        )
    )
    assert saved.retention_days is None
    assert _value(await repository.get()).retention_days is None


async def test_enabling_without_an_identity_is_refused_and_nothing_stored(
    database: Database,
) -> None:
    repository = RepeaterCollectionRepository(database=database)
    with pytest.raises(CollectionSettingsError, match="without a login identity"):
        await repository.save(
            enabled=True, entity_id=None, interval_minutes=60, recent_days=3, retention_days=30
        )
    assert _value(await repository.get()) == CollectionSettings()


@pytest.mark.parametrize(
    ("interval", "recent", "retention", "words"),
    [
        (2, 3, 30, "5 to 1440"),
        (60, 0, 30, "1 to 365"),
        (60, 3, 0, "1 to 365"),
        (60, 3, 366, "1 to 365"),
    ],
)
async def test_out_of_range_values_are_refused(
    database: Database, interval: int, recent: int, retention: int, words: str
) -> None:
    repository = RepeaterCollectionRepository(database=database)
    with pytest.raises(CollectionSettingsError, match=words):
        await repository.save(
            enabled=False,
            entity_id=None,
            interval_minutes=interval,
            recent_days=recent,
            retention_days=retention,
        )
    assert _value(await repository.get()) == CollectionSettings()


async def test_a_room_identity_is_refused(database: Database) -> None:
    room_identity = await _identity(database, "lounge-id", NodeType.ROOM_SERVER)
    created = await RoomRepository(database=database).create(
        entity_id=room_identity, name="Lounge", admin_password_hash=hash_password("admin")
    )
    assert isinstance(created, Succeeded)
    repository = RepeaterCollectionRepository(database=database)
    with pytest.raises(CollectionSettingsError, match="serving the room 'Lounge'"):
        await repository.save(
            enabled=False,
            entity_id=room_identity,
            interval_minutes=60,
            recent_days=3,
            retention_days=30,
        )


async def test_an_identity_not_stored_is_refused(database: Database) -> None:
    with pytest.raises(UnknownIdentityError):
        await RepeaterCollectionRepository(database=database).save(
            enabled=False,
            entity_id=uuid.uuid4(),
            interval_minutes=60,
            recent_days=3,
            retention_days=30,
        )


async def test_removing_the_login_identity_clears_it(database: Database) -> None:
    """The identity removed → collection has no identity, in the schema."""
    entities = EntityRepository(database=database)
    stored = _value(
        await entities.store(
            name="collector", identity=generate_identity(), secret=SECRET, node_type=NodeType.CHAT
        )
    )
    repository = RepeaterCollectionRepository(database=database)
    await repository.save(
        enabled=True, entity_id=stored.id, interval_minutes=60, recent_days=3, retention_days=30
    )
    assert _value(await entities.remove(stored.public_key)) is True, "removal is not blocked"
    settings = _value(await repository.get())
    assert (settings.enabled, settings.entity_id) == (True, None)


async def test_a_cycle_summary_is_recorded_beside_the_settings(database: Database) -> None:
    repository = RepeaterCollectionRepository(database=database)
    await repository.record_cycle(
        started_at=NOW, finished_at=NOW + dt.timedelta(seconds=40), polled=3, succeeded=2
    )
    settings = _value(await repository.get())
    assert settings.last_cycle_started_at == NOW
    assert (settings.last_cycle_polled, settings.last_cycle_succeeded) == (3, 2)
    assert settings.enabled is False, "recording a cycle leaves the settings at their defaults"


# --- Selection --------------------------------------------------------------


async def test_selection_is_stored_by_key_and_removable(database: Database) -> None:
    targets = RepeaterTargetRepository(database=database)
    assert _value(await targets.select(REPEATER, at=NOW)) is True
    assert _value(await targets.select(REPEATER, at=NOW)) is False, "already selected"
    await targets.select(OTHER, at=NOW)
    assert _value(await targets.list_keys()) == frozenset({REPEATER, OTHER})
    assert _value(await targets.deselect(OTHER)) is True
    assert _value(await targets.list_keys()) == frozenset({REPEATER})


# --- Polls ------------------------------------------------------------------


def _poll(at: dt.datetime, outcome: PollOutcome, **kwargs: object) -> PollRecord:
    return PollRecord(
        public_key=REPEATER,
        started_at=at,
        outcome=outcome,
        route="FLOOD",
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_poll_round_trips_with_status_and_neighbours(database: Database) -> None:
    polls = RepeaterPollRepository(database=database)
    neighbours = (
        NeighbourEntry(prefix=b"\x01" * 6, heard_seconds_ago=30, snr=40),
        NeighbourEntry(prefix=b"\x02" * 6, heard_seconds_ago=600, snr=-10),
    )
    await polls.record(
        _poll(NOW, PollOutcome.SUCCEEDED, stats=STATS, neighbours_total=2, neighbours=neighbours)
    )
    latest = _value(await polls.latest_with_status(REPEATER))
    assert latest is not None
    assert latest.stats == STATS
    assert latest.stats.n_recv_errors is None
    assert latest.neighbours == neighbours
    assert latest.neighbours_total == 2


async def test_latest_with_status_skips_a_later_failed_poll(database: Database) -> None:
    polls = RepeaterPollRepository(database=database)
    await polls.record(_poll(NOW, PollOutcome.SUCCEEDED, stats=STATS, neighbours_total=0))
    await polls.record(_poll(NOW + dt.timedelta(hours=1), PollOutcome.LOGIN_UNANSWERED))
    latest = _value(await polls.latest_with_status(REPEATER))
    assert latest is not None and latest.started_at == NOW

    newest = _value(await polls.latest_for([REPEATER, OTHER]))
    assert set(newest) == {REPEATER}
    assert newest[REPEATER].outcome is PollOutcome.LOGIN_UNANSWERED

    history = _value(await polls.history(REPEATER))
    assert [p.outcome for p in history] == [PollOutcome.LOGIN_UNANSWERED, PollOutcome.SUCCEEDED]


async def test_history_is_capped(database: Database) -> None:
    polls = RepeaterPollRepository(database=database)
    for minutes in range(5):
        await polls.record(_poll(NOW + dt.timedelta(minutes=minutes), PollOutcome.NOT_SENT))
    history = _value(await polls.history(REPEATER, limit=3))
    assert [p.started_at for p in history] == [NOW + dt.timedelta(minutes=m) for m in (4, 3, 2)]


async def test_pruning_deletes_old_polls_and_their_neighbours(database: Database) -> None:
    polls = RepeaterPollRepository(database=database)
    old = NOW - dt.timedelta(days=31)
    entry = NeighbourEntry(prefix=b"\x01" * 6, heard_seconds_ago=1, snr=1)
    await polls.record(
        _poll(old, PollOutcome.SUCCEEDED, stats=STATS, neighbours_total=1, neighbours=(entry,))
    )
    await polls.record(
        _poll(NOW, PollOutcome.SUCCEEDED, stats=STATS, neighbours_total=1, neighbours=(entry,))
    )
    assert _value(await polls.prune_older_than(NOW - dt.timedelta(days=30))) == 1
    history = _value(await polls.history(REPEATER))
    assert [p.started_at for p in history] == [NOW]

    from sqlalchemy import text

    async with database.sessions() as session:
        left = (await session.execute(text("SELECT count(*) FROM repeater_neighbour"))).scalar()
    assert left == 1, "the old poll's neighbours went with it"


async def test_latest_status_per_key_is_not_hidden_by_a_newer_failed_poll(
    database: Database,
) -> None:
    polls = RepeaterPollRepository(database=database)
    await polls.record(_poll(NOW, PollOutcome.SUCCEEDED, stats=STATS, neighbours_total=0))
    await polls.record(_poll(NOW + dt.timedelta(hours=1), PollOutcome.LOGIN_UNANSWERED))
    other = replace(STATS, batt_milli_volts=3600)
    await polls.record(replace(_poll(NOW, PollOutcome.SUCCEEDED, stats=other), public_key=OTHER))
    await polls.record(
        replace(
            _poll(NOW - dt.timedelta(hours=1), PollOutcome.SUCCEEDED, stats=STATS),
            public_key=OTHER,
        )
    )
    latest = _value(await polls.latest_status_for([REPEATER, OTHER, b"\x44" * 32]))
    assert set(latest) == {REPEATER, OTHER}
    assert latest[REPEATER].started_at == NOW
    assert latest[REPEATER].stats == STATS
    assert latest[OTHER].stats is not None and latest[OTHER].stats.batt_milli_volts == 3600
    assert _value(await polls.latest_status_for([])) == {}


async def test_one_poll_is_read_by_id_with_its_neighbours(database: Database) -> None:
    polls = RepeaterPollRepository(database=database)
    entry = NeighbourEntry(prefix=b"\x01" * 6, heard_seconds_ago=30, snr=40)
    first = _value(
        await polls.record(
            _poll(NOW, PollOutcome.SUCCEEDED, stats=STATS, neighbours_total=1, neighbours=(entry,))
        )
    )
    await polls.record(_poll(NOW + dt.timedelta(hours=1), PollOutcome.SUCCEEDED, stats=STATS))
    poll = _value(await polls.get(first))
    assert poll is not None
    assert (poll.id, poll.started_at, poll.stats) == (first, NOW, STATS)
    assert poll.neighbours == (entry,)
    assert _value(await polls.get(first + 1000)) is None


async def test_the_series_is_oldest_first_bounded_by_since_and_keeps_failed_polls(
    database: Database,
) -> None:
    polls = RepeaterPollRepository(database=database)
    for hours in (3, 1, 2):
        await polls.record(
            _poll(
                NOW - dt.timedelta(hours=hours),
                PollOutcome.SUCCEEDED,
                stats=STATS,
                neighbours_total=hours,
            )
        )
    await polls.record(_poll(NOW, PollOutcome.LOGIN_UNANSWERED))
    await polls.record(replace(_poll(NOW, PollOutcome.SUCCEEDED, stats=STATS), public_key=OTHER))

    everything = _value(await polls.series(REPEATER, since=None))
    assert [row.started_at for row in everything] == [
        NOW - dt.timedelta(hours=h) for h in (3, 2, 1, 0)
    ]
    first = everything[0]
    assert (first.batt_milli_volts, first.last_snr_db, first.neighbours_total) == (4012, -6.5, 3)
    assert (first.n_packets_recv, first.total_up_time_secs) == (1000, 86400)
    failed = everything[-1]
    assert failed.outcome is PollOutcome.LOGIN_UNANSWERED
    assert (failed.batt_milli_volts, failed.n_packets_recv, failed.neighbours_total) == (
        None,
        None,
        None,
    )

    recent = _value(await polls.series(REPEATER, since=NOW - dt.timedelta(hours=1)))
    assert [row.started_at for row in recent] == [NOW - dt.timedelta(hours=1), NOW]


async def test_a_poll_records_its_resend_count_and_an_uncounted_one_reads_none(
    database: Database,
) -> None:
    polls = RepeaterPollRepository(database=database)
    counted = _value(await polls.record(_poll(NOW, PollOutcome.SUCCEEDED, stats=STATS, retries=2)))
    uncounted = _value(await polls.record(_poll(NOW, PollOutcome.LOGIN_UNANSWERED)))
    first = _value(await polls.get(counted))
    second = _value(await polls.get(uncounted))
    assert first is not None and first.retries == 2
    assert second is not None and second.retries is None


async def test_last_answered_is_the_newest_poll_that_got_past_the_login(
    database: Database,
) -> None:
    polls = RepeaterPollRepository(database=database)
    silent = b"\x44" * 32
    for hours, outcome in (
        (5, PollOutcome.SUCCEEDED),
        (3, PollOutcome.STATUS_UNANSWERED),
        (1, PollOutcome.LOGIN_UNANSWERED),
    ):
        await polls.record(_poll(NOW - dt.timedelta(hours=hours), outcome))
    await polls.record(
        replace(_poll(NOW, PollOutcome.NEIGHBOURS_INCOMPLETE, stats=STATS), public_key=OTHER)
    )
    for outcome in (PollOutcome.LOGIN_UNANSWERED, PollOutcome.NOT_SENT):
        await polls.record(replace(_poll(NOW, outcome), public_key=silent))

    assert _value(await polls.last_answered()) == {
        REPEATER: NOW - dt.timedelta(hours=3),
        OTHER: NOW,
    }
