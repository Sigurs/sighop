"""Durable paths (section 6, design D2/D12).

The path store's contract is unchanged: one shared store, bounded, answering
every lookup from memory. What is new is behind it — routes written off the
reception path, dropped freely when they cannot be, and restored at startup with
the confirmation time they were learned with, because restoring is not
confirming.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import text

import sighop.net.paths as paths_module
from sighop.db.engine import Database, Succeeded
from sighop.db.repositories import PathRepository
from sighop.db.writer import WriteBehind
from sighop.net.paths import LearnedPath, PathKey, PathStore
from sighop.net.rx import RxRecord, decode_event
from sighop.protocol.identity import generate_identity
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CORPUS_DIR, CORPUS_FILES

NOW = dt.datetime(2026, 9, 5, 20, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(minutes=30)

Entry = tuple[PathKey, LearnedPath]


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    """Real receptions, so "learned a route" means what it means on the air."""
    records: list[RxRecord] = []
    for name in CORPUS_FILES:
        replay = CaptureReplay.open(CORPUS_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
    return records


def learned(
    path: bytes = b"\x11\x22",
    *,
    hash_size: int = 1,
    at: dt.datetime = NOW,
    snr_db: float | None = 7.5,
    packet_id: str = "p1",
) -> LearnedPath:
    return LearnedPath(
        path=path,
        hash_size=hash_size,
        hop_count=len(path) // hash_size,
        snr_db=snr_db,
        confirmed_at=at,
        packet_id=packet_id,
    )


class _Sink:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.offered: list[Entry] = []

    def offer(self, entry: Entry) -> bool:
        self.offered.append(entry)
        return self.accept


# --- 6.1 Upsert on the destination tuple ------------------------------------


async def test_hearing_one_route_twice_updates_it_rather_than_adding_a_row(
    database: Database,
) -> None:
    repository = PathRepository(database=database)
    key = PathKey.for_public_key(generate_identity().public_key)

    assert isinstance(await repository.upsert_many([(key, learned(at=NOW))]), Succeeded)
    assert isinstance(
        await repository.upsert_many([(key, learned(at=LATER, snr_db=-2.0))]), Succeeded
    )

    async with database.sessions() as session:
        rows = (await session.execute(text("SELECT count(*) FROM path"))).scalar_one()
    assert rows == 1, "re-hearing a route accumulated a second candidate"

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    (_, restored) = loaded.value[0]
    assert restored.confirmed_at == LATER
    assert restored.snr_db == -2.0


async def test_an_older_confirmation_arriving_late_does_not_regress_the_row(
    database: Database,
) -> None:
    """Most-recently-confirmed-wins is the store's rule; the table follows it
    rather than the order a batch happened to arrive in."""
    repository = PathRepository(database=database)
    key = PathKey.for_public_key(generate_identity().public_key)
    await repository.upsert_many([(key, learned(at=LATER))])
    await repository.upsert_many([(key, learned(at=NOW))])

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    assert loaded.value[0][1].confirmed_at == LATER


async def test_two_routes_to_one_destination_are_two_rows(database: Database) -> None:
    """§13 unknown #3 needs the observation, so every candidate is stored."""
    repository = PathRepository(database=database)
    key = PathKey.for_public_key(generate_identity().public_key)
    await repository.upsert_many([(key, learned(b"\x11\x22")), (key, learned(b"\x33\x44"))])
    async with database.sessions() as session:
        rows = (await session.execute(text("SELECT count(*) FROM path"))).scalar_one()
    assert rows == 2


# --- 6.2 The two keyings stay distinct --------------------------------------


async def test_a_hash_keyed_route_is_still_ambiguous_after_the_round_trip(
    database: Database,
) -> None:
    repository = PathRepository(database=database)
    await repository.upsert_many([(PathKey.for_node_hash(0x42), learned())])

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    (key, _) = loaded.value[0]
    assert key.ambiguous is True
    assert key.public_key is None
    assert key.node_hash == 0x42


async def test_a_key_keyed_and_a_hash_keyed_route_do_not_collide(
    database: Database,
) -> None:
    repository = PathRepository(database=database)
    identity = generate_identity()
    await repository.upsert_many(
        [
            (PathKey.for_public_key(identity.public_key), learned()),
            (PathKey.for_node_hash(identity.node_hash), learned()),
        ]
    )
    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    assert len(loaded.value) == 2
    assert {key.ambiguous for key, _ in loaded.value} == {True, False}


# --- 6.3 An empty path is a route -------------------------------------------


