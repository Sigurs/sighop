"""The four tables (design D3), and the reasons three of their details are odd.

DESIGN.md §6 sketches eight tables. This milestone builds the four the runtime
actually reads and writes — `entity`, `contact`, `path`, `packet_log` — so every
table ships with behaviour and a test behind it. `room`, `room_member` and
`message` belong to milestone 6 and `bot_state` to milestone 7, each in its own
migration; §6 calls its list a sketch and "not final DDL", and committing four
tables nothing reads would make it final by accident.

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
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
