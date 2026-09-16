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

import base64
import binascii
import datetime as dt
import hmac
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, func, literal, select, tuple_, update
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert

from sighop.db.engine import Database, Failed, Outcome, Succeeded
from sighop.db.models import Bot as BotRow
from sighop.db.models import BotState as BotStateRow
from sighop.db.models import Channel as ChannelRow
from sighop.db.models import ChannelMessage as ChannelMessageRow
from sighop.db.models import Contact as ContactRow
from sighop.db.models import DirectMessage as DirectMessageRow
from sighop.db.models import Entity as EntityRow
from sighop.db.models import Message as MessageRow
from sighop.db.models import PacketLog as PacketLogRowModel
from sighop.db.models import Path as PathRow
from sighop.db.models import Room as RoomRow
from sighop.db.models import RoomMember as RoomMemberRow
from sighop.db.models import Webhook as WebhookRow
from sighop.db.models import WebUser as WebUserRow
from sighop.db.sealing import SealError, open_seed, open_value, seal_seed, seal_value
from sighop.db.times import ensure_utc
from sighop.net.channels import (
    ChannelKind,
    ChannelMessageRecord,
    ChannelOutcome,
    ChannelSet,
    LoadedChannel,
)
from sighop.net.contacts import Contact
from sighop.net.dm import DirectMessageRecord, RecordedOutcome
from sighop.net.paths import LearnedPath, PathKey
from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, ChannelKey, channel_key_from_hashtag
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.payloads import (
    PERMISSION_ROLE_MASK,
    NodeType,
    Permission,
    WireText,
)
from sighop.webhooks.config import (
    WebhookExistsError,
    parse_format,
    parse_max_hops,
    parse_name,
    parse_triggers,
    parse_url,
)

DEFAULT_RECENT_PACKETS = 200
"""How much history the feed paints on connection by default. Enough to see the
shape of the last few minutes on a busy mesh — 555 receptions in 2 h 54 min was
the busiest measured — without making the first frame of a page a scroll."""

MAX_RECENT_PACKETS = 1000
"""The hard cap on one read. The bound on a statement's cost is not something
the caller asking for the rows gets to choose."""

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

BOT_ENTITY_TYPE = "bot"
"""sighop's role for an identity a driver runs on. It is *not* derived from the
node type and cannot be: a bot adverts as `NodeType.CHAT`, exactly as a
companion does, because a bot is a companion to every other node on the mesh
and its automation is sighop's business rather than the mesh's. Only the stored
type tells the two apart, and it is set explicitly at creation."""


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


class EntityRoleError(RuntimeError):
    """The role asked for disagrees with what this identity adverts as.

    One rule, here rather than in a command handler, because the browser stores
    a bot's identity through the same call: a refusal an operator meets in one
    surface has to be the same refusal in the other.
    """


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


def _node_type_name(node_type: NodeType | int) -> str:
    """What a node type is called, without assuming it is one we know."""
    try:
        return NodeType(int(node_type)).name
    except ValueError:
        return f"type_{int(node_type)}"


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
        """Seal the seed, then write the row. The sealing is not a database step.

        A public key that is already stored is refused *here*, naming the row
        that holds it, for the reason `RoomRepository.create` and
        `BotRepository.create` refuse here: the rule belongs to the table rather
        than to whichever surface reached it, and a second surface reimplementing
        it is how two surfaces end up disagreeing. The unique constraint stays
        the backstop — this check loses a race and the constraint does not.
        """
        if entity_type == BOT_ENTITY_TYPE and int(node_type) != int(NodeType.CHAT):
            raise EntityRoleError(
                f"this identity adverts as {_node_type_name(node_type)}; a bot "
                "presents itself to the mesh as a chat node, indistinguishable "
                "from a companion. Store it with node type CHAT"
            )
        existing = await self.get(identity.public_key)
        if isinstance(existing, Succeeded) and existing.value is not None:
            raise EntityExistsError(
                f"public key {identity.public_key.hex()} is already stored as "
                f"entity {existing.value.name!r} ({existing.value.id}); the "
                "stored row is unchanged"
            )
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

    async def get_by_id(self, entity_id: uuid.UUID) -> Outcome[EntityRecord | None]:
        """One stored identity by its row id, with no key material.

        The public key is what makes an identity an identity; the row id is what
        a room or a bot is bound *by*, so the rules those tables enforce look up
        this way round.
        """

        async def work(session: object) -> EntityRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(EntityRow).where(EntityRow.id == entity_id)
                )
            ).scalar_one_or_none()
            return None if row is None else _record(row)

        return await self.database.run("get_entity_by_id", work)

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

    async def recent(self, limit: int = DEFAULT_RECENT_PACKETS) -> Outcome[list[PacketLogRow]]:
        """The most recently recorded packets, newest first (design D14).

        The log's first read, and it stays a feed: this exists so a display can
        paint what happened before it connected, and nothing on the reception,
        dedup, dispatch or transmit path calls it. It goes through
        `Database.run` like every other operation, so a degraded database
        answers it as unavailable inside the operation bound rather than
        queueing it for later.

        The cap is a cap, not a suggestion: a caller asking for more than
        `MAX_RECENT_PACKETS` gets `MAX_RECENT_PACKETS`, because the bound on
        this statement's cost must not be settable by whoever is asking.
        """
        capped = max(1, min(int(limit), MAX_RECENT_PACKETS))

        async def work(session: object) -> list[PacketLogRow]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(PacketLogRowModel)
                    .order_by(PacketLogRowModel.at.desc(), PacketLogRowModel.id.desc())
                    .limit(capped)
                )
            ).scalars()
            return [_packet_log_row(row) for row in rows]

        return await self.database.run("read_recent_packets", work)

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


def _packet_log_row(row: PacketLogRowModel) -> PacketLogRow:
    """A stored packet as the shape it was written in.

    An undecodable frame comes back exactly as recorded — its `raw` bytes and
    its `reason` — because §4.1's rule is that a frame we could not decode must
    not become invisible, and a read that quietly returned it without its
    evidence would be the same disappearance one step later.
    """
    return PacketLogRow(
        packet_id=row.packet_id,
        direction=row.direction,
        at=row.at,
        outcome=row.outcome,
        route_type=row.route_type,
        payload_type=row.payload_type,
        path_bytes=None if row.path_bytes is None else bytes(row.path_bytes),
        hop_count=row.hop_count,
        size_bytes=row.size_bytes,
        snr_db=row.snr_db,
        rssi_dbm=row.rssi_dbm,
        airtime_ms=row.airtime_ms,
        entity_id=row.entity_id,
        priority_class=row.priority_class,
        reason=row.reason,
        raw=None if row.raw is None else bytes(row.raw),
    )


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

        # Being a room server is an explicit choice and never a side effect of
        # having a room bound to you. The rule lives here rather than in a
        # command handler because the browser creates rooms through this same
        # call and has to meet the same refusal.
        entity = await EntityRepository(database=self.database).get_by_id(entity_id)
        if isinstance(entity, Succeeded) and entity.value is not None:
            if entity.value.node_type is not NodeType.ROOM_SERVER:
                raise EntityRoleError(
                    f"{entity.value.name!r} adverts as "
                    f"{_node_type_name(entity.value.node_type)}, not a room "
                    "server; store the identity with node type ROOM_SERVER, "
                    "because being a room server is an explicit choice and "
                    "never a side effect of having a room bound to it"
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
        allow_read_only: bool | None = None,
    ) -> Outcome[bool]:
        """Rotate a password. **Evicts nobody**, because membership is keyed on
        the public key recorded at first login (§7) — this changes only what a
        *new* login is gated by.

        `guest_open` and `allow_read_only` are here for that reason rather than
        by convenience: `PasswordPolicy` is the two hashes and those two flags,
        and all four decide the same question — what a login that arrives now is
        answered with. `None` for any of them leaves it as it was."""

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
            if allow_read_only is not None:
                row.allow_read_only = allow_read_only
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
        self,
        room_id: uuid.UUID,
        *,
        limit: int = 100,
        newest_first: bool = False,
        before: int | None = None,
    ) -> Outcome[list[PostRecord]]:
        """One page of a room's history, in the protocol's own total order.

        `before` is a `post_timestamp` cursor and needs no tie-break: the column
        is unique per room (design D3), which is exactly what makes it a total
        order and therefore a page boundary that cannot show a message twice or
        skip one. Paging back is `newest_first` plus the oldest timestamp
        already shown.
        """

        async def work(session: object) -> list[PostRecord]:
            order = (
                MessageRow.post_timestamp.desc()
                if newest_first
                else MessageRow.post_timestamp
            )
            statement = (
                select(MessageRow)
                .where(MessageRow.room_id == room_id)
                .order_by(order)
                .limit(limit)
            )
            if before is not None:
                statement = statement.where(MessageRow.post_timestamp < before)
            rows = (await session.execute(statement)).scalars()  # type: ignore[attr-defined]
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


