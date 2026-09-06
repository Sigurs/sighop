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

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sighop.db.engine import Database, Failed, Outcome, Succeeded
from sighop.db.models import Contact as ContactRow
from sighop.db.models import Entity as EntityRow
from sighop.db.models import Message as MessageRow
from sighop.db.models import PacketLog as PacketLogRowModel
from sighop.db.models import Path as PathRow
from sighop.db.models import Room as RoomRow
from sighop.db.models import RoomMember as RoomMemberRow
from sighop.db.sealing import open_seed, seal_seed
from sighop.db.times import ensure_utc
from sighop.net.contacts import Contact
from sighop.net.paths import LearnedPath, PathKey
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.payloads import (
    PERMISSION_ROLE_MASK,
    NodeType,
    Permission,
    WireText,
)

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


# --- Rooms, members and history (milestone 6, design D2/D3/D5) --------------
#
# The asymmetry here is design D5's and is the whole reason these are three
# repositories rather than one. The ACL is read *inside* the MAC trial, once per
# packet, so it is loaded into memory at startup and written through on change —
# milestone 5's contact policy applied to the same kind of data. History is
# unbounded, exists to outlive the process, and is read by the push loop, which
# is a background task nothing waits on — so it is read from and written to the
# database directly, with no in-memory mirror to fall out of sync.


class RoomExistsError(RuntimeError):
    """The identity already has a room. Names the existing one (`runtime-cli`)."""


@dataclass(frozen=True, slots=True)
class RoomRecord:
    """A room as stored, with the two password hashes it holds.

    The hashes are here because rotation and login both need them and there is
    nowhere else for them to live. Nothing renders this object: `as_json` is
    what log events and status output carry, and it has no hash in it.
    """

    id: uuid.UUID
    entity_id: uuid.UUID
    name: str
    admin_password_hash: str
    guest_password_hash: str | None
    guest_open: bool
    allow_read_only: bool
    retention_days: int | None
    retention_messages: int | None
    created_at: dt.datetime

    @property
    def guest_access(self) -> str:
        """The three states the two guest columns encode between them (design D2)."""
        if self.guest_password_hash is not None:
            return "password"
        return "open" if self.guest_open else "refused"

    @property
    def retention(self) -> str:
        """"unlimited" said plainly when no policy is set (design D15)."""
        bounds = []
        if self.retention_days is not None:
            bounds.append(f"{self.retention_days} days")
        if self.retention_messages is not None:
            bounds.append(f"{self.retention_messages} messages")
        return " and ".join(bounds) if bounds else "unlimited"

    def as_json(self) -> dict[str, object]:
        return {
            "room_id": str(self.id),
            "room_name": self.name,
            "entity_id": str(self.entity_id),
            "guest_access": self.guest_access,
            "allow_read_only": self.allow_read_only,
            "retention": self.retention,
        }


@dataclass(frozen=True, slots=True)
class MemberRecord:
    """One member of one room — permissions, cursor and replay guard together.

    `sync_since` and `last_timestamp` are MeshCore's unsigned 32-bit epoch
    seconds as they appear on the wire, not instants (design D2).
    """

    room_id: uuid.UUID
    public_key: bytes
    node_hash: int
    permissions: int
    sync_since: int
    last_timestamp: int
    first_login: dt.datetime
    last_activity: dt.datetime

    @property
    def permission(self) -> Permission:
        try:
            return Permission(self.permissions & PERMISSION_ROLE_MASK)
        except ValueError:  # pragma: no cover - the mask makes this unreachable
            return Permission.GUEST

    def as_json(self) -> dict[str, object]:
        return {
            "member": self.public_key.hex()[:16],
            "node_hash": self.node_hash,
            "permission": self.permission.name.lower(),
            "sync_since": self.sync_since,
        }


@dataclass(frozen=True, slots=True)
class PostRecord:
    """One stored post. `text` is bytes, because the wire's text is (design D4)."""

    id: int
    room_id: uuid.UUID
    author_public_key: bytes
    post_timestamp: int
    sender_timestamp: int | None
    text: bytes
    posted_at: dt.datetime

    @property
    def rendered(self) -> WireText:
        """The text as something displayable, *marked* as a rendering."""
        return WireText.from_bytes(self.text)


