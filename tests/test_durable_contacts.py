"""Durable contacts (section 5, design D2/D12/D15/D16).

Contacts are the one store where losing a row is expensive: re-acquiring one
means waiting for the peer to advert again, and the advert floor is 24 h. That
asymmetry is what every test here is about — the queue that refuses rather than
drops, the marker that carries what the queue could not take, and the backfill
that fires on the database's own probe rather than on the next write.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import text

from sighop.db.engine import Database, Failed, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import ContactRepository
from sighop.net.contacts import Contact, ContactStore
from sighop.protocol.crypto import AdvertVerificationFailure, sign_advert, verify_advert
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.payloads import NodeType, build_appdata

NOW = dt.datetime(2026, 9, 5, 20, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(hours=24)


def verified_advert(
    identity: LocalIdentity,
    name: str,
    *,
    timestamp: int = 1_700_000_000,
    node_type: NodeType = NodeType.CHAT,
    location: tuple[int, int] | None = None,
):
    """A signed advert whose verification succeeded.

    `location` is how a test changes the advert's *flags* without inventing a
    value: the wire format derives them from the node type plus the lat/lon and
    feature masks, so setting a location is what a real flag change looks like.
    """
    latitude, longitude = location or (None, None)
    appdata = build_appdata(node_type, name=name, latitude=latitude, longitude=longitude)
    verification = verify_advert(sign_advert(identity, timestamp, appdata))
    assert not isinstance(verification, AdvertVerificationFailure)
    return verification


class _Sink:
    """A contact sink a test drives: accept, refuse, or record what it took."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.offered: list[Contact] = []

    def offer(self, contact: Contact) -> bool:
        self.offered.append(contact)
        return self.accept


# --- 5.1 The repository -----------------------------------------------------


@pytest.mark.database
async def test_a_contact_round_trips_with_its_flags_timestamps_and_marker(
    database: Database,
) -> None:
    repository = ContactRepository(database=database)
    identity = generate_identity()
    advert = verified_advert(
        identity, "skogen", node_type=NodeType.ROOM_SERVER, location=(60_000_000, 24_000_000)
    )
    contact = Contact(
        public_key=identity.public_key,
        name=advert.appdata.name,
        node_type=NodeType.ROOM_SERVER,
        flags=advert.appdata.flags,
        first_heard=NOW,
        last_heard=LATER,
        advert_verified=True,
    )
    assert isinstance(await repository.upsert(contact), Succeeded)

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    (restored,) = loaded.value
    assert restored.public_key == identity.public_key
    assert restored.name is not None and restored.name.text == "skogen"
    assert restored.node_type is NodeType.ROOM_SERVER
    assert restored.flags == advert.appdata.flags
    assert restored.first_heard == NOW
    assert restored.last_heard == LATER
    assert restored.advert_verified is True


@pytest.mark.database
async def test_an_upsert_advances_last_heard_and_leaves_first_heard_alone(
    database: Database,
) -> None:
    repository = ContactRepository(database=database)
    identity = generate_identity()
    await repository.upsert(
        Contact(
            public_key=identity.public_key,
            first_heard=NOW,
            last_heard=NOW,
            advert_verified=True,
        )
    )
    await repository.upsert(
        Contact(
            public_key=identity.public_key,
            first_heard=LATER,
            last_heard=LATER,
            advert_verified=True,
        )
    )
    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    (restored,) = loaded.value
    assert restored.first_heard == NOW, "re-hearing a peer moved first_heard"
    assert restored.last_heard == LATER


# --- 5.2 Writes follow observed change, not receptions ----------------------


def test_repeated_copies_of_one_advert_produce_one_write() -> None:
    """A flood advert reaches us over several paths; that is one thing learned."""
    sink = _Sink()
    store = ContactStore(sink=sink)
    identity = generate_identity()
    advert = verified_advert(identity, "skogen", timestamp=1_700_000_000)

    for index in range(10):
        store.observe_advert(advert, at=NOW + dt.timedelta(seconds=index))

    assert len(sink.offered) == 1, "the write rate followed receptions, not changes"


def test_a_later_advert_from_the_same_peer_is_a_new_write() -> None:
    sink = _Sink()
    store = ContactStore(sink=sink)
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen", timestamp=1), at=NOW)
    store.observe_advert(verified_advert(identity, "skogen", timestamp=2), at=LATER)
    assert len(sink.offered) == 2