# --- Bots and their durable state (milestone 7, design D3/D16) --------------
#
# `bot` follows `room` line for line, and that is the point of design D3: the
# loader, the CLI noun and the startup reporting all have a direct analogue, so
# this milestone writes little new structure. `bot_state` does not follow
# `contact` or `path`: it is written *straight through* rather than through
# `db/writer.py`, because losing a contact costs a re-learn and losing a
# greeting record costs a duplicate greeting to a stranger — and the greeter
# needs the write to have landed before it transmits (design D16, D6).


class BotExistsError(RuntimeError):
    """The identity already carries a bot. Names the driver that holds it."""


class EntityHasRoleError(RuntimeError):
    """The identity already plays another role — today, a room server's."""


@dataclass(frozen=True, slots=True)
class BotRecord:
    """A bot as stored, with the identity it is bound to named beside it.

    `entity_name` is not a column: it is read from `entity` on the same query,
    because every place that shows a bot shows whose identity it speaks as, and
    a bot has no name of its own — it *is* its entity.
    """

    id: uuid.UUID
    entity_id: uuid.UUID
    driver: str
    enabled: bool
    mode: str
    config: dict
    created_at: dt.datetime
    entity_name: str = ""

    @property
    def active(self) -> bool:
        """Whether this bot may transmit at all. `observe` runs everything else."""
        return self.mode == "active"

    def as_json(self) -> dict[str, object]:
        return {
            "bot_id": str(self.id),
            "bot_name": self.entity_name,
            "entity_id": str(self.entity_id),
            "driver": self.driver,
            "mode": self.mode,
            "enabled": self.enabled,
        }


@dataclass(slots=True)
class BotRepository:
    """The `bot` table. One row per identity, enforced by the schema."""

    database: Database

    async def create(
        self,
        *,
        entity_id: uuid.UUID,
        driver: str,
        config: dict[str, object],
        mode: str = "observe",
        enabled: bool = True,
        entity_name: str = "",
        created_at: dt.datetime | None = None,
    ) -> Outcome[BotRecord]:
        """Bind a driver to an identity that carries no other role.

        Both refusals are checked before the insert so they can *name* what
        already holds the entity, which is what `sighop bot create` prints. The
        unique constraint stays the backstop: this check loses a race and the
        constraint does not.
        """
        record = BotRecord(
            id=uuid.uuid4(),
            entity_id=entity_id,
            driver=driver,
            enabled=enabled,
            mode=mode,
            config=dict(config),
            created_at=ensure_utc(created_at or dt.datetime.now(dt.UTC), field="bot.created_at"),
            entity_name=entity_name,
        )

        existing = await self.get_for_entity(entity_id)
        if isinstance(existing, Succeeded) and existing.value is not None:
            raise BotExistsError(
                f"this identity already carries the {existing.value.driver!r} bot; "
                "one identity is one node to the mesh, and a node plays one role"
            )
        room = await RoomRepository(database=self.database).get_for_entity(entity_id)
        if isinstance(room, Succeeded) and room.value is not None:
            raise EntityHasRoleError(
                f"this identity already serves the room {room.value.name!r}; an "
                "identity has one role, and a room server answers its members "
                "rather than acting on its own initiative"
            )

        async def work(session: object) -> BotRecord:
            session.add(  # type: ignore[attr-defined]
                BotRow(
                    id=record.id,
                    entity_id=record.entity_id,
                    driver=record.driver,
                    enabled=record.enabled,
                    mode=record.mode,
                    config=record.config,
                    created_at=record.created_at,
                )
            )
            return record

        return await self.database.run("create_bot", work)

    async def list_all(self) -> Outcome[list[BotRecord]]:
        """Every bot, each carrying the name of the identity it speaks as."""

        async def work(session: object) -> list[BotRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(BotRow, EntityRow.name)
                    .join(EntityRow, EntityRow.id == BotRow.entity_id)
                    .order_by(BotRow.created_at)
                )
            ).all()
            return [_bot(row, name) for row, name in rows]

        return await self.database.run("list_bots", work)

    async def get_for_entity(self, entity_id: uuid.UUID) -> Outcome[BotRecord | None]:
        async def work(session: object) -> BotRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(BotRow, EntityRow.name)
                    .join(EntityRow, EntityRow.id == BotRow.entity_id)
                    .where(BotRow.entity_id == entity_id)
                )
            ).one_or_none()
            return None if row is None else _bot(row[0], row[1])

        return await self.database.run("get_bot_for_entity", work)

    async def get_by_name(self, name: str) -> Outcome[BotRecord | None]:
        """One bot, by the name of the identity it runs on.

        A bot has no name of its own by design (D3) — it is a role an identity
        plays — so this is the only name there is to look one up by.
        """

        async def work(session: object) -> BotRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(BotRow, EntityRow.name)
                    .join(EntityRow, EntityRow.id == BotRow.entity_id)
                    .where(EntityRow.name == name)
                )
            ).one_or_none()
            return None if row is None else _bot(row[0], row[1])

        return await self.database.run("get_bot_by_name", work)

    async def set_enabled(self, bot_id: uuid.UUID, enabled: bool) -> Outcome[bool]:
        return await self._update(bot_id, "set_bot_enabled", enabled=enabled)

    async def set_mode(self, bot_id: uuid.UUID, mode: str) -> Outcome[bool]:
        """Move a bot between observe and active. The whole of the transmit
        decision that belongs to the bot; the run's own gate is separate."""
        return await self._update(bot_id, "set_bot_mode", mode=mode)

    async def set_config(self, bot_id: uuid.UUID, config: dict[str, object]) -> Outcome[bool]:
        """Replace the stored configuration whole.

        Whole rather than merged: the caller has already read it, applied the
        driver's validator to the change and rejected what the driver refused,
        so a merge here would be a second, silent policy.
        """
        return await self._update(bot_id, "set_bot_config", config=dict(config))

    async def _update(self, bot_id: uuid.UUID, operation: str, **values: object) -> Outcome[bool]:
        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                update(BotRow).where(BotRow.id == bot_id).values(**values)
            )
            return bool(result.rowcount)

        return await self.database.run(operation, work)


def _bot(row: BotRow, entity_name: str = "") -> BotRecord:
    return BotRecord(
        id=row.id,
        entity_id=row.entity_id,
        driver=row.driver,
        enabled=row.enabled,
        mode=row.mode,
        config=dict(row.config or {}),
        created_at=row.created_at,
        entity_name=entity_name,
    )