@dataclass(slots=True)
class RoomRepository:
    """The `room` table. One row per identity, enforced by the schema."""

    database: Database

    async def create(
        self,
        *,
        entity_id: uuid.UUID,
        name: str,
        admin_password_hash: str,
        guest_password_hash: str | None = None,
        guest_open: bool = False,
        allow_read_only: bool = False,
        created_at: dt.datetime | None = None,
    ) -> Outcome[RoomRecord]:
        record = RoomRecord(
            id=uuid.uuid4(),
            entity_id=entity_id,
            name=name,
            admin_password_hash=admin_password_hash,
            guest_password_hash=guest_password_hash,
            guest_open=guest_open,
            allow_read_only=allow_read_only,
            # Both bounds unset: nothing is deleted until an operator sets a
            # policy (design D15), and a default here would answer §13's first
            # unknown by accident.
            retention_days=None,
            retention_messages=None,
            created_at=ensure_utc(created_at or dt.datetime.now(dt.UTC), field="room.created_at"),
        )

        # Checked before the insert so the refusal can *name* the existing room,
        # which is what `sighop room create` prints. The unique constraint stays
        # the backstop: this check loses a race and the constraint does not.
        existing = await self.get_for_entity(entity_id)
        if isinstance(existing, Succeeded) and existing.value is not None:
            raise RoomExistsError(
                f"this identity already carries the room {existing.value.name!r}; "
                "one identity is one node to the mesh, and a node is one room"
            )

        async def work(session: object) -> RoomRecord:
            session.add(  # type: ignore[attr-defined]
                RoomRow(
                    id=record.id,
                    entity_id=record.entity_id,
                    name=record.name,
                    admin_password_hash=record.admin_password_hash,
                    guest_password_hash=record.guest_password_hash,
                    guest_open=record.guest_open,
                    allow_read_only=record.allow_read_only,
                    created_at=record.created_at,
                )
            )
            return record

        return await self.database.run("create_room", work)

    async def list_all(self) -> Outcome[list[RoomRecord]]:
        async def work(session: object) -> list[RoomRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(RoomRow).order_by(RoomRow.created_at)
                )
            ).scalars()
            return [_room(row) for row in rows]

        return await self.database.run("list_rooms", work)

    async def get_for_entity(self, entity_id: uuid.UUID) -> Outcome[RoomRecord | None]:
        async def work(session: object) -> RoomRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(RoomRow).where(RoomRow.entity_id == entity_id)
                )
            ).scalar_one_or_none()
            return None if row is None else _room(row)

        return await self.database.run("get_room", work)

    async def set_passwords(
        self,
        room_id: uuid.UUID,
        *,
        admin_password_hash: str | None = None,
        guest_password_hash: str | None = None,
        guest_open: bool | None = None,
        clear_guest_password: bool = False,
    ) -> Outcome[bool]:
        """Rotate a password. **Evicts nobody**, because membership is keyed on
        the public key recorded at first login (§7) — this changes only what a
        *new* login is gated by."""

        async def work(session: object) -> bool:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(RoomRow).where(RoomRow.id == room_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            if admin_password_hash is not None:
                row.admin_password_hash = admin_password_hash
            if clear_guest_password:
                row.guest_password_hash = None
            elif guest_password_hash is not None:
                row.guest_password_hash = guest_password_hash
            if guest_open is not None:
                row.guest_open = guest_open
            return True

        return await self.database.run("set_room_passwords", work)

    async def set_retention(
        self,
        room_id: uuid.UUID,
        *,
        retention_days: int | None,
        retention_messages: int | None,
    ) -> Outcome[bool]:
        """Set or clear both bounds. `None` for either is "unlimited" for that one."""

        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                update(RoomRow)
                .where(RoomRow.id == room_id)
                .values(
                    retention_days=retention_days,
                    retention_messages=retention_messages,
                )
            )
            return bool(result.rowcount)

        return await self.database.run("set_room_retention", work)


def _room(row: RoomRow) -> RoomRecord:
    return RoomRecord(
        id=row.id,
        entity_id=row.entity_id,
        name=row.name,
        admin_password_hash=row.admin_password_hash,
        guest_password_hash=row.guest_password_hash,
        guest_open=row.guest_open,
        allow_read_only=row.allow_read_only,
        retention_days=row.retention_days,
        retention_messages=row.retention_messages,
        created_at=row.created_at,
    )