def test_a_rename_and_a_flag_change_are_each_a_write() -> None:
    sink = _Sink()
    store = ContactStore(sink=sink)
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen", timestamp=1), at=NOW)
    store.observe_advert(verified_advert(identity, "hamnen", timestamp=1), at=NOW)
    store.observe_advert(
        verified_advert(identity, "hamnen", timestamp=1, location=(60_000_000, 24_000_000)),
        at=NOW,
    )
    assert len(sink.offered) == 3


def test_with_no_sink_nothing_is_offered_and_nothing_is_marked() -> None:
    store = ContactStore()
    store.observe_advert(verified_advert(generate_identity(), "skogen"), at=NOW)
    assert store.awaiting_backfill == 0
    assert len(store) == 1


# --- 5.3 Restoration --------------------------------------------------------


def test_a_restored_peer_resolves_by_name_and_by_key_prefix_before_any_traffic() -> None:
    identity = generate_identity()
    stored = Contact(
        public_key=identity.public_key,
        name=verified_advert(identity, "skogen").appdata.name,
        first_heard=NOW,
        last_heard=NOW,
        advert_verified=True,
    )
    store = ContactStore()
    assert store.restore([stored]) == 1

    assert store.resolve("skogen").public_key == identity.public_key
    assert store.resolve(identity.public_key.hex()[:8]).public_key == identity.public_key
    assert store.by_node_hash(identity.node_hash) == frozenset({stored})


def test_restoring_does_not_re_offer_the_contacts_it_just_read() -> None:
    sink = _Sink()
    store = ContactStore(sink=sink)
    identity = generate_identity()
    store.restore([Contact(public_key=identity.public_key, first_heard=NOW, last_heard=NOW)])
    assert sink.offered == []
    assert store.awaiting_backfill == 0


def test_net_contacts_imports_no_sqlalchemy() -> None:
    """Design D10: the persistent path is an adapter, not a rewrite."""
    import ast
    from pathlib import Path

    import sighop.net.contacts as module

    source = Path(module.__file__).read_text()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any(name.split(".")[0] in {"sqlalchemy", "asyncpg"} for name in names)
    assert not any(name.startswith("sighop.db") for name in names)


# --- 5.4 / 5.9 A failed write keeps the contact ----------------------------


def test_a_refused_write_leaves_the_contact_addressable_and_marked() -> None:
    sink = _Sink(accept=False)
    store = ContactStore(sink=sink)
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)

    assert store.resolve("skogen").public_key == identity.public_key
    assert store.awaiting_backfill == 1
    assert store.writes_refused == 1


def test_the_marker_clears_on_success_and_a_redundant_clear_is_harmless() -> None:
    sink = _Sink()
    store = ContactStore(sink=sink)
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)
    contact = store.get(identity.public_key)
    assert contact is not None
    assert store.awaiting_backfill == 1, "queued is not written"

    store.mark_persisted(contact)
    assert store.awaiting_backfill == 0
    store.mark_persisted(contact)  # idempotent: the upsert is too
    assert store.awaiting_backfill == 0

    store.mark_unpersisted(contact)
    store.mark_unpersisted(contact)
    assert store.awaiting_backfill == 1


# --- 5.5 The manual/verified distinction ------------------------------------


@pytest.mark.database
async def test_a_manually_added_contact_stays_unverified_across_the_round_trip(
    database: Database,
) -> None:
    repository = ContactRepository(database=database)
    identity = generate_identity()
    memory = ContactStore()
    contact = memory.add_public_key(identity.public_key)
    assert isinstance(await repository.upsert(contact), Succeeded)

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    (restored,) = loaded.value
    assert restored.advert_verified is False
    assert restored.name is None, "a restored manual contact gained a name it never had"


@pytest.mark.database
async def test_a_later_verified_advert_upgrades_a_stored_manual_contact(
    database: Database,
) -> None:
    repository = ContactRepository(database=database)
    identity = generate_identity()
    seed_store = ContactStore()
    await repository.upsert(seed_store.add_public_key(identity.public_key))

    # A new run: restore, then hear the peer for the first time.
    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    store = ContactStore()
    store.restore(loaded.value)
    advert = verified_advert(identity, "skogen", location=(60_000_000, 24_000_000))
    observation = store.observe_advert(advert, at=NOW)
    assert observation.changed is True
    assert isinstance(await repository.upsert(observation.contact), Succeeded)

    reloaded = await repository.load_all()
    assert isinstance(reloaded, Succeeded)
    (upgraded,) = reloaded.value
    assert upgraded.advert_verified is True
    assert upgraded.name is not None and upgraded.name.text == "skogen"
    assert upgraded.flags == advert.appdata.flags


# --- 5.6 An unverified advert writes nothing --------------------------------