@dataclass(slots=True)
class BotStateRepository:
    """The `bot_state` table: a driver's only durable memory.

    Every method is a bounded call under the connect and statement timeouts
    `db/engine.py` already sets, made from the bot's worker task and never from
    the reception path. Nothing is buffered and nothing is batched: design D6
    needs a greeting record to have *landed* before the greeting is transmitted,
    and a queue would answer "written" before that was true.
    """

    database: Database

    async def get(self, bot_id: uuid.UUID, key: str) -> Outcome[object | None]:
        async def work(session: object) -> object | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(BotStateRow).where(
                        BotStateRow.bot_id == bot_id, BotStateRow.key == key
                    )
                )
            ).scalar_one_or_none()
            return None if row is None else row.value

        return await self.database.run("get_bot_state", work)

    async def set(
        self, bot_id: uuid.UUID, key: str, value: object, *, at: dt.datetime | None = None
    ) -> Outcome[int]:
        """Write one key. Upserted, because writing a key twice is ordinary."""
        updated = ensure_utc(at or dt.datetime.now(dt.UTC), field="bot_state.updated_at")
        values = {"bot_id": bot_id, "key": key, "value": value, "updated_at": updated}

        async def work(session: object) -> int:
            statement = insert(BotStateRow).values([values])
            await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_update(
                    constraint="pk_bot_state",
                    set_={
                        "value": statement.excluded.value,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
            return 1

        return await self.database.run("set_bot_state", work)

    async def set_many(
        self,
        bot_id: uuid.UUID,
        entries: dict[str, object],
        *,
        at: dt.datetime | None = None,
    ) -> Outcome[int]:
        """Write many keys at once, for seeding a new bot (design D7).

        One statement rather than a call per key: a greeter created on an
        established node seeds one row per contact, and a table of a few
        thousand would otherwise be a few thousand round trips at a moment the
        operator is watching. `DO NOTHING` on conflict, because seeding must
        never overwrite a record that says a greeting was actually sent.
        """
        if not entries:
            return Succeeded(value=0)
        updated = ensure_utc(at or dt.datetime.now(dt.UTC), field="bot_state.updated_at")
        values = [
            {"bot_id": bot_id, "key": key, "value": value, "updated_at": updated}
            for key, value in entries.items()
        ]

        async def work(session: object) -> int:
            statement = insert(BotStateRow).values(values)
            result = await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_nothing(constraint="pk_bot_state")
            )
            return int(result.rowcount or 0)

        return await self.database.run("seed_bot_state", work)

    async def delete(self, bot_id: uuid.UUID, key: str) -> Outcome[bool]:
        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                delete(BotStateRow).where(
                    BotStateRow.bot_id == bot_id, BotStateRow.key == key
                )
            )
            return bool(result.rowcount)

        return await self.database.run("delete_bot_state", work)

    async def list(self, bot_id: uuid.UUID) -> Outcome[dict[str, object]]:
        """Everything one bot has stored, which is what `sighop bot state` shows."""

        async def work(session: object) -> dict[str, object]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(BotStateRow)
                    .where(BotStateRow.bot_id == bot_id)
                    .order_by(BotStateRow.key)
                )
            ).scalars()
            return {row.key: row.value for row in rows}

        return await self.database.run("list_bot_state", work)

    async def clear(self, bot_id: uuid.UUID) -> Outcome[int]:
        """Forget everything one bot knows. Reports how many keys went."""

        async def work(session: object) -> int:
            result = await session.execute(  # type: ignore[attr-defined]
                delete(BotStateRow).where(BotStateRow.bot_id == bot_id)
            )
            return int(result.rowcount or 0)

        return await self.database.run("clear_bot_state", work)


# --- Direct messages (milestone 8, design D7/D8) ----------------------------


DEFAULT_CONVERSATION_PAGE = 50
"""How many messages one page of a conversation holds by default. Bounded for
the reason every read here is bounded: an unbounded read is a statement whose
cost is set by how long the deployment has been running."""


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """One conversation, as the list of them shows it.

    A conversation is the pair of a local identity and a peer, never the peer
    alone: two identities talking to one contact are two conversations, and
    merging them would attribute one identity's words to the other.
    """

    entity_public_key: bytes
    peer_public_key: bytes
    messages: int
    latest_at: dt.datetime
    latest_direction: str
    latest_outcome: RecordedOutcome
    latest_text: bytes

    def as_json(self) -> dict[str, object]:
        return {
            "entity_public_key": self.entity_public_key.hex(),
            "peer_public_key": self.peer_public_key.hex(),
            "messages": self.messages,
            "latest_direction": self.latest_direction,
            "latest_outcome": str(self.latest_outcome),
        }


@dataclass(slots=True)
class DirectMessageRepository:
    """The `direct_message` table: one row per message, updated in place.

    Read directly rather than mirrored in memory, for `MessageRepository`'s
    reason (design D5): a conversation is unbounded, exists to outlive the
    process, and is read by a browser rather than by anything on the packet
    path. Nothing in `net/` consults it.
    """

    database: Database

    async def upsert_many(self, records: Sequence[DirectMessageRecord]) -> Outcome[int]:
        """Write a batch, collapsing it to one row per `(entity, ref)`.

        Milestone 5's `cannot affect row a second time` finding applies here more
        sharply than anywhere else it has: a send offers its record at submission
        and again when it resolves, and the two land in the *same* batch whenever
        the send is quick or the writer is behind. Collapsing to the latest is
        exactly right — the later offer carries the earlier one's message with
        its outcome known.

        `handled_at` is deliberately absent from the update, as
        `contact.first_heard` is: it is when the platform first handled this
        message, and a conversation ordered by it must not have a message move
        because its send resolved.
        """
        if not records:
            return Succeeded(value=0)
        values = _latest_per_key(
            ((record.entity_public_key, record.ref), _direct_message_values(record))
            for record in records
        )

        async def work(session: object) -> int:
            statement = insert(DirectMessageRow).values(values)
            await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_update(
                    index_elements=[
                        DirectMessageRow.entity_public_key,
                        DirectMessageRow.ref,
                    ],
                    set_={
                        "peer_public_key": statement.excluded.peer_public_key,
                        "direction": statement.excluded.direction,
                        "text": statement.excluded.text,
                        "wire_timestamp": statement.excluded.wire_timestamp,
                        "packet_ids": statement.excluded.packet_ids,
                        "attempts": statement.excluded.attempts,
                        "route_flood": statement.excluded.route_flood,
                        "route_path": statement.excluded.route_path,
                        "outcome": statement.excluded.outcome,
                        "ack_latency_ms": statement.excluded.ack_latency_ms,
                    },
                )
            )
            return len(values)

        return await self.database.run("upsert_direct_messages", work)

    async def upsert(self, record: DirectMessageRecord) -> Outcome[int]:
        return await self.upsert_many([record])

    async def conversation(
        self,
        entity_public_key: bytes,
        peer_public_key: bytes,
        *,
        limit: int = DEFAULT_CONVERSATION_PAGE,
        before: tuple[dt.datetime, int] | None = None,
    ) -> Outcome[list[DirectMessageRecord]]:
        """One page of a conversation, newest first.

        Ordered by `handled_at` and never by `wire_timestamp`: the wire value is
        the peer's clock, and a peer with a wrong clock must not be able to
        reorder a conversation (design D7). The row id breaks ties, which is what
        makes the page boundary stable — two messages handled in the same
        millisecond would otherwise be able to swap across a page and be shown
        twice or not at all.

        `before` is the `(handled_at, row_id)` of the oldest message already
        shown; the next page is everything strictly older than it.
        """

        async def work(session: object) -> list[DirectMessageRecord]:
            statement = (
                select(DirectMessageRow)
                .where(
                    DirectMessageRow.entity_public_key == entity_public_key,
                    DirectMessageRow.peer_public_key == peer_public_key,
                )
                .order_by(DirectMessageRow.handled_at.desc(), DirectMessageRow.id.desc())
                .limit(limit)
            )
            if before is not None:
                statement = statement.where(
                    tuple_(DirectMessageRow.handled_at, DirectMessageRow.id)
                    < tuple_(
                        literal(ensure_utc(before[0], field="direct_message.handled_at")),
                        literal(before[1]),
                    )
                )
            rows = (await session.execute(statement)).scalars()  # type: ignore[attr-defined]
            return [_direct_message(row) for row in rows]

        return await self.database.run("read_conversation", work)

    async def conversations(
        self, entity_public_key: bytes | None = None
    ) -> Outcome[list[ConversationSummary]]:
        """Every conversation, most recently active first.

        Two statements in one unit of work rather than one clever one: the latest
        message per peer is a `DISTINCT ON`, the message count per peer is a
        grouped count, and joining them in SQL would produce a query harder to
        read than the two it replaced for no gain a browser could measure.
        """

        async def work(session: object) -> list[ConversationSummary]:
            keys = (DirectMessageRow.entity_public_key, DirectMessageRow.peer_public_key)
            latest = (
                select(DirectMessageRow)
                .distinct(*keys)
                .order_by(
                    *keys, DirectMessageRow.handled_at.desc(), DirectMessageRow.id.desc()
                )
            )
            counts = select(*keys, func.count().label("messages")).group_by(*keys)
            if entity_public_key is not None:
                latest = latest.where(DirectMessageRow.entity_public_key == entity_public_key)
                counts = counts.where(DirectMessageRow.entity_public_key == entity_public_key)

            totals = {
                (bytes(entity), bytes(peer)): int(messages)
                for entity, peer, messages in (
                    await session.execute(counts)  # type: ignore[attr-defined]
                ).all()
            }
            summaries = [
                ConversationSummary(
                    entity_public_key=bytes(row.entity_public_key),
                    peer_public_key=bytes(row.peer_public_key),
                    messages=totals.get(
                        (bytes(row.entity_public_key), bytes(row.peer_public_key)), 0
                    ),
                    latest_at=row.handled_at,
                    latest_direction=row.direction,
                    latest_outcome=_recorded_outcome(row.outcome),
                    latest_text=bytes(row.text),
                )
                for row in (await session.execute(latest)).scalars()  # type: ignore[attr-defined]
            ]
            summaries.sort(key=lambda summary: summary.latest_at, reverse=True)
            return summaries

        return await self.database.run("list_conversations", work)

    async def count(self) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(DirectMessageRow)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_direct_messages", work)

    async def conversation_count(self) -> Outcome[int]:
        """How many distinct identity-and-peer pairs the table holds."""

        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(
                        func.count(
                            func.distinct(
                                tuple_(
                                    DirectMessageRow.entity_public_key,
                                    DirectMessageRow.peer_public_key,
                                )
                            )
                        )
                    )
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_conversations", work)