@dataclass(slots=True)
class RoomMemberRepository:
    """The `room_member` table: the ACL, the cursor and the replay guard."""

    database: Database

    async def load_for_room(self, room_id: uuid.UUID) -> Outcome[list[MemberRecord]]:
        async def work(session: object) -> list[MemberRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(RoomMemberRow)
                    .where(RoomMemberRow.room_id == room_id)
                    .order_by(RoomMemberRow.first_login)
                )
            ).scalars()
            return [_member(row) for row in rows]

        return await self.database.run("load_room_members", work)

    async def upsert(self, member: MemberRecord) -> Outcome[int]:
        values = {
            "room_id": member.room_id,
            "public_key": member.public_key,
            "node_hash": member.node_hash,
            "permissions": member.permissions,
            "sync_since": member.sync_since,
            "last_timestamp": member.last_timestamp,
            "first_login": ensure_utc(member.first_login, field="room_member.first_login"),
            "last_activity": ensure_utc(
                member.last_activity, field="room_member.last_activity"
            ),
        }

        async def work(session: object) -> int:
            statement = insert(RoomMemberRow).values([values])
            await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_update(
                    constraint="pk_room_member",
                    set_={
                        "node_hash": statement.excluded.node_hash,
                        "permissions": statement.excluded.permissions,
                        "sync_since": statement.excluded.sync_since,
                        "last_timestamp": statement.excluded.last_timestamp,
                        # first_login is deliberately absent: it is when this
                        # member first joined, and logging in again does not
                        # move it, exactly as re-hearing a contact does not move
                        # `first_heard`.
                        "last_activity": statement.excluded.last_activity,
                    },
                )
            )
            return 1

        return await self.database.run("upsert_room_member", work)

    async def delete(self, room_id: uuid.UUID, public_key: bytes) -> Outcome[bool]:
        """Revoke: membership, permissions, cursor and replay guard go together.

        One row holds all four, so there is no partial revocation to get wrong —
        which is why the ACL keeping the cursor turned out to be the right shape
        rather than merely a convenient one.
        """

        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                delete(RoomMemberRow).where(
                    RoomMemberRow.room_id == room_id,
                    RoomMemberRow.public_key == public_key,
                )
            )
            return bool(result.rowcount)

        return await self.database.run("delete_room_member", work)


def _member(row: RoomMemberRow) -> MemberRecord:
    return MemberRecord(
        room_id=row.room_id,
        public_key=bytes(row.public_key),
        node_hash=row.node_hash,
        permissions=row.permissions,
        sync_since=row.sync_since,
        last_timestamp=row.last_timestamp,
        first_login=row.first_login,
        last_activity=row.last_activity,
    )