@pytest.mark.database
async def test_a_badly_signed_advert_writes_no_row(database: Database) -> None:
    """The rule is unchanged under persistence, and it is the type that holds it:
    a verification failure carries no advert content to record at all."""
    repository = ContactRepository(database=database)
    failure = AdvertVerificationFailure(reason="bad_signature", node_hash=0x42)
    assert not hasattr(failure, "public_key")

    async with database.sessions() as session:
        rows = (await session.execute(text("SELECT count(*) FROM contact"))).scalar_one()
    assert rows == 0
    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    assert loaded.value == []


# --- 5.7 Durability across an ungraceful stop -------------------------------


@pytest.mark.database
async def test_a_contact_whose_write_landed_survives_a_process_that_never_stopped(
    database: Database, database_config
) -> None:
    """No graceful shutdown, no flush: the row is there because it was written
    when the advert was observed, which is what design D2 buys."""
    persistence = Persistence(database=database)
    store = ContactStore(sink=persistence.contact_sink())
    persistence.attach_contacts(store)
    persistence.start()

    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)
    await asyncio.wait_for(persistence.contact_writer.wait_idle(), timeout=5)
    assert store.awaiting_backfill == 0

    # The process dies here: no `stop()`, no flush, no dispose.
    reader = ContactRepository(database=database)
    loaded = await reader.load_all()
    assert isinstance(loaded, Succeeded)
    assert [contact.public_key for contact in loaded.value] == [identity.public_key]


# --- 5.8 The write is off the subscriber path -------------------------------