def _direct_message_values(record: DirectMessageRecord) -> dict[str, object]:
    return {
        "entity_public_key": record.entity_public_key,
        "peer_public_key": record.peer_public_key,
        "direction": record.direction,
        "text": record.text,
        "wire_timestamp": record.wire_timestamp,
        "handled_at": ensure_utc(record.handled_at, field="direct_message.handled_at"),
        "ref": record.ref,
        "packet_ids": list(record.packet_ids),
        "attempts": record.attempts,
        "route_flood": record.route_flood,
        "route_path": record.route_path,
        "outcome": str(record.outcome),
        "ack_latency_ms": record.ack_latency_ms,
    }


def _recorded_outcome(stored: str) -> RecordedOutcome:
    """A stored outcome, or `IN_FLIGHT` for one this build does not know.

    A row written by a later build must not make a conversation unreadable, and
    the honest reading of an outcome we cannot interpret is that we do not know
    how the message ended.
    """
    try:
        return RecordedOutcome(stored)
    except ValueError:
        return RecordedOutcome.IN_FLIGHT


def _direct_message(row: DirectMessageRow) -> DirectMessageRecord:
    return DirectMessageRecord(
        entity_public_key=bytes(row.entity_public_key),
        peer_public_key=bytes(row.peer_public_key),
        direction=row.direction,
        text=bytes(row.text),
        wire_timestamp=int(row.wire_timestamp),
        handled_at=row.handled_at,
        ref=row.ref,
        outcome=_recorded_outcome(row.outcome),
        packet_ids=tuple(row.packet_ids),
        attempts=int(row.attempts),
        route_flood=row.route_flood,
        route_path=None if row.route_path is None else bytes(row.route_path),
        ack_latency_ms=row.ack_latency_ms,
        row_id=int(row.id),
    )


# --- Web accounts (milestone 9) ---------------------------------------------
#
# The operators who sign in to the web interface. Accounts are managed from the
# terminal only (milestone 9 design D2), read by the panel at sign-in and at
# each session's revalidation, and never rendered with their hash.

MAX_USERNAME_LENGTH = 64


class UsernameError(ValueError):
    """A username that cannot be stored. Says which rule it broke."""


class WebUserExistsError(RuntimeError):
    """An account with this normalised username exists. Names the existing one."""


def normalise_username(value: str) -> str:
    """The one form a username is stored, compared and looked up in (design D1).

    NFKC first, so a compatibility form (a full-width letter, a ligature) is the
    letter it stands for; then `casefold()`, which is `lower()` done properly for
    the scripts where the two differ. The same function is used by the
    repository, the command line and sign-in, so "differs only in case" cannot
    mean one thing in one surface and another in the next.

    Empty, whitespace and control characters are refused rather than stripped:
    a username that silently lost a character is a different username.
    """
    normalised = unicodedata.normalize("NFKC", value).casefold()
    if not normalised:
        raise UsernameError("a username cannot be empty")
    if len(normalised) > MAX_USERNAME_LENGTH:
        raise UsernameError(
            f"a username is at most {MAX_USERNAME_LENGTH} characters; this one is "
            f"{len(normalised)}"
        )
    for character in normalised:
        if character.isspace():
            raise UsernameError("a username cannot contain whitespace")
        if unicodedata.category(character).startswith("C"):
            raise UsernameError(
                f"a username cannot contain control or unassigned characters "
                f"(found U+{ord(character):04X})"
            )
    return normalised


@dataclass(frozen=True, slots=True, repr=False)
class WebUserRecord:
    """One account as stored, hash included, rendered without it.

    The hash is here because sign-in has to verify against it and there is
    nowhere else for it to live. `repr` and `as_json` are the two rendering
    paths and neither carries it — a traceback is a rendering path too.
    """

    id: uuid.UUID
    username: str
    password_hash: str
    enabled: bool
    created_at: dt.datetime
    password_set_at: dt.datetime

    def __repr__(self) -> str:
        return (
            f"WebUserRecord(id={self.id}, username={self.username!r}, "
            f"password_hash=<redacted>, enabled={self.enabled}, "
            f"created_at={self.created_at.isoformat()}, "
            f"password_set_at={self.password_set_at.isoformat()})"
        )

    def as_json(self) -> dict[str, object]:
        return {
            "web_user_id": str(self.id),
            "username": self.username,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "password_set_at": self.password_set_at.isoformat(),
        }


