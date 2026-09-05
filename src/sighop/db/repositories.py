"""The operations `net/` calls, and the only place SQL lives (design D10).

`net/contacts.py` and `net/paths.py` keep their in-memory structures and their
interfaces, and gain an optional repository they write to and are loaded from
once at startup. Neither imports SQLAlchemy; the record types crossing the
boundary are the ones those modules already define (`Contact`, `PathKey`,
`LearnedPath`), so the persistent path is a thin adapter rather than a second
model of the same thing.

Every method here returns an `Outcome` from `Database.run`, which means a
database fault reaches a caller as a value rather than as an exception in the RX
path (design D8). The one deliberate exception is decrypting an entity seed:
that happens *outside* `run`, because a wrong secret or an altered row is not a
database fault and must not be reported as one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from sighop.db.engine import Database, Failed, Outcome, Succeeded
from sighop.db.models import Contact as ContactRow
from sighop.db.models import Entity as EntityRow
from sighop.db.models import PacketLog as PacketLogRowModel
from sighop.db.models import Path as PathRow
from sighop.db.sealing import open_seed, seal_seed
from sighop.db.times import ensure_utc
from sighop.net.contacts import Contact
from sighop.net.paths import LearnedPath, PathKey
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.payloads import NodeType, WireText

DEFAULT_PACKET_LOG_MAX_ROWS = 100_000
"""Design D3, open question 2. Derived from one 2 h 54 min session at ~191
receptions/hour — roughly three weeks — and enforced by row count rather than by
age, because per-file traffic in the corpus varies by an order of magnitude."""

ENTITY_TYPES = {
    NodeType.ROOM_SERVER: "room_server",
    NodeType.CHAT: "companion",
    NodeType.REPEATER: "repeater",
    NodeType.SENSOR: "sensor",
}
DEFAULT_ENTITY_TYPE = "companion"


class EntityLoadError(RuntimeError):
    """A stored entity that could not be turned back into an identity."""


class EntityKeyMismatchError(EntityLoadError):
    """The stored public key is not the one the decrypted seed derives.

    Corrupt data rather than a wrong secret: the box authenticated, so the
    secret is right and the row disagrees with itself. Neither half is preferred
    — either could be the corrupted one, and guessing would produce an identity
    nobody holds.
    """


class EntityExistsError(RuntimeError):
    """An identity with this public key is already stored. Names the existing one."""


# --- Entities ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """A stored identity without anything secret in it.

    This is what listing shows and what log events carry. It holds neither the
    seed nor its ciphertext, so there is no rendering path along which either
    could escape.
    """

    id: uuid.UUID
    type: str
    name: str
    public_key: bytes
    node_hash: int
    advert_config: dict
    enabled: bool
    created_at: dt.datetime

    @property
    def node_type(self) -> NodeType | int:
        raw = int(self.advert_config.get("node_type", int(NodeType.CHAT)))
        try:
            return NodeType(raw)
        except ValueError:
            return raw

    def as_json(self) -> dict[str, object]:
        return {
            "entity_id": str(self.id),
            "entity_name": self.name,
            "entity_type": self.type,
            "public_key": self.public_key.hex(),
            "node_hash": self.node_hash,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class LoadedEntity:
    """A stored identity whose seed has been opened. Not logged, ever."""

    record: EntityRecord
    identity: LocalIdentity

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def public_key(self) -> bytes:
        return self.record.public_key

    @property
    def node_hash(self) -> int:
        return self.record.node_hash


def advert_config_for(
    node_type: NodeType | int,
    *,
    flood_interval_seconds: float | None = None,
    zero_hop_interval_seconds: float = 0.0,
    latitude: int | None = None,
    longitude: int | None = None,
) -> dict[str, object]:
    """The advert configuration a stored entity carries, as JSON.

    Deliberately holds no schedule *state* — `next_flood_at`, `adverts_sent` —
    only configuration. Restoring a schedule would make a restart advert on the
    old clock, and the stagger exists precisely so a restart does not burst."""
    return {
        "node_type": int(node_type),
        "flood_interval_seconds": flood_interval_seconds,
        "zero_hop_interval_seconds": zero_hop_interval_seconds,
        "latitude": latitude,
        "longitude": longitude,
    }


def entity_type_for(node_type: NodeType | int) -> str:
    try:
        return ENTITY_TYPES.get(NodeType(int(node_type)), DEFAULT_ENTITY_TYPE)
    except ValueError:
        return DEFAULT_ENTITY_TYPE


@dataclass(slots=True)
class EntityRepository:
    """The `entity` table: identities whose seeds never touch it in the clear."""

    database: Database

    async def store(
        self,
        *,
        name: str,
        identity: LocalIdentity,
        secret: bytes,
        node_type: NodeType | int = NodeType.CHAT,
        entity_type: str | None = None,
        advert_config: dict[str, object] | None = None,
        enabled: bool = True,
        created_at: dt.datetime | None = None,
    ) -> Outcome[EntityRecord]:
        """Seal the seed, then write the row. The sealing is not a database step."""
        sealed = seal_seed(identity.seed, secret)
        record = EntityRecord(
            id=uuid.uuid4(),
            type=entity_type or entity_type_for(node_type),
            name=name,
            public_key=identity.public_key,
            node_hash=identity.node_hash,
            advert_config=advert_config or advert_config_for(node_type),
            enabled=enabled,
            created_at=ensure_utc(created_at or dt.datetime.now(dt.UTC), field="entity.created_at"),
        )

        async def work(session: object) -> EntityRecord:
            session.add(  # type: ignore[attr-defined]
                EntityRow(
                    id=record.id,
                    type=record.type,
                    name=record.name,
                    public_key=record.public_key,
                    node_hash=record.node_hash,
                    sealed_seed=sealed,
                    advert_config=record.advert_config,
                    enabled=record.enabled,
                    created_at=record.created_at,
                )
            )
            return record

        return await self.database.run("store_entity", work)

    async def list_all(self) -> Outcome[list[EntityRecord]]:
        """Every stored identity, with no key material — the secret is not needed."""

        async def work(session: object) -> list[EntityRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(EntityRow).order_by(EntityRow.created_at)
                )
            ).scalars()
            return [_record(row) for row in rows]

        return await self.database.run("list_entities", work)

    async def get(self, public_key: bytes) -> Outcome[EntityRecord | None]:
        async def work(session: object) -> EntityRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(EntityRow).where(EntityRow.public_key == public_key)
                )
            ).scalar_one_or_none()
            return None if row is None else _record(row)

        return await self.database.run("get_entity", work)

    async def load_all(
        self, secret: bytes, *, enabled_only: bool = False
    ) -> Outcome[list[LoadedEntity]]:
        """Read every stored identity and open its seed.

        The read is a database operation and returns an outcome; the opening is
        not, and a wrong secret, an altered row or a public key that disagrees
        with its seed raises rather than being reported as a database fault.
        """

        async def work(session: object) -> list[tuple[EntityRecord, bytes]]:
            statement = select(EntityRow).order_by(EntityRow.created_at)
            if enabled_only:
                statement = statement.where(EntityRow.enabled.is_(True))
            rows = (await session.execute(statement)).scalars()  # type: ignore[attr-defined]
            return [(_record(row), row.sealed_seed) for row in rows]

        outcome = await self.database.run("load_entities", work)
        if isinstance(outcome, Failed):
            return outcome
        return Succeeded(value=[_open(record, sealed, secret) for record, sealed in outcome.value])

    async def set_enabled(self, public_key: bytes, enabled: bool) -> Outcome[bool]:
        async def work(session: object) -> bool:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(EntityRow).where(EntityRow.public_key == public_key)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.enabled = enabled
            return True

        return await self.database.run("set_entity_enabled", work)


def _record(row: EntityRow) -> EntityRecord:
    return EntityRecord(
        id=row.id,
        type=row.type,
        name=row.name,
        public_key=bytes(row.public_key),
        node_hash=row.node_hash,
        advert_config=dict(row.advert_config or {}),
        enabled=row.enabled,
        created_at=row.created_at,
    )


def _open(record: EntityRecord, sealed: bytes, secret: bytes) -> LoadedEntity:
    """Open one seed and check it against the public key stored beside it.

    The check is the last thing standing between a wrong row and an identity
    nobody holds: the box authenticating proves the secret, not that the row is
    internally consistent.
    """
    label = f"entity {record.name!r} ({record.public_key.hex()[:16]})"
    seed = open_seed(bytes(sealed), secret, entity=label)
    identity = LocalIdentity.from_seed(seed)
    if identity.public_key != record.public_key:
        raise EntityKeyMismatchError(
            f"{label}: the stored public key is not the one the stored seed derives "
            f"({identity.public_key.hex()}); refusing to prefer either value, and no "
            "key was produced"
        )
    return LoadedEntity(record=record, identity=identity)


# --- Contacts (design D12) --------------------------------------------------


@dataclass(slots=True)
class ContactRepository:
    """The `contact` table, upserted on the public key because it is the identity.

    A restored contact's `name` is the *rendering* the advert produced, not its
    exact bytes: the column is `text` and a name that was not valid UTF-8 comes
    back with its replacement characters. The display name and the verified
    marker survive, which is what resolution and the status line use; the raw
    bytes of a malformed name do not, and the advert that carries them is the
    record of those.
    """

    database: Database

    async def upsert_many(self, contacts: Sequence[Contact]) -> Outcome[int]:
        if not contacts:
            return Succeeded(value=0)
        values = _latest_per_key(
            (contact.public_key, _contact_values(contact)) for contact in contacts
        )

        async def work(session: object) -> int:
            statement = insert(ContactRow).values(values)
            await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_update(
                    index_elements=[ContactRow.public_key],
                    set_={
                        "node_hash": statement.excluded.node_hash,
                        "name": statement.excluded.name,
                        "node_type": statement.excluded.node_type,
                        "flags": statement.excluded.flags,
                        "advert_verified": statement.excluded.advert_verified,
                        # first_heard is deliberately absent: it is when we first
                        # heard the peer, and re-hearing does not move it.
                        "last_heard": statement.excluded.last_heard,
                    },
                )
            )
            return len(values)

        return await self.database.run("upsert_contacts", work)

    async def upsert(self, contact: Contact) -> Outcome[int]:
        return await self.upsert_many([contact])

    async def load_all(self) -> Outcome[list[Contact]]:
        async def work(session: object) -> list[Contact]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ContactRow).order_by(ContactRow.first_heard)
                )
            ).scalars()
            return [_contact(row) for row in rows]

        return await self.database.run("load_contacts", work)

    async def count(self) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(ContactRow)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_contacts", work)


def _latest_per_key(
    entries: Iterable[tuple[object, dict[str, object]]],
) -> list[dict[str, object]]:
    """Collapse a batch to one row per conflict key, keeping the last seen.

    Postgres refuses an `ON CONFLICT DO UPDATE` whose own batch proposes the same
    constrained values twice — "cannot affect row a second time" — and a batch
    naturally does: a peer observed several times, or a route re-confirmed twice,
    before the writer drained. Collapsing to the latest is the same semantics the
    design already states for the recovery flush (design D15): the write carries
    a contact's *latest state* once rather than replaying each observation.

    A dict preserves insertion order, so the surviving row keeps the position of
    its first appearance and the values of its last.
    """
    collapsed: dict[object, dict[str, object]] = {}
    for key, values in entries:
        collapsed[key] = values
    return list(collapsed.values())


def _contact_values(contact: Contact) -> dict[str, object]:
    now = dt.datetime.now(dt.UTC)
    first = contact.first_heard or now
    last = contact.last_heard or first
    return {
        "public_key": contact.public_key,
        "node_hash": contact.node_hash,
        "name": None if contact.name is None else contact.name.text,
        "node_type": None if contact.node_type is None else int(contact.node_type),
        "flags": contact.flags,
        "advert_verified": contact.advert_verified,
        "first_heard": ensure_utc(first, field="contact.first_heard"),
        "last_heard": ensure_utc(last, field="contact.last_heard"),
    }


def _contact(row: ContactRow) -> Contact:
    node_type: NodeType | int | None = None
    if row.node_type is not None:
        try:
            node_type = NodeType(row.node_type)
        except ValueError:
            node_type = row.node_type
    return Contact(
        public_key=bytes(row.public_key),
        name=None if row.name is None else WireText.from_bytes(row.name.encode("utf-8")),
        node_type=node_type,
        flags=row.flags,
        first_heard=row.first_heard,
        last_heard=row.last_heard,
        advert_verified=row.advert_verified,
    )


# --- Paths (design D12) -----------------------------------------------------


@dataclass(slots=True)
class PathRepository:
    """The `path` table, upserted on what makes a candidate route distinct.

    Every candidate is stored rather than only the winner: §13 unknown #3 —
    whether path scoring needs more than most-recently-confirmed-wins — is open
    *because* nobody has yet observed two routes to one peer, and persisting
    candidates is how that observation eventually gets made across runs.
    """

    database: Database

    async def upsert_many(self, entries: Sequence[tuple[PathKey, LearnedPath]]) -> Outcome[int]:
        if not entries:
            return Succeeded(value=0)
        # Sorted by confirmation first, so that when a batch carries the same
        # route twice the survivor is the later one — most-recently-confirmed
        # wins here exactly as it does in memory.
        values = _latest_per_key(
            (
                (key.public_key, key.node_hash, learned.path, learned.hash_size),
                _path_values(key, learned),
            )
            for key, learned in sorted(entries, key=lambda entry: entry[1].confirmed_at)
        )

        async def work(session: object) -> int:
            statement = insert(PathRow).values(values)
            await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_update(
                    constraint="uq_path_destination",
                    set_={
                        "hop_count": statement.excluded.hop_count,
                        "snr_db": statement.excluded.snr_db,
                        "confirmed_at": statement.excluded.confirmed_at,
                        "packet_id": statement.excluded.packet_id,
                    },
                    # A batch can carry an older confirmation than the row already
                    # holds; most-recently-confirmed-wins is the store's rule and
                    # the table follows it rather than the arrival order.
                    where=PathRow.confirmed_at <= statement.excluded.confirmed_at,
                )
            )
            return len(values)

        return await self.database.run("upsert_paths", work)

    async def load_all(self) -> Outcome[list[tuple[PathKey, LearnedPath]]]:
        async def work(session: object) -> list[tuple[PathKey, LearnedPath]]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(PathRow).order_by(PathRow.confirmed_at)
                )
            ).scalars()
            return [_path(row) for row in rows]

        return await self.database.run("load_paths", work)

    async def count(self) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(PathRow)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_paths", work)


def _path_values(key: PathKey, learned: LearnedPath) -> dict[str, object]:
    return {
        # Exactly one of these is set, and which one is what `ambiguous` means.
        "dest_public_key": key.public_key,
        "dest_node_hash": None if key.public_key is not None else key.node_hash,
        "path_bytes": learned.path,
        "hash_size": learned.hash_size,
        "hop_count": learned.hop_count,
        "snr_db": learned.snr_db,
        "confirmed_at": ensure_utc(learned.confirmed_at, field="path.confirmed_at"),
        "packet_id": learned.packet_id,
    }


def _path(row: PathRow) -> tuple[PathKey, LearnedPath]:
    key = (
        PathKey.for_public_key(bytes(row.dest_public_key))
        if row.dest_public_key is not None
        else PathKey.for_node_hash(int(row.dest_node_hash or 0))
    )
    learned = LearnedPath(
        path=bytes(row.path_bytes),
        hash_size=row.hash_size,
        hop_count=row.hop_count,
        snr_db=row.snr_db,
        # The original confirmation time, not now: restoration is not
        # confirmation, so a live reception beats a restored route.
        confirmed_at=row.confirmed_at,
        packet_id=row.packet_id or "",
    )
    return key, learned


# --- The packet log ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PacketLogRow:
    """One row of the feed. Built off the reception path, written behind it."""

    packet_id: str
    direction: str
    at: dt.datetime
    outcome: str
    route_type: str | None = None
    payload_type: str | None = None
    path_bytes: bytes | None = None
    hop_count: int | None = None
    size_bytes: int | None = None
    snr_db: float | None = None
    rssi_dbm: int | None = None
    airtime_ms: float | None = None
    entity_id: uuid.UUID | None = None
    priority_class: int | None = None
    reason: str | None = None
    raw: bytes | None = None

    def values(self) -> dict[str, object]:
        return {
            "packet_id": self.packet_id,
            "direction": self.direction,
            "at": ensure_utc(self.at, field="packet_log.at"),
            "outcome": self.outcome,
            "route_type": self.route_type,
            "payload_type": self.payload_type,
            "path_bytes": self.path_bytes,
            "hop_count": self.hop_count,
            "size_bytes": self.size_bytes,
            "snr_db": self.snr_db,
            "rssi_dbm": self.rssi_dbm,
            "airtime_ms": self.airtime_ms,
            "entity_id": self.entity_id,
            "priority_class": self.priority_class,
            "reason": self.reason,
            "raw": self.raw,
        }


@dataclass(slots=True)
class PacketLogRepository:
    """The bounded ring buffer: written in batches, pruned on a schedule."""

    database: Database
    max_rows: int = DEFAULT_PACKET_LOG_MAX_ROWS
    pruned: int = field(default=0, init=False)

    async def write_many(self, rows: Sequence[PacketLogRow]) -> Outcome[int]:
        if not rows:
            return Succeeded(value=0)
        values = [row.values() for row in rows]

        async def work(session: object) -> int:
            await session.execute(insert(PacketLogRowModel).values(values))  # type: ignore[attr-defined]
            return len(values)

        return await self.database.run("write_packet_log", work)

    async def count(self) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(PacketLogRowModel)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_packet_log", work)

    async def prune(self) -> Outcome[int]:
        """Delete everything beyond the row cap, oldest first. Reports the count.

        The cap is on rows rather than on age because per-file traffic in the
        corpus varies by an order of magnitude, and a bound nobody can predict
        the effect of is a bound nobody sets correctly.
        """
        keep = self.max_rows

        async def work(session: object) -> int:
            doomed = (
                select(PacketLogRowModel.id)
                .order_by(PacketLogRowModel.at.desc(), PacketLogRowModel.id.desc())
                .offset(keep)
                .subquery()
            )
            result = await session.execute(  # type: ignore[attr-defined]
                delete(PacketLogRowModel).where(PacketLogRowModel.id.in_(select(doomed.c.id)))
            )
            return int(result.rowcount or 0)

        outcome = await self.database.run("prune_packet_log", work)
        if isinstance(outcome, Succeeded):
            self.pruned += outcome.value
        return outcome
