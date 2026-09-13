"""The eleven tables (milestone 5 D3, 6 D2, 7 D3, 8 D7, 9 D1).

DESIGN.md §6 sketches eight tables. Milestone 5 built the four the runtime reads
and writes on every packet — `entity`, `contact`, `path`, `packet_log` —
milestone 6 added the three a room server needs — `room`, `room_member`,
`message` — and milestone 7 adds the eighth, `bot_state`, together with a `bot`
table §6 did not sketch. §6's list was called a sketch and "not final DDL"; a
bot has a driver name, a mode and configuration to store, and those are per bot
rather than per key, so they need a row of their own (milestone 7 design D3).

Milestone 8 adds the tenth, `direct_message`. §6's `message` is room-scoped —
it hangs off `room_id` because a room server's whole purpose is to hold what was
posted to it — so a person's own conversation had nowhere to live and a page
refresh lost a conversation the radio actually carried (milestone 8 design D7).

Milestone 9 adds the eleventh, `web_user`: the operator accounts that sign in to
the web interface. It is not platform state about the mesh at all, and it lives
here because revoking an account has to reach a running process (design D1).

Three details are deliberate rather than accidental:

* **`node_hash` is indexed but not unique, on either table.** §3 says a 1-byte
  hash collides at 1 in 256 and the whole design is built on candidate sets. A
  unique constraint there is a bug waiting for a busy mesh.
* **`path_bytes` may legitimately be empty.** An empty path is a zero-hop route
  — the most useful route a node can have — and is distinct from the absence of
  a row, which is the same distinction `PathStore.lookup` already draws.
* **`packet_log.raw` exists** so §4.1's rule, that a malformed frame silently
  dropped is invisible forever, reaches the database as well as the log stream.

Every timestamp is `TIMESTAMPTZ` (design D7). The development server's `TimeZone`
is `Europe/Helsinki`, so a `TIMESTAMP WITHOUT TIME ZONE` column here would record
local wall-clock time and the same code against a UTC server would record
something else — a bug that appears in one deployment and looks like a clock
problem.

Nothing in this module or anywhere else calls `create_all()`. Migrations are the
only schema authority (design D5), and `tests/test_db_schema.py` asserts it.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Named constraints, so a migration's `downgrade()` drops what its `upgrade()`
created rather than whatever Postgres happened to call it."""

TIMESTAMPTZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Entity(Base):
    """A local identity, with its seed sealed under `SIGHOP_SECRET_KEY`.

    `sealed_seed` is ciphertext with an authentication tag and the secret lives
    only in the environment, which is what makes §6's "a DB dump must not be
    sufficient to impersonate a room server" a property of the schema rather
    than a hope.
    """

    __tablename__ = "entity"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    """`room_server` | `companion` | `bot` — sighop's role for this identity,
    not MeshCore's `NodeType`, which travels in `advert_config`."""

    name: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    node_hash: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sealed_seed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    advert_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (Index("ix_entity_node_hash", "node_hash"),)


class Contact(Base):
    """A peer we hold a public key for, keyed by that key because it is the
    identity (§3, and the in-memory store's own rule).

    `advert_verified` travels with the row so a contact an operator pasted in
    cannot become advert-verified by being persisted and reloaded.
    """

    __tablename__ = "contact"

    public_key: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    node_hash: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_type: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    flags: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    advert_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_heard: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    last_heard: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (Index("ix_contact_node_hash", "node_hash"),)


class Path(Base):
    """One candidate route, keyed by what makes a candidate distinct.

    Exactly one of `dest_public_key` and `dest_node_hash` identifies the
    destination; a row with only the hash is the ambiguous kind, and stays
    ambiguous across the round trip because restoring it resolves nothing about
    which node the hash denoted.

    The unique constraint is `NULLS NOT DISTINCT` on purpose. Postgres treats
    NULLs as distinct by default, which would make every hash-keyed route insert
    a new row instead of re-confirming the one already there — the accumulation
    design D12 exists to prevent.
    """

    __tablename__ = "path"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dest_public_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dest_node_hash: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    path_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hash_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    hop_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    snr_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    packet_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "dest_public_key",
            "dest_node_hash",
            "path_bytes",
            "hash_size",
            name="uq_path_destination",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_path_dest_node_hash", "dest_node_hash"),
    )


class PacketLog(Base):
    """The bounded ring buffer §6 describes — a feed, not an audit trail.

    Nothing consults it for dedup or protocol behaviour, and nothing may depend
    on a row being present: rows are written best-effort behind the reception
    path and discarded when the writer falls behind.
    """

    __tablename__ = "packet_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    packet_id: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    """`rx` | `tx`."""

    at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    route_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    path_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    hop_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snr_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    rssi_dbm: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    airtime_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    """TX only: which local identity originated the packet."""

    priority_class: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """TX only: DESIGN.md §4.3's class, so a feed can show what yielded to what."""

    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why an outcome was what it was, where there is more to say than its name —
    the decode failure behind an undecodable frame, the reason behind a drop.
    Beside `raw` it is what carries §4.1's rule into the database."""

    raw: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    """The frame's bytes, kept only for what could not be decoded: a frame we
    silently drop is invisible forever, and this is where it stops being."""

    __table_args__ = (
        Index("ix_packet_log_packet_id", "packet_id"),
        Index("ix_packet_log_at", "at"),
    )