@dataclass(slots=True)
class WebUserRepository:
    """The `web_user` table. Every username argument is normalised here."""

    database: Database

    async def add(
        self,
        username: str,
        *,
        password_hash: str,
        enabled: bool = True,
        created_at: dt.datetime | None = None,
    ) -> Outcome[WebUserRecord]:
        """Store a new account, refusing a username that already exists.

        Refused *here*, naming the stored account, for the reason
        `EntityRepository.store` refuses here. The unique constraint on the
        normalised column stays the backstop: this check loses a race and the
        constraint does not.
        """
        name = normalise_username(username)
        existing = await self.get(name)
        if isinstance(existing, Succeeded) and existing.value is not None:
            raise WebUserExistsError(
                f"an account named {existing.value.username!r} already exists "
                f"({existing.value.id}); usernames are compared without regard to "
                "case, and the stored account is unchanged"
            )
        at = ensure_utc(created_at or dt.datetime.now(dt.UTC), field="web_user.created_at")
        record = WebUserRecord(
            id=uuid.uuid4(),
            username=name,
            password_hash=password_hash,
            enabled=enabled,
            created_at=at,
            password_set_at=at,
        )

        async def work(session: object) -> WebUserRecord:
            session.add(  # type: ignore[attr-defined]
                WebUserRow(
                    id=record.id,
                    username=record.username,
                    password_hash=record.password_hash,
                    enabled=record.enabled,
                    created_at=record.created_at,
                    password_set_at=record.password_set_at,
                )
            )
            return record

        return await self.database.run("add_web_user", work)

    async def add_first(
        self, username: str, *, password_hash: str, created_at: dt.datetime | None = None
    ) -> Outcome[WebUserRecord | None]:
        """Store an enabled account only if the table holds none at all.

        First-run setup's one write (web-first-run-setup design D6). The count
        and the insert are one transaction under `SHARE ROW EXCLUSIVE`, which
        conflicts with itself and with the `ROW EXCLUSIVE` lock a plain `add`'s
        insert takes: two setups serialise, and a terminal `add` either commits
        first (this sees it and inserts nothing) or waits for this to commit.
        `INSERT … WHERE NOT EXISTS` without the lock would not do: under READ
        COMMITTED two concurrent statements both see an empty table.

        `None` means an account already existed, disabled ones included.
        """
        name = normalise_username(username)
        at = ensure_utc(created_at or dt.datetime.now(dt.UTC), field="web_user.created_at")
        record = WebUserRecord(
            id=uuid.uuid4(),
            username=name,
            password_hash=password_hash,
            enabled=True,
            created_at=at,
            password_set_at=at,
        )

        async def work(session: object) -> WebUserRecord | None:
            await session.execute(  # type: ignore[attr-defined]
                sql_text("LOCK TABLE web_user IN SHARE ROW EXCLUSIVE MODE")
            )
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(WebUserRow)
                )
            ).scalar_one()
            if total:
                return None
            session.add(  # type: ignore[attr-defined]
                WebUserRow(
                    id=record.id,
                    username=record.username,
                    password_hash=record.password_hash,
                    enabled=record.enabled,
                    created_at=record.created_at,
                    password_set_at=record.password_set_at,
                )
            )
            await session.flush()  # type: ignore[attr-defined]
            return record

        return await self.database.run("add_first_web_user", work)

    async def get(self, username: str) -> Outcome[WebUserRecord | None]:
        name = normalise_username(username)

        async def work(session: object) -> WebUserRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(WebUserRow).where(WebUserRow.username == name)
                )
            ).scalar_one_or_none()
            return None if row is None else _web_user(row)

        return await self.database.run("get_web_user", work)

    async def list(self) -> Outcome[tuple[WebUserRecord, ...]]:
        async def work(session: object) -> tuple[WebUserRecord, ...]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(WebUserRow).order_by(WebUserRow.created_at, WebUserRow.username)
                )
            ).scalars()
            return tuple(_web_user(row) for row in rows)

        return await self.database.run("list_web_users", work)

    async def set_password(
        self, username: str, *, password_hash: str, at: dt.datetime | None = None
    ) -> Outcome[bool]:
        """Replace the hash and move the credential epoch, in one statement.

        Moving `password_set_at` is what ends every session issued under the old
        password at its next revalidation; a hash change that left it alone
        would leave those sessions signed in.
        """
        when = ensure_utc(at or dt.datetime.now(dt.UTC), field="web_user.password_set_at")
        return await self._update(
            username, "set_web_user_password", password_hash=password_hash, password_set_at=when
        )

    async def set_enabled(self, username: str, enabled: bool) -> Outcome[bool]:
        return await self._update(username, "set_web_user_enabled", enabled=enabled)

    async def remove(self, username: str) -> Outcome[bool]:
        name = normalise_username(username)

        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                delete(WebUserRow).where(WebUserRow.username == name)
            )
            return bool(result.rowcount)

        return await self.database.run("remove_web_user", work)

    async def count(self) -> Outcome[int]:
        """Every account, enabled or not. Zero is what offers first-run setup."""

        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(WebUserRow)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_web_users", work)

    async def count_enabled(self) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count())
                    .select_from(WebUserRow)
                    .where(WebUserRow.enabled.is_(True))
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_enabled_web_users", work)

    async def _update(self, username: str, operation: str, **values: object) -> Outcome[bool]:
        name = normalise_username(username)

        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                update(WebUserRow).where(WebUserRow.username == name).values(**values)
            )
            return bool(result.rowcount)

        return await self.database.run(operation, work)


def _web_user(row: WebUserRow) -> WebUserRecord:
    return WebUserRecord(
        id=row.id,
        username=row.username,
        password_hash=row.password_hash,
        enabled=row.enabled,
        created_at=row.created_at,
        password_set_at=row.password_set_at,
    )


# --- Webhooks (webhook-notifications) ---------------------------------------
#
# Stored configuration read by the dispatcher at each event, and written by the
# command line and the panel through the same methods (design D4, D7). The URL
# is sealed on the way in and opened only for delivery; nothing here returns it
# to a caller that renders.

MAX_FAILURE_REASON_LENGTH = 500


class _Unset:
    """A keyword left out, as distinct from one given as `None`."""


UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class WebhookRecord:
    """One webhook as stored, with its target reduced to scheme and host.

    Carries no URL, sealed or open: every place a record is shown is a place the
    URL must not be (the `webhooks` requirement), so the type cannot leak it.
    """

    id: uuid.UUID
    name: str
    url_host: str
    format: str
    triggers: tuple[str, ...]
    max_hops: int | None
    enabled: bool
    created_at: dt.datetime
    last_delivered_at: dt.datetime | None = None
    last_failed_at: dt.datetime | None = None
    last_failure: str | None = None

    @property
    def plaintext_http(self) -> bool:
        return self.url_host.startswith("http://")

    def as_json(self) -> dict[str, object]:
        return {
            "webhook_id": str(self.id),
            "webhook_name": self.name,
            "url_host": self.url_host,
            "format": self.format,
            "triggers": list(self.triggers),
            "max_hops": self.max_hops,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True, repr=False)
class OpenedWebhook:
    """A record with its URL opened for delivery, or the reason it could not be.

    `repr` omits the URL: a traceback is a rendering path too.
    """

    record: WebhookRecord
    url: str | None
    error: str | None = None

    def __repr__(self) -> str:
        state = "<redacted>" if self.url is not None else f"unsealable: {self.error}"
        return f"OpenedWebhook(name={self.record.name!r}, url={state})"


