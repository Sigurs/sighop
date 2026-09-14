"""In-memory storage and stand-ins for a bot, so bot tests need no Postgres.

The same trade `tests/roomfixtures.py` makes and for the same reason: design D16
makes the bot runtime depend on *behaviour* — a key/value store and a degraded
flag — rather than on SQLAlchemy, so every test about dispatch, limits, modes
and the greeter's gates runs with no database at all, and the database-marked
tests are the ones actually about storage.

The semantics here are the repository's rather than a simplification of it. In
particular `set` returns a *failure* when asked to, because "a write that could
not be persisted is reported and is not presented to the driver as having
succeeded" is a requirement, and a fixture that always succeeded would hide
exactly the case design D6 turns on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sighop.db.engine import DatabaseError, Failed, Outcome, Succeeded
from sighop.db.repositories import BotRecord
from sighop.net.contacts import Contact, ContactObservation
from sighop.net.dm import Route, SendOutcome, SendResult
from sighop.net.rx import AdvertOutcome, RxRecord
from sighop.protocol.crypto import VerifiedAdvert, sign_advert, verify_advert
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.payloads import NodeType, build_appdata


@dataclass(slots=True)
class MemoryBotState:
    """`bot_state` in memory, keyed the way the table is."""

    rows: dict[tuple[uuid.UUID, str], object] = field(default_factory=dict)
    fail_writes: bool = False
    """What a degraded database looks like to a caller: the write is refused as
    a value, never as an exception on the reception path (design D8)."""

    fail_reads: bool = False
    writes: int = 0

    async def get(self, bot_id: uuid.UUID, key: str) -> Outcome[object | None]:
        if self.fail_reads:
            return Failed(operation="get_bot_state", error=DatabaseError("unavailable"))
        return Succeeded(value=self.rows.get((bot_id, key)))

    async def set(
        self, bot_id: uuid.UUID, key: str, value: object, *, at: dt.datetime | None = None
    ) -> Outcome[int]:
        if self.fail_writes:
            return Failed(operation="set_bot_state", error=DatabaseError("unavailable"))
        self.writes += 1
        self.rows[(bot_id, key)] = value
        return Succeeded(value=1)

    async def set_many(
        self, bot_id: uuid.UUID, entries: dict[str, object], *, at: dt.datetime | None = None
    ) -> Outcome[int]:
        if self.fail_writes:
            return Failed(operation="seed_bot_state", error=DatabaseError("unavailable"))
        # `DO NOTHING` on conflict, exactly as the repository does: seeding must
        # never overwrite a record that says a greeting was actually sent.
        written = 0
        for key, value in entries.items():
            if (bot_id, key) in self.rows:
                continue
            self.rows[(bot_id, key)] = value
            written += 1
        self.writes += written
        return Succeeded(value=written)

    async def delete(self, bot_id: uuid.UUID, key: str) -> Outcome[bool]:
        return Succeeded(value=self.rows.pop((bot_id, key), None) is not None)

    async def list(self, bot_id: uuid.UUID) -> Outcome[dict[str, object]]:
        if self.fail_reads:
            return Failed(operation="list_bot_state", error=DatabaseError("unavailable"))
        return Succeeded(
            value={key: value for (bid, key), value in self.rows.items() if bid == bot_id}
        )

    async def clear(self, bot_id: uuid.UUID) -> Outcome[int]:
        doomed = [key for key in self.rows if key[0] == bot_id]
        for key in doomed:
            del self.rows[key]
        return Succeeded(value=len(doomed))


@dataclass(slots=True)
class MemoryBotStorage:
    """What a `BotWorker` is handed instead of `Persistence`."""

    bot_state: MemoryBotState = field(default_factory=MemoryBotState)
    degraded: bool = False


def bot_record(
    *,
    driver: str = "greeter",
    config: dict | None = None,
    mode: str = "observe",
    enabled: bool = True,
    entity_name: str = "greeter-bot",
    entity_id: uuid.UUID | None = None,
) -> BotRecord:
    from sighop.bots import drivers as bot_drivers
    from sighop.bots.base import UnknownDriverError

    if config is None:
        try:
            config = bot_drivers.default_config(driver)
        except UnknownDriverError:
            # A test driver that is deliberately not in the registry: the row is
            # still a perfectly good row, and this fixture is not the place to
            # enforce what `sighop bot create` enforces.
            config = {}

    return BotRecord(
        id=uuid.uuid4(),
        entity_id=entity_id or uuid.uuid4(),
        driver=driver,
        enabled=enabled,
        mode=mode,
        config=config,
        created_at=dt.datetime(2026, 9, 6, tzinfo=dt.UTC),
        entity_name=entity_name,
    )


@dataclass(slots=True)
class RecordingSender:
    """Stands in for `DirectMessenger.send`, and counts what reached it.

    A bot in observe mode must produce **zero** submissions, and the only honest
    way to assert that is to count at the seam the runtime would have crossed.
    """

    sent: list[tuple[Contact, str]] = field(default_factory=list)
    result: SendResult = SendResult.ACKNOWLEDGED
    results: list[SendResult] = field(default_factory=list)
    """Outcomes per call, consumed in order; `result` once they run out. Lets a
    test say "the bare greeting went unanswered, the one after the advert did
    not" without a stateful stub of its own."""

    graces: list[float] = field(default_factory=list)
    """The grace window each call asked for, so a test can assert the greeter
    buys extra listening rather than extra transmissions."""

    async def __call__(
        self, contact: Contact, text: str, ack_grace_seconds: float = 0.0
    ) -> SendOutcome:
        self.sent.append((contact, text))
        self.graces.append(ack_grace_seconds)
        result = self.results.pop(0) if self.results else self.result
        return SendOutcome(
            result=result,
            message_id="deadbeef",
            attempts=1,
            route=Route(flood=False, hop_count=0),
        )