class Room(Base):
    """A room, bound to exactly one local identity (milestone 6 design D2).

    `entity_id` is unique because one identity is one node to the mesh and a
    node is a room — the Non-Goal that keeps `dest_hash` from being ambiguous
    about which room a packet is for.

    The two password columns say three different things between them, and the
    difference is §7's *"an empty guest password is legal… but must be an
    explicit choice, never the default"* expressed as columns an operator has to
    set on purpose:

    * `guest_password_hash` set — that password admits a guest.
    * NULL with `guest_open` false — guest logins are refused. This is what a
      newly created room is.
    * NULL with `guest_open` true — any password admits a guest, including an
      empty one.

    Both retention bounds are nullable and default to NULL, which is
    "keep everything" (design D15). Nothing is ever deleted until an operator
    sets a policy, because §13's unknown #1 — what retention is sensible —
    is answered from observed volume and a default would answer it by accident.
    """

    __tablename__ = "room"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entity.id", ondelete="CASCADE", name="fk_room_entity_id_entity"),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    admin_password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """An encoded `$argon2id$v=19$m=…,t=…,p=…$salt$tag` string (design D1): the
    parameters travel with the hash, so changing them needs no schema change."""

    guest_password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    guest_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class RoomMember(Base):
    """One member of one room — the ACL, and also the routing and cursor table.

    §6's sketch had this as a permission table. The firmware's `ClientInfo`
    (`src/helpers/ClientACL.h`) keeps `sync_since`, `last_timestamp`,
    `permissions` and `out_path` on the same record, and so must we: the sync
    cursor and the replay guard are per member and have nowhere else to live.
    The route itself stays in `path`, which already knows how to hold candidates.

    `node_hash` is indexed and **not** unique, for the reason §3 gives and
    `entity`/`contact` already apply: one byte collides at 1 in 256, and two
    members of one room may perfectly well share one.

    `sync_since` and `last_timestamp` are `BIGINT` because they are MeshCore's
    unsigned 32-bit epoch seconds *as they appear on the wire*, not instants.
    `first_login` and `last_activity` are ours and are `TIMESTAMPTZ`; mixing the
    two representations in one column is how a comparison silently changes
    meaning.
    """

    __tablename__ = "room_member"

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("room.id", ondelete="CASCADE", name="fk_room_member_room_id_room"),
        nullable=False,
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    node_hash: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    permissions: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """MeshCore's permission byte, stored as it travels."""

    sync_since: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    """The ordering value up to which history is confirmed delivered. Advances
    only on an acknowledgement (design D3), so a restart resends nothing the
    member already has and skips nothing it does not."""

    last_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    """The replay guard, persisted so a restart does not reopen the window the
    firmware's transient copy opens on every reboot (design D9)."""

    first_login: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    last_activity: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("room_id", "public_key", name="pk_room_member"),
        Index("ix_room_member_node_hash", "node_hash"),
    )


class Message(Base):
    """One post, ordered within its room by a value the wire protocol can name.

    `post_timestamp` is the cursor, not decoration (design D3). The push carries
    it, the acknowledgement advances `room_member.sync_since` to it, and a
    keep-alive may force a cursor to a specific one — so it has to be exactly
    this value and it has to be a total order. `UNIQUE (room_id, post_timestamp)`
    is what makes `max(now, last + 1)` safe rather than hopeful: a clock that
    steps backwards produces a stall in stamping, never a duplicate.

    `text` is `bytea` because text off the wire is `WireText` and is not
    guaranteed valid UTF-8 (design D4). A room server re-transmits a post to
    every member and must reproduce it byte for byte; rendering to a string
    happens at the edges, marked as a rendering.

    `sender_timestamp` is the author's own claim and is nullable because a post
    the server itself makes has no sender. It is what a retry is recognised by.
    """

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("room.id", ondelete="CASCADE", name="fk_message_room_id_room"),
        nullable=False,
    )
    author_public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    post_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    posted_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        UniqueConstraint("room_id", "post_timestamp", name="uq_message_room_id_post_timestamp"),
        Index("ix_message_room_id_post_timestamp", "room_id", "post_timestamp"),
    )