@dataclass(slots=True)
class WebhookRepository:
    """The `webhook` table. Every setting is validated here, before any write."""

    database: Database

    async def create(
        self,
        *,
        name: str,
        url: str,
        format: str,
        triggers: Iterable[str],
        max_hops: int | str | None = None,
        secret: bytes,
        created_at: dt.datetime | None = None,
    ) -> Outcome[WebhookRecord]:
        """Store a new, enabled webhook, refusing anything the rules refuse.

        The name clash is checked here so the refusal can name it; the unique
        constraint stays the backstop for the race this check loses.
        """
        checked_name = parse_name(name)
        parsed = parse_url(url)
        checked_format = parse_format(format)
        checked_triggers = parse_triggers(triggers)
        checked_hops = parse_max_hops(max_hops)
        existing = await self.get_by_name(checked_name)
        if isinstance(existing, Succeeded) and existing.value is not None:
            raise WebhookExistsError(
                f"a webhook named {checked_name!r} already exists; the stored webhook "
                "is unchanged"
            )
        sealed = seal_value(parsed.url.encode("utf-8"), secret)
        record = WebhookRecord(
            id=uuid.uuid4(),
            name=checked_name,
            url_host=parsed.url_host,
            format=checked_format.value,
            triggers=tuple(trigger.value for trigger in checked_triggers),
            max_hops=checked_hops,
            enabled=True,
            created_at=ensure_utc(
                created_at or dt.datetime.now(dt.UTC), field="webhook.created_at"
            ),
        )

        async def work(session: object) -> WebhookRecord:
            session.add(  # type: ignore[attr-defined]
                WebhookRow(
                    id=record.id,
                    name=record.name,
                    sealed_url=sealed,
                    url_host=record.url_host,
                    format=record.format,
                    triggers=list(record.triggers),
                    max_hops=record.max_hops,
                    enabled=record.enabled,
                    created_at=record.created_at,
                )
            )
            return record

        return await self.database.run("create_webhook", work)

    async def list_all(self) -> Outcome[list[WebhookRecord]]:
        async def work(session: object) -> list[WebhookRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(WebhookRow).order_by(WebhookRow.created_at, WebhookRow.name)
                )
            ).scalars()
            return [_webhook(row) for row in rows]

        return await self.database.run("list_webhooks", work)

    async def list_enabled(self, secret: bytes) -> Outcome[list[OpenedWebhook]]:
        """Every enabled webhook with its URL opened, one row's failure its own.

        Opening happens outside `run`, for `EntityRepository.load_all`'s reason:
        a wrong secret or an altered row is not a database fault, and a row
        that does not open must not take the other webhooks with it.
        """

        async def work(session: object) -> list[tuple[WebhookRecord, bytes]]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(WebhookRow)
                    .where(WebhookRow.enabled.is_(True))
                    .order_by(WebhookRow.created_at, WebhookRow.name)
                )
            ).scalars()
            return [(_webhook(row), bytes(row.sealed_url)) for row in rows]

        outcome = await self.database.run("list_enabled_webhooks", work)
        if not isinstance(outcome, Succeeded):
            return outcome
        return Succeeded(
            [_open_webhook(record, sealed, secret) for record, sealed in outcome.value]
        )

    async def open_url(
        self, webhook_id: uuid.UUID, secret: bytes
    ) -> Outcome[OpenedWebhook | None]:
        """One webhook with its URL opened, enabled or not — for a test send."""

        async def work(session: object) -> tuple[WebhookRecord, bytes] | None:
            row = await session.get(WebhookRow, webhook_id)  # type: ignore[attr-defined]
            return None if row is None else (_webhook(row), bytes(row.sealed_url))

        outcome = await self.database.run("open_webhook", work)
        if not isinstance(outcome, Succeeded):
            return outcome
        if outcome.value is None:
            return Succeeded(None)
        record, sealed = outcome.value
        return Succeeded(_open_webhook(record, sealed, secret))

    async def get_by_name(self, name: str) -> Outcome[WebhookRecord | None]:
        wanted = name.strip()

        async def work(session: object) -> WebhookRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(WebhookRow).where(WebhookRow.name == wanted)
                )
            ).scalar_one_or_none()
            return None if row is None else _webhook(row)

        return await self.database.run("get_webhook_by_name", work)

    async def get_by_id(self, webhook_id: uuid.UUID) -> Outcome[WebhookRecord | None]:
        async def work(session: object) -> WebhookRecord | None:
            row = await session.get(WebhookRow, webhook_id)  # type: ignore[attr-defined]
            return None if row is None else _webhook(row)

        return await self.database.run("get_webhook", work)

    async def set_enabled(self, webhook_id: uuid.UUID, enabled: bool) -> Outcome[bool]:
        return await self._update(webhook_id, "set_webhook_enabled", enabled=enabled)

    async def update(
        self,
        webhook_id: uuid.UUID,
        *,
        triggers: Iterable[str] | None = None,
        format: str | None = None,
        max_hops: int | str | _Unset | None = UNSET,
    ) -> Outcome[bool]:
        """Change what was given, validating all of it before writing any.

        `max_hops=None` removes the limit; leaving it out keeps the stored one.
        """
        values: dict[str, object] = {}
        if triggers is not None:
            values["triggers"] = [trigger.value for trigger in parse_triggers(triggers)]
        if format is not None:
            values["format"] = parse_format(format).value
        if not isinstance(max_hops, _Unset):
            values["max_hops"] = parse_max_hops(max_hops)
        if not values:
            return await self._exists(webhook_id)
        return await self._update(webhook_id, "update_webhook", **values)

    async def set_url(
        self, webhook_id: uuid.UUID, url: str, *, secret: bytes
    ) -> Outcome[str | None]:
        """Replace the URL. The new scheme and host, or `None` for no such webhook."""
        parsed = parse_url(url)
        sealed = seal_value(parsed.url.encode("utf-8"), secret)
        outcome = await self._update(
            webhook_id, "set_webhook_url", sealed_url=sealed, url_host=parsed.url_host
        )
        if not isinstance(outcome, Succeeded):
            return outcome
        return Succeeded(parsed.url_host if outcome.value else None)

    async def remove(self, webhook_id: uuid.UUID) -> Outcome[bool]:
        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                delete(WebhookRow).where(WebhookRow.id == webhook_id)
            )
            return bool(result.rowcount)

        return await self.database.run("remove_webhook", work)

    async def record_delivery(self, webhook_id: uuid.UUID, at: dt.datetime) -> Outcome[bool]:
        return await self._update(
            webhook_id,
            "record_webhook_delivery",
            last_delivered_at=ensure_utc(at, field="webhook.last_delivered_at"),
        )

    async def record_failure(
        self, webhook_id: uuid.UUID, at: dt.datetime, reason: str
    ) -> Outcome[bool]:
        return await self._update(
            webhook_id,
            "record_webhook_failure",
            last_failed_at=ensure_utc(at, field="webhook.last_failed_at"),
            last_failure=reason[:MAX_FAILURE_REASON_LENGTH],
        )

    async def _exists(self, webhook_id: uuid.UUID) -> Outcome[bool]:
        outcome = await self.get_by_id(webhook_id)
        if not isinstance(outcome, Succeeded):
            return outcome
        return Succeeded(outcome.value is not None)

    async def _update(
        self, webhook_id: uuid.UUID, operation: str, **values: object
    ) -> Outcome[bool]:
        async def work(session: object) -> bool:
            result = await session.execute(  # type: ignore[attr-defined]
                update(WebhookRow).where(WebhookRow.id == webhook_id).values(**values)
            )
            return bool(result.rowcount)

        return await self.database.run(operation, work)


def _webhook(row: WebhookRow) -> WebhookRecord:
    return WebhookRecord(
        id=row.id,
        name=row.name,
        url_host=row.url_host,
        format=row.format,
        triggers=tuple(row.triggers or ()),
        max_hops=row.max_hops,
        enabled=row.enabled,
        created_at=row.created_at,
        last_delivered_at=row.last_delivered_at,
        last_failed_at=row.last_failed_at,
        last_failure=row.last_failure,
    )


def _open_webhook(record: WebhookRecord, sealed: bytes, secret: bytes) -> OpenedWebhook:
    try:
        opened = open_value(sealed, secret, what=f"webhook {record.name!r}")
        return OpenedWebhook(record=record, url=opened.decode("utf-8"))
    except (SealError, UnicodeDecodeError) as exc:
        return OpenedWebhook(record=record, url=None, error=str(exc))


# --- Channels (change `channel-messaging`) ------------------------------------
#
# The station's group channels and what was said in them. Every rule a stored
# channel obeys is here, so `sighop channel` and the panel refuse identically and
# in the same words (design D9, the `webui-write-parity` lesson). No refusal
# repeats a pre-shared key: a refusal is printed to a terminal and rendered into
# a page.

MAX_CHANNEL_NAME_LENGTH = 64
PUBLIC_CHANNEL_NAME = "Public"
PSK_SIZES = (16, 32)
DEFAULT_CHANNEL_PAGE = 50