@dataclass(slots=True)
class MessageRepository:
    """The `message` table: durable, ordered, and read directly (design D5)."""

    database: Database
    pruned: int = field(default=0, init=False)
    pruned_unsynced: int = field(default=0, init=False)
    """Messages deleted whose ordering values were above at least one member's
    cursor — retention outrunning sync, reported rather than hidden (D15)."""

    async def store(
        self,
        *,
        room_id: uuid.UUID,
        author_public_key: bytes,
        text: bytes,
        sender_timestamp: int | None = None,
        now: int | None = None,
        posted_at: dt.datetime | None = None,
    ) -> Outcome[PostRecord]:
        """Store one post, stamping it `max(now, last + 1)` for its room.

        The stamp is computed **in the statement** rather than read and then
        written, so two concurrent posts cannot both read the same maximum. The
        unique constraint is what makes that safe rather than hopeful (design
        D3): a clock that steps backwards produces a stall in stamping, never a
        duplicate or a reordering.
        """
        at = ensure_utc(posted_at or dt.datetime.now(dt.UTC), field="message.posted_at")
        stamp = int(at.timestamp()) if now is None else now

        async def work(session: object) -> PostRecord:
            highest = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.max(MessageRow.post_timestamp)).where(
                        MessageRow.room_id == room_id
                    )
                )
            ).scalar_one_or_none()
            post_timestamp = stamp if highest is None else max(stamp, int(highest) + 1)
            row = MessageRow(
                room_id=room_id,
                author_public_key=author_public_key,
                post_timestamp=post_timestamp,
                sender_timestamp=sender_timestamp,
                text=text,
                posted_at=at,
            )
            session.add(row)  # type: ignore[attr-defined]
            await session.flush()  # type: ignore[attr-defined]
            return PostRecord(
                id=row.id,
                room_id=room_id,
                author_public_key=author_public_key,
                post_timestamp=post_timestamp,
                sender_timestamp=sender_timestamp,
                text=text,
                posted_at=at,
            )

        return await self.database.run("store_message", work)

    async def find_retry(
        self, room_id: uuid.UUID, author_public_key: bytes, sender_timestamp: int
    ) -> Outcome[PostRecord | None]:
        """The post this sender already made at this timestamp, if any.

        What makes a retry a retry is the sender's own timestamp, which the
        firmware also uses (`MyMesh.cpp:449`): a resend carries the same one and
        differs only in its attempt counter.
        """

        async def work(session: object) -> PostRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(MessageRow)
                    .where(
                        MessageRow.room_id == room_id,
                        MessageRow.author_public_key == author_public_key,
                        MessageRow.sender_timestamp == sender_timestamp,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return None if row is None else _post(row)

        return await self.database.run("find_message_retry", work)

    async def next_for_member(
        self, room_id: uuid.UUID, *, since: int, author_to_skip: bytes, not_after: int | None = None
    ) -> Outcome[PostRecord | None]:
        """The oldest post this member has not had, skipping its own.

        `not_after` is the hold: a post is not eligible until it has been stored
        for the reference implementation's delay (`POST_SYNC_DELAY_SECS`), so a
        client that is about to receive the sender's own copy is not raced.
        """

        async def work(session: object) -> PostRecord | None:
            statement = (
                select(MessageRow)
                .where(
                    MessageRow.room_id == room_id,
                    MessageRow.post_timestamp > since,
                    MessageRow.author_public_key != author_to_skip,
                )
                .order_by(MessageRow.post_timestamp)
                .limit(1)
            )
            if not_after is not None:
                statement = statement.where(MessageRow.post_timestamp <= not_after)
            row = (await session.execute(statement)).scalar_one_or_none()  # type: ignore[attr-defined]
            return None if row is None else _post(row)

        return await self.database.run("next_message_for_member", work)

    async def unsynced_count(
        self, room_id: uuid.UUID, *, since: int, author_to_skip: bytes
    ) -> Outcome[int]:
        """How many posts this member has yet to receive (`MyMesh.cpp:110-119`)."""

        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count())
                    .select_from(MessageRow)
                    .where(
                        MessageRow.room_id == room_id,
                        MessageRow.post_timestamp > since,
                        MessageRow.author_public_key != author_to_skip,
                    )
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_unsynced", work)

    async def history(
        self, room_id: uuid.UUID, *, limit: int = 100, newest_first: bool = False
    ) -> Outcome[list[PostRecord]]:
        async def work(session: object) -> list[PostRecord]:
            order = (
                MessageRow.post_timestamp.desc()
                if newest_first
                else MessageRow.post_timestamp
            )
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(MessageRow)
                    .where(MessageRow.room_id == room_id)
                    .order_by(order)
                    .limit(limit)
                )
            ).scalars()
            return [_post(row) for row in rows]

        return await self.database.run("read_history", work)

    async def count(self, room_id: uuid.UUID) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count())
                    .select_from(MessageRow)
                    .where(MessageRow.room_id == room_id)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_messages", work)

    async def prune(
        self,
        room_id: uuid.UUID,
        *,
        retention_days: int | None,
        retention_messages: int | None,
        now: dt.datetime | None = None,
    ) -> Outcome[tuple[int, int]]:
        """Apply a room's retention policy. Returns (deleted, deleted-while-unsynced).

        Both bounds unset deletes nothing, however old or numerous the history —
        that is what "unlimited" is, and it is the state a room ships in.

        The second number is the one design D15 insists on: retention wins over
        sync, deliberately, and the count of messages removed while at least one
        member was still behind them is what makes the trade visible instead of
        letting a gap in a member's history look like a delivery failure.
        """
        if retention_days is None and retention_messages is None:
            return Succeeded(value=(0, 0))
        cutoff = ensure_utc(now or dt.datetime.now(dt.UTC), field="message.posted_at")

        async def work(session: object) -> tuple[int, int]:
            doomed: set[int] = set()
            if retention_days is not None:
                horizon = cutoff - dt.timedelta(days=retention_days)
                doomed.update(
                    (
                        await session.execute(  # type: ignore[attr-defined]
                            select(MessageRow.id).where(
                                MessageRow.room_id == room_id,
                                MessageRow.posted_at < horizon,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            if retention_messages is not None:
                doomed.update(
                    (
                        await session.execute(  # type: ignore[attr-defined]
                            select(MessageRow.id)
                            .where(MessageRow.room_id == room_id)
                            .order_by(MessageRow.post_timestamp.desc())
                            .offset(retention_messages)
                        )
                    )
                    .scalars()
                    .all()
                )
            if not doomed:
                return 0, 0

            # Counted before the delete, because afterwards there is nothing to
            # count and the operator would never learn what the policy cost.
            behind = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.min(RoomMemberRow.sync_since)).where(
                        RoomMemberRow.room_id == room_id
                    )
                )
            ).scalar_one_or_none()
            unsynced = 0
            if behind is not None:
                unsynced = int(
                    (
                        await session.execute(  # type: ignore[attr-defined]
                            select(func.count())
                            .select_from(MessageRow)
                            .where(
                                MessageRow.id.in_(doomed),
                                MessageRow.post_timestamp > int(behind),
                            )
                        )
                    ).scalar_one()
                )

            result = await session.execute(  # type: ignore[attr-defined]
                delete(MessageRow).where(MessageRow.id.in_(doomed))
            )
            return int(result.rowcount or 0), unsynced

        outcome = await self.database.run("prune_messages", work)
        if isinstance(outcome, Succeeded):
            deleted, unsynced = outcome.value
            self.pruned += deleted
            self.pruned_unsynced += unsynced
        return outcome


def _post(row: MessageRow) -> PostRecord:
    return PostRecord(
        id=row.id,
        room_id=row.room_id,
        author_public_key=bytes(row.author_public_key),
        post_timestamp=row.post_timestamp,
        sender_timestamp=row.sender_timestamp,
        text=bytes(row.text),
        posted_at=row.posted_at,
    )