def verified_advert(
    identity: LocalIdentity,
    *,
    name: str = "stranger",
    node_type: NodeType | int = NodeType.CHAT,
    timestamp: int = 1_700_000_000,
) -> VerifiedAdvert:
    """A genuinely signed advert.

    Signed rather than manufactured: `ContactStore.observe_advert` takes a
    `VerifiedAdvert` and only a verified signature produces one, so a test that
    faked the type would be testing a shape the reception path cannot produce.
    """
    verification = verify_advert(
        sign_advert(identity, timestamp, build_appdata(node_type, name=name))
    )
    assert isinstance(verification, VerifiedAdvert)
    return verification


def advert_record(
    verified: VerifiedAdvert,
    *,
    hop_count: int = 0,
    snr_db: float | None = 6.0,
    packet_id: str = "packet-1",
    at: dt.datetime | None = None,
    hash_size: int = 1,
    path: bytes | None = None,
) -> RxRecord:
    """The reception that carried an advert, with a real packet behind it.

    A real `Packet` rather than `None`, because `RxRecord.hop_count` reads
    through to one and the hop count is exactly what the greeter's distance gate
    is built on — a fixture that supplied the number some other way would not be
    exercising the gate the runtime uses.

    A given `path` sets the hop count itself, at `hash_size` bytes a hop.
    """
    when = at or dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)
    if path is None:
        path = bytes(range(1, hop_count * hash_size + 1))
    else:
        hop_count = len(path) // hash_size
    packet = Packet(
        header=PacketHeader(
            route_type=RouteType.FLOOD,
            payload_type=PayloadType.ADVERT,
            payload_version=PAYLOAD_VERSION_1,
        ),
        transport_codes=None,
        hop_count=hop_count,
        hash_size=hash_size,
        path=path,
        payload=b"",
    )
    return RxRecord(
        packet_id=packet_id,
        received_at=when,
        raw=b"\x00" * 32,
        snr_db=snr_db,
        rssi_dbm=-80,
        packet=packet,
        outcome=AdvertOutcome(verification=verified),
    )


def observation(contact: Contact, *, created: bool = True) -> ContactObservation:
    return ContactObservation(contact=contact, created=created)