class ChannelConfigError(ValueError):
    """A channel that cannot be stored. Says which rule it broke."""


class ChannelExistsError(ChannelConfigError):
    """The name, or the key, is already stored."""


GUESSABLE_STATEMENT = (
    "anyone who knows or guesses its name can derive the key, and read and post in it"
)
"""Said wherever a hashtag or Public channel is added or listed."""


def parse_channel_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ChannelConfigError("a channel name cannot be empty")
    if len(name) > MAX_CHANNEL_NAME_LENGTH:
        raise ChannelConfigError(
            f"a channel name is at most {MAX_CHANNEL_NAME_LENGTH} characters; this one "
            f"is {len(name)}"
        )
    for character in name:
        if unicodedata.category(character).startswith("C"):
            raise ChannelConfigError(
                "a channel name cannot contain control or unassigned characters "
                f"(found U+{ord(character):04X})"
            )
    return name


def parse_hashtag(value: str) -> str:
    """A hashtag with its leading `#`, which is part of the hashed bytes."""
    tag = value.strip()
    if not tag.startswith("#"):
        tag = f"#{tag}"
    if len(tag) == 1:
        raise ChannelConfigError("a hashtag cannot be empty")
    if any(character.isspace() for character in tag):
        raise ChannelConfigError("a hashtag cannot contain whitespace")
    return parse_channel_name(tag)


def parse_psk(value: str) -> bytes:
    """A base64 pre-shared key of 16 or 32 bytes. The refusal never echoes it."""
    text = "".join(value.split())
    if not text:
        raise ChannelConfigError("a pre-shared key cannot be empty")
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChannelConfigError("the pre-shared key does not decode as base64") from exc
    if len(key) not in PSK_SIZES:
        raise ChannelConfigError(
            f"the pre-shared key decodes to {len(key)} bytes; a channel key is 16 or 32 bytes"
        )
    return key


@dataclass(frozen=True, slots=True)
class ChannelRecord:
    """One stored channel. Carries no key, sealed or open, so it cannot leak one."""

    id: int
    name: str
    kind: ChannelKind
    channel_hash: int
    created_at: dt.datetime
    hashtag: str | None = None

    @property
    def guessable(self) -> bool:
        return self.kind.guessable

    def as_json(self) -> dict[str, object]:
        return {
            "channel_id": self.id,
            "channel": self.name,
            "kind": str(self.kind),
            "channel_hash": f"{self.channel_hash:02x}",
            "guessable": self.guessable,
        }


@dataclass(slots=True)
class ChannelRepository:
    """The `channel` table. Every rule is checked here, before any write."""

    database: Database

    async def add_public(self, *, name: str = PUBLIC_CHANNEL_NAME) -> Outcome[ChannelRecord]:
        return await self._add(
            parse_channel_name(name), ChannelKind.PUBLIC, PUBLIC_CHANNEL_KEY, secret=None
        )

    async def add_hashtag(
        self, hashtag: str, *, name: str | None = None, secret: bytes | None = None
    ) -> Outcome[ChannelRecord]:
        """`secret`, when given, lets the duplicate-key check open stored pre-shared keys."""
        tag = parse_hashtag(hashtag)
        return await self._add(
            parse_channel_name(name or tag),
            ChannelKind.HASHTAG,
            channel_key_from_hashtag(tag),
            hashtag=tag,
            secret=secret,
        )

    async def add_psk(self, key: str, *, name: str, secret: bytes) -> Outcome[ChannelRecord]:
        checked_name = parse_channel_name(name)
        raw = parse_psk(key)
        return await self._add(
            checked_name,
            ChannelKind.PSK,
            ChannelKey(key=raw),
            sealed=seal_value(raw, secret),
            secret=secret,
        )

    async def _add(
        self,
        name: str,
        kind: ChannelKind,
        key: ChannelKey,
        *,
        secret: bytes | None,
        hashtag: str | None = None,
        sealed: bytes | None = None,
    ) -> Outcome[ChannelRecord]:
        """Refuse a name or key already stored; the unique constraint backs the name race."""

        async def read(session: object) -> list[tuple[ChannelRecord, bytes | None]]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelRow).order_by(ChannelRow.id)
                )
            ).scalars()
            return [
                (_channel(row), None if row.sealed_key is None else bytes(row.sealed_key))
                for row in rows
            ]

        stored = await self.database.run("read_channels_for_add", read)
        if not isinstance(stored, Succeeded):
            return stored
        for record, _sealed in stored.value:
            if record.name == name:
                raise ChannelExistsError(
                    f"a channel named {name!r} already exists; the stored channel is unchanged"
                )
        for record, stored_sealed in stored.value:
            existing = _derive_key(record, stored_sealed, secret)
            # Constant time: the candidate may be a secret, and so may the stored key.
            if existing is not None and hmac.compare_digest(existing.key, key.key):
                raise ChannelExistsError(
                    f"this key is already stored as the channel {record.name!r}; "
                    "nothing was added"
                )

        created_at = dt.datetime.now(dt.UTC)

        async def work(session: object) -> ChannelRecord:
            row = ChannelRow(
                name=name,
                kind=str(kind),
                hashtag=hashtag,
                sealed_key=sealed,
                channel_hash=key.channel_hash,
                created_at=created_at,
            )
            session.add(row)  # type: ignore[attr-defined]
            await session.flush()  # type: ignore[attr-defined]
            return _channel(row)

        return await self.database.run("add_channel", work)

    async def list_all(self) -> Outcome[list[ChannelRecord]]:
        async def work(session: object) -> list[ChannelRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelRow).order_by(ChannelRow.id)
                )
            ).scalars()
            return [_channel(row) for row in rows]

        return await self.database.run("list_channels", work)

    async def get(self, name: str) -> Outcome[ChannelRecord | None]:
        wanted = name.strip()

        async def work(session: object) -> ChannelRecord | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelRow).where(ChannelRow.name == wanted)
                )
            ).scalar_one_or_none()
            return None if row is None else _channel(row)

        return await self.database.run("get_channel", work)

    async def get_by_id(self, channel_id: int) -> Outcome[ChannelRecord | None]:
        async def work(session: object) -> ChannelRecord | None:
            row = await session.get(ChannelRow, channel_id)  # type: ignore[attr-defined]
            return None if row is None else _channel(row)

        return await self.database.run("get_channel_by_id", work)

    async def message_count(self, channel_id: int) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count())
                    .select_from(ChannelMessageRow)
                    .where(ChannelMessageRow.channel_id == channel_id)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_channel_messages", work)

    async def message_counts(self) -> Outcome[dict[int, int]]:
        async def work(session: object) -> dict[int, int]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelMessageRow.channel_id, func.count()).group_by(
                        ChannelMessageRow.channel_id
                    )
                )
            ).all()
            return {int(channel_id): int(count) for channel_id, count in rows}

        return await self.database.run("count_messages_per_channel", work)

    async def remove(self, channel_id: int) -> Outcome[int | None]:
        """Delete a channel and, by cascade, its history.

        The number of messages deleted with it, or `None` when no such channel.
        """

        async def work(session: object) -> int | None:
            counted = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count())
                    .select_from(ChannelMessageRow)
                    .where(ChannelMessageRow.channel_id == channel_id)
                )
            ).scalar_one()
            result = await session.execute(  # type: ignore[attr-defined]
                delete(ChannelRow).where(ChannelRow.id == channel_id)
            )
            return int(counted) if result.rowcount else None

        return await self.database.run("remove_channel", work)

    async def load_keys(self, secret: bytes | None) -> Outcome[ChannelSet]:
        """Every stored channel with its key, and the names of any that would not open.

        Opening happens outside `run`, for `EntityRepository.load_all`'s reason: a
        wrong secret or an altered row is not a database fault, and one row that
        does not open must not take the other channels with it.
        """

        async def work(session: object) -> list[tuple[ChannelRecord, bytes | None]]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelRow).order_by(ChannelRow.id)
                )
            ).scalars()
            return [
                (_channel(row), None if row.sealed_key is None else bytes(row.sealed_key))
                for row in rows
            ]

        outcome = await self.database.run("load_channels", work)
        if not isinstance(outcome, Succeeded):
            return outcome
        loaded: list[LoadedChannel] = []
        skipped: list[str] = []
        for record, sealed in outcome.value:
            key = _derive_key(record, sealed, secret)
            if key is None:
                skipped.append(record.name)
                continue
            loaded.append(LoadedChannel(record.id, record.name, record.kind, key))
        return Succeeded(ChannelSet(channels=tuple(loaded), skipped=tuple(skipped)))

    async def key_of(self, name: str, secret: bytes | None) -> Outcome[bytes | None]:
        """One channel's key bytes, for `sighop channel key`. `None` for no such channel.

        Raises `SealError` when a pre-shared key does not open, so the caller
        can say why rather than print nothing.
        """

        async def work(session: object) -> tuple[ChannelRecord, bytes | None] | None:
            row = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelRow).where(ChannelRow.name == name.strip())
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _channel(row), None if row.sealed_key is None else bytes(row.sealed_key)

        outcome = await self.database.run("read_channel_key", work)
        if not isinstance(outcome, Succeeded):
            return outcome
        if outcome.value is None:
            return Succeeded(None)
        record, sealed = outcome.value
        if record.kind is ChannelKind.PSK:
            if secret is None or sealed is None:
                raise SealError(
                    f"channel {record.name!r}: its pre-shared key is sealed under "
                    "SIGHOP_SECRET_KEY, which is not set"
                )
            return Succeeded(open_value(sealed, secret, what=f"channel {record.name!r}"))
        key = _derive_key(record, sealed, secret)
        assert key is not None
        return Succeeded(key.key)