class Bot(Base):
    """A driver bound to exactly one local identity (milestone 7 design D3).

    The `room` shape, deliberately: `entity_id` is unique because one identity
    is one node to the mesh and a node plays one role, so the loader, the CLI
    and the startup reporting all follow code that already exists.

    Two columns carry the safety posture rather than mere configuration:

    * **`mode`** is `observe` or `active`, and a new row is `observe`. A bot
      that transmits is an explicit operator act (`sighop bot mode`), stored
      rather than passed per run, so a bot cannot become active because an
      operator forgot which flags the last run had (design D4).
    * **`enabled`** is the ordinary off switch, and is checked together with the
      entity's own — a bot on a disabled identity does not run either.

    `config` holds both the driver's configuration and the two limit values the
    runtime owns (`rate_per_hour`, `burst`). They share one column because they
    are configured, listed and validated together; the runtime's two keys are
    reserved and a driver's validator may not claim them.
    """

    __tablename__ = "bot"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entity.id", ondelete="CASCADE", name="fk_bot_entity_id_entity"),
        nullable=False,
        unique=True,
    )
    driver: Mapped[str] = mapped_column(Text, nullable=False)
    """The registry name of an in-tree driver class (design D14). Never an
    import path: loading foreign code into the process holding the entity seeds
    needs a better reason than convenience."""

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="observe")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class BotState(Base):
    """§6's eighth table: the durable key/value store a driver may persist in.

    The only place a driver may write anything, and the reason bots require a
    database at all (design D5): the greeter's "greeted at most once for the
    lifetime of that contact" is a row here, and a run that could not write one
    would re-greet the neighbourhood after every restart.

    `(bot_id, key)` is the primary key, so two bots may hold the same key with
    different values — state is per bot, and the isolation is the schema's
    rather than a convention the runtime remembers to apply.
    """

    __tablename__ = "bot_state"

    bot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bot.id", ondelete="CASCADE", name="fk_bot_state_bot_id_bot"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("bot_id", "key", name="pk_bot_state"),)


class DirectMessage(Base):
    """§6's tenth table: one direct message, in either direction (design D7).

    Keyed by the pair that makes a conversation — the local identity's public key
    and the peer's — by *key* rather than by `entity.id`, deliberately. A
    conversation outlives the row that held the identity that carried it: an
    entity deleted from `entity` takes its foreign keys with it, and the messages
    it exchanged are the one thing that should survive being able to explain
    themselves. The key is also what both directions actually have in hand at the
    moment the record is made.

    `ref` is what makes "written at submission, updated when it resolves" one row
    rather than two: the send's `message_id` outbound, the reception's `packet_id`
    inbound, unique per identity. Every write is `ON CONFLICT (entity_public_key,
    ref) DO UPDATE`, so a message in flight is visible and a resolved one does not
    appear twice.

    Three columns are shaped by rules established earlier:

    * **`wire_timestamp` is `BIGINT`**, per §6's rule for the cursor columns
      `room_member` already carries: it is MeshCore's unsigned 32-bit epoch
      seconds *as they appear on the wire*, the peer's own claim about its clock,
      not an instant. `handled_at` is ours and is `TIMESTAMPTZ`. The ordering is
      by `handled_at` and never by `wire_timestamp` — a peer with a wrong clock
      must not be able to reorder a conversation.
    * **`text` is `bytea`**, for `message.text`'s reason (milestone 6 design D4):
      text off the wire is `WireText` and is not guaranteed valid UTF-8. It is
      stored as it arrived and rendered at the edges, marked as a rendering.
      **It is not encrypted at rest**: a database dump exposes conversation
      content. §6 was careful that a dump must not be sufficient to *impersonate*
      a room server, which `entity.sealed_seed` delivers; it makes no such
      promise about content, and this table is where that becomes concrete.
    * **`outcome` is never NULL.** A record written at submission says
      `in_flight`, which is what a restart leaves behind — an outcome that is
      unknown rather than a claim of delivery or a row that vanished.
    """

    __tablename__ = "direct_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    peer_public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    """`out` | `in`, as `packet_log.direction` is `tx` | `rx`."""

    text: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wire_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    handled_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    """The send's `message_id` outbound, the reception's `packet_id` inbound."""

    packet_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    """Every packet this message put on the air, in attempt order — the join to
    `packet_log` and to the feed. One entry inbound."""

    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    route_flood: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    route_path: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    """The route the send used. NULL inbound, and empty rather than NULL for a
    zero-hop direct route — the distinction `path.path_bytes` already draws."""

    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    ack_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "entity_public_key", "ref", name="uq_direct_message_entity_public_key_ref"
        ),
        Index(
            "ix_direct_message_entity_public_key_peer_public_key_handled_at",
            "entity_public_key",
            "peer_public_key",
            "handled_at",
        ),
    )


class WebUser(Base):
    """An operator account that signs in to the web interface (milestone 9 D1).

    Not one of §6's tables: §6 predates the panel. Accounts are durable state
    because revoking one has to reach a running process without re-reading a
    file, and `sighop web user disable` in another process reaches it through
    the revalidation every session does against this row.

    * **`username` is stored normalised** — NFKC, then `casefold()` — by the one
      function every surface uses (`normalise_username`), so `UNIQUE` here is a
      case-insensitive uniqueness without the `citext` extension a measured role
      may not be able to create.
    * **`password_hash` is the encoded `$argon2id$…` string**, parameters and
      salt included, as `room.admin_password_hash` is. Nothing renders it.
    * **`password_set_at` doubles as the credential epoch.** A session records
      the value it was issued under and ends when the row's differs, so there is
      no separate generation counter to drift from the thing it describes.
    """

    __tablename__ = "web_user"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    password_set_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (UniqueConstraint("username", name="uq_web_user_username"),)