async def test_the_advert_subscriber_returns_without_waiting_for_the_write() -> None:
    """`offer` is the whole of what the subscriber does (design D16)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(batch: list[Contact]) -> bool:
        started.set()
        await release.wait()
        return True

    from sighop.db.writer import WriteBehind

    writer: WriteBehind[Contact] = WriteBehind("contacts", slow, capacity=4, drop_oldest=False)
    writer.start()
    store = ContactStore(sink=writer)
    try:
        loop = asyncio.get_running_loop()
        before = loop.time()
        store.observe_advert(verified_advert(generate_identity(), "skogen"), at=NOW)
        assert loop.time() - before < 0.05
        await asyncio.wait_for(started.wait(), timeout=2)
        assert store.awaiting_backfill == 1
    finally:
        release.set()
        await writer.stop()


async def test_an_overflowed_queue_leaves_every_contact_marked_unpersisted() -> None:
    from sighop.db.writer import WriteBehind

    async def never_drains(batch: list[Contact]) -> bool:  # pragma: no cover
        return True

    writer: WriteBehind[Contact] = WriteBehind(
        "contacts", never_drains, capacity=2, drop_oldest=False
    )
    store = ContactStore(sink=writer)
    for index in range(6):
        store.observe_advert(verified_advert(generate_identity(), f"peer-{index}"), at=NOW)

    assert writer.overflowed == 0, "the contact queue must refuse, not drop"
    assert writer.pending == 2
    assert store.awaiting_backfill == 6, "an overflowed contact lost its marker"


# --- 5.10 / 5.11 The backfill -----------------------------------------------


@pytest.mark.database
async def test_several_observations_during_an_outage_backfill_as_one_row(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    store = ContactStore(sink=persistence.contact_sink())
    persistence.attach_contacts(store)

    identity = generate_identity()
    for timestamp in (1, 2, 3):
        store.observe_advert(
            verified_advert(identity, f"skogen-{timestamp}", timestamp=timestamp),
            at=NOW + dt.timedelta(hours=timestamp),
        )
    # Nothing has been written: the writer task was never started, which is the
    # same position an outage leaves the queue in.
    assert store.awaiting_backfill == 1, "three observations of one peer, one marker"

    await persistence.backfill_contacts()

    loaded = await ContactRepository(database=database).load_all()
    assert isinstance(loaded, Succeeded)
    assert len(loaded.value) == 1, "the backfill replayed observations instead of state"
    (row,) = loaded.value
    assert row.name is not None and row.name.text == "skogen-3", "not the latest state"
    assert store.awaiting_backfill == 0


@pytest.mark.database
async def test_the_backfill_needs_no_restart_and_no_further_advert(
    database: Database,
) -> None:
    """The probe fires the recovery hook; nothing else has to happen."""
    persistence = Persistence(database=database)
    store = ContactStore(sink=persistence.contact_sink())
    persistence.attach_contacts(store)

    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)
    database.degraded = True

    assert await database.probe_once() is True
    assert database.degraded is False

    loaded = await ContactRepository(database=database).load_all()
    assert isinstance(loaded, Succeeded)
    assert [contact.public_key for contact in loaded.value] == [identity.public_key]


@pytest.mark.database
async def test_a_restart_during_the_outage_loses_what_was_never_written(
    database: Database,
) -> None:
    """5.11: the backfill closes the recovery window, not the crash window.

    Design D15 states this residual rather than designing it away: closing it
    too would take a local write-ahead file — a second store to reason about and
    a second thing to corrupt, for a case a restart already tolerates.
    """
    persistence = Persistence(database=database)
    store = ContactStore(sink=persistence.contact_sink())
    persistence.attach_contacts(store)

    observed = generate_identity()
    store.observe_advert(verified_advert(observed, "unwritten"), at=NOW)
    assert store.awaiting_backfill == 1

    # The process restarts here, still with no database: the marker set dies
    # with it, and so does the contact.
    restarted = ContactStore()
    loaded = await ContactRepository(database=database).load_all()
    assert isinstance(loaded, Succeeded)
    restored = restarted.restore(loaded.value)

    assert restored == 0
    assert restarted.get(observed.public_key) is None
    assert restarted.restored == 0, "the restored count claimed what was observed"


# --- 5.12 The backfill is not extended to paths or the packet log -----------


@pytest.mark.database
async def test_a_discarded_route_and_log_row_are_not_rewritten_on_recovery(
    database: Database,
) -> None:
    """Design D15's asymmetry is the point: contacts are backfilled because they
    are the only store where the loss is expensive."""
    from sighop.net.paths import LearnedPath, PathKey

    persistence = Persistence(database=database)
    store = ContactStore(sink=persistence.contact_sink())
    persistence.attach_contacts(store)

    key = PathKey.for_public_key(generate_identity().public_key)
    learned = LearnedPath(
        path=b"\x01\x02",
        hash_size=1,
        hop_count=2,
        snr_db=None,
        confirmed_at=NOW,
        packet_id="p1",
    )
    # Simulate what an outage does: the queues took them and the flush failed,
    # so the rows are gone and only the counters remember.
    persistence.database.stats.routes_discarded += 1
    persistence.database.stats.packet_log_discarded += 1
    assert learned.hop_count == 2 and key.ambiguous is False

    database.degraded = True
    assert await database.probe_once() is True

    async with database.sessions() as session:
        paths = (await session.execute(text("SELECT count(*) FROM path"))).scalar_one()
        logs = (await session.execute(text("SELECT count(*) FROM packet_log"))).scalar_one()
    assert paths == 0, "the recovery rewrote a discarded route"
    assert logs == 0, "the recovery rewrote a discarded packet-log row"
    assert persistence.database.stats.routes_discarded == 1
    assert persistence.database.stats.packet_log_discarded == 1


@pytest.mark.database
async def test_a_failed_contact_flush_leaves_the_marker_set(database: Database) -> None:
    persistence = Persistence(database=database)
    store = ContactStore(sink=persistence.contact_sink())
    persistence.attach_contacts(store)

    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)
    contact = store.get(identity.public_key)
    assert contact is not None

    class _FailingRepository:
        """A repository whose write never lands, which is what an outage is."""

        async def upsert_many(self, contacts: list[Contact]) -> Failed:
            from sighop.db.engine import DatabaseUnavailableError

            return Failed(operation="upsert_contacts", error=DatabaseUnavailableError("down"))

    persistence.contacts = _FailingRepository()  # type: ignore[assignment]
    landed = await persistence._flush_contacts([contact])

    assert landed is False
    assert store.awaiting_backfill == 1


@pytest.mark.database
async def test_one_batch_carrying_a_peer_twice_writes_its_latest_state_once(
    database: Database,
) -> None:
    """Postgres refuses an ON CONFLICT batch that proposes one key twice —
    "cannot affect row a second time" — and a real batch does exactly that: a
    peer observed again before the writer drained. Observed against the dev
    database while replaying the corpus, where it discarded every contact and
    route write in the run.
    """
    repository = ContactRepository(database=database)
    identity = generate_identity()
    early = Contact(
        public_key=identity.public_key,
        name=verified_advert(identity, "early").appdata.name,
        first_heard=NOW,
        last_heard=NOW,
        advert_verified=True,
    )
    late = Contact(
        public_key=identity.public_key,
        name=verified_advert(identity, "late").appdata.name,
        first_heard=NOW,
        last_heard=LATER,
        advert_verified=True,
    )

    outcome = await repository.upsert_many([early, late])
    assert isinstance(outcome, Succeeded), "a batch with a repeated key failed"

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    (row,) = loaded.value
    assert row.name is not None and row.name.text == "late"
    assert row.last_heard == LATER