def _derive_key(
    record: ChannelRecord, sealed: bytes | None, secret: bytes | None
) -> ChannelKey | None:
    """A stored channel's key, or `None` when a pre-shared key cannot be opened."""
    match record.kind:
        case ChannelKind.PUBLIC:
            return PUBLIC_CHANNEL_KEY
        case ChannelKind.HASHTAG:
            return channel_key_from_hashtag(record.hashtag or record.name)
        case ChannelKind.PSK:
            if secret is None or sealed is None:
                return None
            try:
                return ChannelKey(key=open_value(sealed, secret, what=f"channel {record.name!r}"))
            except (SealError, ValueError):
                return None


def _channel(row: ChannelRow) -> ChannelRecord:
    return ChannelRecord(
        id=int(row.id),
        name=row.name,
        kind=ChannelKind(row.kind),
        channel_hash=int(row.channel_hash),
        created_at=row.created_at,
        hashtag=row.hashtag,
    )


@dataclass(slots=True)
class ChannelMessageRepository:
    """The `channel_message` table: one row per message, updated in place.

    Read directly rather than mirrored, for `DirectMessageRepository`'s reason.
    Nothing on the packet path consults it.
    """

    database: Database

    async def upsert_many(self, records: Sequence[ChannelMessageRecord]) -> Outcome[int]:
        """Write a batch, collapsed to the latest offer per `(channel_id, ref)`.

        `handled_at` is left out of the update: a post must not move in its
        channel because it resolved or was heard repeated.
        """
        if not records:
            return Succeeded(value=0)
        values = _latest_per_key(
            ((record.channel_id, record.ref), _channel_message_values(record))
            for record in records
        )

        async def work(session: object) -> int:
            statement = insert(ChannelMessageRow).values(values)
            await session.execute(  # type: ignore[attr-defined]
                statement.on_conflict_do_update(
                    index_elements=[ChannelMessageRow.channel_id, ChannelMessageRow.ref],
                    set_={
                        "text": statement.excluded.text,
                        "packet_id": statement.excluded.packet_id,
                        "outcome": statement.excluded.outcome,
                        "outcome_reason": statement.excluded.outcome_reason,
                        "repeats_heard": func.greatest(
                            ChannelMessageRow.repeats_heard, statement.excluded.repeats_heard
                        ),
                    },
                )
            )
            return len(values)

        return await self.database.run("upsert_channel_messages", work)

    async def upsert(self, record: ChannelMessageRecord) -> Outcome[int]:
        return await self.upsert_many([record])

    async def recent(
        self, channel_id: int, *, limit: int = DEFAULT_CHANNEL_PAGE
    ) -> Outcome[list[ChannelMessageRecord]]:
        """The newest `limit` messages, newest first, by handled time and never wire time."""

        async def work(session: object) -> list[ChannelMessageRecord]:
            rows = (
                await session.execute(  # type: ignore[attr-defined]
                    select(ChannelMessageRow)
                    .where(ChannelMessageRow.channel_id == channel_id)
                    .order_by(ChannelMessageRow.handled_at.desc(), ChannelMessageRow.id.desc())
                    .limit(limit)
                )
            ).scalars()
            return [_channel_message(row) for row in rows]

        return await self.database.run("read_channel_history", work)

    async def mark_awaiting_unknown(self) -> Outcome[int]:
        """At startup: a post left awaiting by the last run has an unknown outcome."""

        async def work(session: object) -> int:
            result = await session.execute(  # type: ignore[attr-defined]
                update(ChannelMessageRow)
                .where(ChannelMessageRow.outcome == str(ChannelOutcome.AWAITING))
                .values(outcome=str(ChannelOutcome.UNKNOWN))
            )
            return int(result.rowcount or 0)

        return await self.database.run("mark_channel_posts_unknown", work)

    async def count(self) -> Outcome[int]:
        async def work(session: object) -> int:
            total = (
                await session.execute(  # type: ignore[attr-defined]
                    select(func.count()).select_from(ChannelMessageRow)
                )
            ).scalar_one()
            return int(total)

        return await self.database.run("count_all_channel_messages", work)


def _channel_message_values(record: ChannelMessageRecord) -> dict[str, object]:
    return {
        "channel_id": record.channel_id,
        "direction": record.direction,
        "ref": record.ref,
        "entity_public_key": record.entity_public_key,
        "unverified_sender_name": record.unverified_sender_name,
        "text": record.text,
        "wire_timestamp": record.wire_timestamp,
        "handled_at": ensure_utc(record.handled_at, field="channel_message.handled_at"),
        "packet_id": record.packet_id,
        "hop_count": record.hop_count,
        "snr_db": record.snr_db,
        "rssi_dbm": record.rssi_dbm,
        "outcome": str(record.outcome),
        "outcome_reason": record.outcome_reason,
        "repeats_heard": record.repeats_heard,
    }


def _channel_outcome(stored: str) -> ChannelOutcome:
    """A stored outcome, or `UNKNOWN` for one this build does not know."""
    try:
        return ChannelOutcome(stored)
    except ValueError:
        return ChannelOutcome.UNKNOWN


def _channel_message(row: ChannelMessageRow) -> ChannelMessageRecord:
    return ChannelMessageRecord(
        channel_id=int(row.channel_id),
        direction=row.direction,
        ref=row.ref,
        text=bytes(row.text),
        wire_timestamp=int(row.wire_timestamp),
        handled_at=row.handled_at,
        outcome=_channel_outcome(row.outcome),
        entity_public_key=None if row.entity_public_key is None else bytes(row.entity_public_key),
        unverified_sender_name=row.unverified_sender_name,
        packet_id=row.packet_id,
        hop_count=row.hop_count,
        snr_db=row.snr_db,
        rssi_dbm=row.rssi_dbm,
        repeats_heard=int(row.repeats_heard),
        outcome_reason=row.outcome_reason,
        row_id=int(row.id),
    )