async def test_a_zero_hop_route_round_trips_and_is_not_the_absence_of_one(
    database: Database,
) -> None:
    repository = PathRepository(database=database)
    identity = generate_identity()
    key = PathKey.for_public_key(identity.public_key)
    zero_hop = LearnedPath(
        path=b"", hash_size=1, hop_count=0, snr_db=9.0, confirmed_at=NOW, packet_id="p0"
    )
    assert isinstance(await repository.upsert_many([(key, zero_hop)]), Succeeded)

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    store = PathStore()
    store.restore(loaded.value)

    found = store.lookup(key)
    assert found is not None
    assert found.path == b""
    assert found.is_zero_hop
    # An unknown destination still reports no path, which is a different thing.
    assert store.lookup_public_key(generate_identity().public_key) is None


# --- 6.4 Routes are written behind the reception path -----------------------


async def test_learning_faster_than_the_sink_accepts_keeps_reception_at_full_rate() -> None:
    accepted: list[Entry] = []
    gate = asyncio.Event()

    async def slow(batch: list[Entry]) -> bool:
        await gate.wait()
        accepted.extend(batch)
        return True

    writer: WriteBehind[Entry] = WriteBehind("routes", slow, capacity=8, batch_size=4)
    writer.start()
    store = PathStore(sink=writer)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        for index in range(200):
            store._insert(PathKey.for_node_hash(index % 256), learned(packet_id=f"p{index}"))
            writer.offer((PathKey.for_node_hash(index % 256), learned()))
        elapsed = loop.time() - started

        assert elapsed < 0.5, "offering routes blocked on the sink"
        assert writer.pending <= writer.capacity, "the backlog was unbounded"
        assert writer.discarded > 0, "drops were not counted"
    finally:
        gate.set()
        await writer.stop()


def test_learning_from_a_real_reception_offers_the_route(
    corpus_records: list[RxRecord],
) -> None:
    """`observe` is the reception path; `offer` is the whole of what it adds."""
    sink = _Sink()
    store = PathStore(sink=sink)
    learned_any = 0
    for record in corpus_records[:200]:
        if store.observe(record) is not None:
            learned_any += 1
    assert learned_any > 0, "the corpus slice taught this store nothing"
    assert len(sink.offered) == learned_any
    # Restoration is the other direction and must not feed back into the sink.
    before = len(sink.offered)
    store.restore([(PathKey.for_node_hash(0x42), learned())])
    assert len(sink.offered) == before


# --- 6.5 Restoration is not confirmation ------------------------------------


def test_a_freshly_learned_route_beats_a_restored_one_and_both_are_kept() -> None:
    store = PathStore()
    key = PathKey.for_public_key(generate_identity().public_key)
    store.restore([(key, learned(b"\x11\x22", at=NOW))])
    store._insert(key, learned(b"\x33\x44", at=LATER))

    winner = store.lookup(key)
    assert winner is not None
    assert winner.path == b"\x33\x44", "restoration was treated as confirmation"
    assert {candidate.path for candidate in store.candidates(key)} == {
        b"\x11\x22",
        b"\x33\x44",
    }


async def test_a_restored_route_keeps_the_confirmation_time_it_was_learned_with(
    database: Database,
) -> None:
    repository = PathRepository(database=database)
    key = PathKey.for_public_key(generate_identity().public_key)
    await repository.upsert_many([(key, learned(at=NOW))])

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    store = PathStore()
    assert store.restore(loaded.value) == 1
    found = store.lookup(key)
    assert found is not None
    assert found.confirmed_at == NOW


# --- 6.6 Lookups never touch the database -----------------------------------


def test_lookups_are_answered_from_memory_with_a_refusing_sink() -> None:
    sink = _Sink(accept=False)
    store = PathStore(sink=sink)
    key = PathKey.for_public_key(generate_identity().public_key)
    store._insert(key, learned())

    loop_free_result = store.lookup(key)
    assert loop_free_result is not None
    assert store.lookup_node_hash(key.node_hash) is None  # a different keying
    assert store.destination_count == 1


def test_net_paths_imports_no_sqlalchemy() -> None:
    """Design D10 again: `net/` stays free of the database's own vocabulary."""
    source = Path(paths_module.__file__).read_text()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any(name.split(".")[0] in {"sqlalchemy", "asyncpg"} for name in names)
    assert not any(name.startswith("sighop.db") for name in names)


async def test_one_batch_carrying_a_route_twice_keeps_the_later_confirmation(
    database: Database,
) -> None:
    """The same defect as for contacts, and the same fix: a batch is collapsed
    to one row per conflict key, and for routes the survivor is the later
    confirmation — most-recently-confirmed-wins, in the table as in memory."""
    repository = PathRepository(database=database)
    key = PathKey.for_public_key(generate_identity().public_key)

    outcome = await repository.upsert_many(
        [(key, learned(at=LATER, snr_db=-3.0)), (key, learned(at=NOW, snr_db=9.0))]
    )
    assert isinstance(outcome, Succeeded), "a batch with a repeated route failed"

    loaded = await repository.load_all()
    assert isinstance(loaded, Succeeded)
    assert len(loaded.value) == 1
    assert loaded.value[0][1].confirmed_at == LATER
    assert loaded.value[0][1].snr_db == -3.0
