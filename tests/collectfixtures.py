"""In-memory storage and a scripted repeater, so collector tests need no Postgres.

`MemorySettings`, `MemoryTargets` and `MemoryPolls` satisfy the collector's
Protocols the way `roomfixtures.py`'s stores satisfy the room server's.
`FakeRepeater` stands in for the scheduler *and* the far end: every submission
is decrypted and answered the way `simple_repeater/MyMesh.cpp` answers it —
silence for a refused login or a replayed timestamp, a path return bundling the
answer to a flooded request, a direct `RESPONSE` otherwise.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from dataclasses import dataclass, field

from sighop.db.engine import DatabaseError, Failed, Outcome, Succeeded
from sighop.db.repositories import CollectionSettings, PollRecord
from sighop.net.bus import TxOutcome, TxResult
from sighop.net.collect import RepeaterCollector
from sighop.net.contacts import Contact, ContactStore
from sighop.net.pathbodies import PathBodyReader
from sighop.net.paths import PathStore
from sighop.net.rx import Payload, decode_event
from sighop.protocol.crypto import SharedSecretCache, encrypt_then_mac, mac_then_decrypt
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    AnonRequestEnvelope,
    DirectEnvelope,
    NeighbourEntry,
    NeighboursBody,
    NodeType,
    RepeaterStats,
    RepeaterStatusBody,
    RequestBody,
    RequestType,
    ReturnedPathBody,
    WireText,
    build_direct_envelope,
    build_neighbours_body,
    build_repeater_status_body,
    build_returned_path_body,
    parse_request_body,
    parse_returned_path_body,
)
from sighop.radio.modem import EU868_NARROW, RxEvent, RxMeta
from tests.test_dm import START, Entity, TickingClock
from tests.test_tx import RecordingLogger

STATS = RepeaterStats(
    batt_milli_volts=4012,
    curr_tx_queue_len=0,
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
    last_snr=24,
    n_direct_dups=5,
    n_flood_dups=6,
    total_rx_air_time_secs=7200,
    n_recv_errors=3,
)


def _failed(operation: str) -> Failed:
    return Failed(operation=operation, error=DatabaseError("storage is unavailable"))


@dataclass(slots=True)
class MemorySettings:
    current: CollectionSettings = field(default_factory=CollectionSettings)
    fail: bool = False
    reads: int = 0
    cycles: list[dict[str, object]] = field(default_factory=list)

    async def get(self) -> Outcome[CollectionSettings]:
        self.reads += 1
        if self.fail:
            return _failed("get_repeater_collection")
        return Succeeded(self.current)

    async def record_cycle(
        self,
        *,
        started_at: dt.datetime,
        finished_at: dt.datetime | None,
        polled: int | None,
        succeeded: int | None,
        note: str | None = None,
    ) -> Outcome[bool]:
        self.cycles.append(
            {
                "started_at": started_at,
                "finished_at": finished_at,
                "polled": polled,
                "succeeded": succeeded,
                "note": note,
            }
        )
        self.current = dataclasses.replace(
            self.current,
            last_cycle_started_at=started_at,
            last_cycle_finished_at=finished_at,
            last_cycle_polled=polled,
            last_cycle_succeeded=succeeded,
            last_cycle_note=note,
        )
        return Succeeded(True)

    def update(self, **changes: object) -> None:
        self.current = dataclasses.replace(self.current, **changes)  # type: ignore[arg-type]


@dataclass(slots=True)
class MemoryTargets:
    keys: set[bytes] = field(default_factory=set)

    async def list_keys(self) -> Outcome[frozenset[bytes]]:
        return Succeeded(frozenset(self.keys))


@dataclass(slots=True)
class MemoryPolls:
    polls: list[PollRecord] = field(default_factory=list)

    async def record(self, poll: PollRecord) -> Outcome[int]:
        self.polls.append(poll)
        return Succeeded(len(self.polls))

    async def prune_older_than(self, cutoff: dt.datetime) -> Outcome[int]:
        before = len(self.polls)
        self.polls = [poll for poll in self.polls if poll.started_at >= cutoff]
        return Succeeded(before - len(self.polls))


def repeater_contact(
    identity: LocalIdentity,
    *,
    name: str = "hilltop",
    last_heard: dt.datetime | None = START - dt.timedelta(days=1),
    node_type: NodeType = NodeType.REPEATER,
) -> Contact:
    return Contact(
        public_key=identity.public_key,
        name=WireText.from_bytes(name.encode()),
        node_type=node_type,
        last_heard=last_heard,
        advert_verified=True,
    )


@dataclass(slots=True)
class FakeRepeater:
    """A stock repeater, as far as the collector can tell.

    `answers` scripts silence: a step name (`login`, `status`) or
    `neighbours:<offset>` listed in `silent` is never answered.
    """

    identity: LocalIdentity = field(default_factory=generate_identity)
    guest_password_set: bool = False
    stats: RepeaterStats = STATS
    neighbours: list[NeighbourEntry] = field(default_factory=list)
    silent: set[str] = field(default_factory=set)
    submit_results: list[TxResult] = field(default_factory=list)
    """Scheduler outcomes to hand back, in order; transmitted when exhausted."""

    return_hop: bytes = b"\x77"
    """The one-hop route a path return declares."""

    collector: RepeaterCollector | None = None
    path_bodies: PathBodyReader | None = None
    submissions: list = field(default_factory=list)
    requests: list[tuple[str, RouteType | None, int]] = field(default_factory=list)
    """What arrived here: (step, route type, offset for neighbour pages)."""

    delayed: list = field(default_factory=list)
    """Answers withheld by `hold_next`, to be delivered later by the test."""

    hold_next: bool = False
    on_request: object = None
    out_path: bytes | None = None
    """The route to the client this repeater has stored, as `onPeerPathRecv`
    stores it. Unknown means a direct request is answered by flood."""

    path_returns: list[bytes] = field(default_factory=list)
    """The routes the client's path returns stated."""

    answered_by_flood: list[bool] = field(default_factory=list)
    """For each answer sent, whether it went out by flood."""

    submitted_at: list[tuple[str, dt.datetime]] = field(default_factory=list)
    """Every packet handed to the scheduler, by payload type, on the rig's clock."""

    _last_timestamp: int = 0
    _secrets: SharedSecretCache = field(default_factory=SharedSecretCache)

    def __call__(self, submission):  # the scheduler's `submit`
        self.submissions.append(submission)
        assert self.collector is not None
        self.submitted_at.append((submission.origin, self.collector.clock.now()))
        result = self.submit_results.pop(0) if self.submit_results else TxResult.TRANSMITTED
        outcome = TxOutcome(
            result=result,
            packet_id=f"pkt{len(self.submissions)}",
            airtime_ms=10.0,
            queue_wait_ms=0.0,
            attempts=1,
            reason={
                TxResult.DROPPED: "deadline_expired",
                TxResult.SUPPRESSED: "transmit disabled",
            }.get(result, ""),
        )
        if result is TxResult.TRANSMITTED:
            self._receive(submission.packet)

        class _Handle:
            def __await__(_self):
                async def resolve() -> TxOutcome:
                    return outcome

                return resolve().__await__()

        return _Handle()

    @property
    def contact(self) -> Contact:
        return repeater_contact(self.identity)

    def _receive(self, raw: bytes) -> None:
        record = decode_event(
            RxEvent(packet=raw, rx_meta=RxMeta(snr_db=9.0, rssi_dbm=-40), received_at=START),
            received_at=START,
        )
        assert isinstance(record.outcome, Payload)
        flood = record.route_type is RouteType.FLOOD
        envelope = record.outcome.payload
        assert self.collector is not None
        client = next(iter(self.collector.entities))
        secret = self._secrets.get(self.identity, client.identity.public_key)
        if isinstance(envelope, AnonRequestEnvelope):
            matched, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
            assert matched.matched and plaintext is not None
            timestamp = int.from_bytes(plaintext[:4], "little")
            self.requests.append(("login", record.route_type, 0))
            self._hook("login")
            if plaintext[4] != 0 or self.guest_password_set or "login" in self.silent:
                return  # a refused login is silence
            if timestamp <= self._last_timestamp:
                return  # replay guard
            self._last_timestamp = timestamp
            if flood:
                self.out_path = None  # `MyMesh.cpp:131`: rediscover the route

            reply = (1_800_000_000).to_bytes(4, "little") + bytes([0, 0, 0, 0]) + b"blob" + b"\x01"
            self._answer(client, secret, reply, flood)
            return
        assert isinstance(envelope, DirectEnvelope)
        matched, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
        assert matched.matched and plaintext is not None
        if envelope.payload_type is PayloadType.PATH:
            stated = parse_returned_path_body(plaintext)
            assert isinstance(stated, ReturnedPathBody)
            self.out_path = stated.path
            self.path_returns.append(stated.path)
            return
        assert envelope.payload_type is PayloadType.REQ
        request = parse_request_body(plaintext)
        assert isinstance(request, RequestBody)
        if request.timestamp <= self._last_timestamp:
            return
        self._last_timestamp = request.timestamp
        tag = request.timestamp
        if request.request_type == RequestType.GET_STATUS:
            self.requests.append(("status", record.route_type, 0))
            self._hook("status")
            if "status" in self.silent:
                return
            reply = build_repeater_status_body(RepeaterStatusBody(tag=tag, stats=self.stats))
        else:
            arguments = request.arguments
            count, offset = arguments[1], int.from_bytes(arguments[2:4], "little")
            prefix_length = arguments[5]
            self.requests.append(("neighbours", record.route_type, offset))
            self._hook(f"neighbours:{offset}")
            if f"neighbours:{offset}" in self.silent:
                return
            page = self.neighbours[offset : offset + count]
            reply = build_neighbours_body(
                NeighboursBody(
                    tag=tag,
                    total=len(self.neighbours),
                    entries=tuple(
                        dataclasses.replace(entry, prefix=entry.prefix[:prefix_length])
                        for entry in page
                    ),
                )
            )
        self._answer(client, secret, reply, flood)

    def _hook(self, step: str) -> None:
        if callable(self.on_request):
            self.on_request(step)

    def _answer(self, client, secret: bytes, reply: bytes, flood: bool) -> None:
        target: RepeaterCollector | PathBodyReader | None
        self.answered_by_flood.append(flood or self.out_path is None)
        if flood:
            body = ReturnedPathBody(
                hop_count=len(self.return_hop),
                hash_size=1,
                path=self.return_hop,
                extra_type=PayloadType.RESPONSE,
                extra_raw=reply,
            )
            packet = self._datagram(
                client, secret, PayloadType.PATH, build_returned_path_body(body), flood=True
            )
            target = self.path_bodies
        else:
            # A direct request is answered direct only along a stored route;
            # without one the answer is flooded (`MyMesh.cpp:692-696`).
            packet = self._datagram(
                client, secret, PayloadType.RESPONSE, reply, flood=self.out_path is None
            )
            target = self.collector
        assert target is not None
        record = decode_event(
            RxEvent(packet=packet, rx_meta=RxMeta(snr_db=8.0, rssi_dbm=-50), received_at=START),
            received_at=START,
        )
        if self.hold_next:
            self.hold_next = False
            self.delayed.append((target, record))
            return
        asyncio.get_running_loop().create_task(target.handle(record))

    def _datagram(
        self, client, secret: bytes, payload_type: PayloadType, body: bytes, *, flood: bool
    ) -> bytes:
        mac, ciphertext = encrypt_then_mac(secret, body)
        envelope = DirectEnvelope(
            payload_type=payload_type,
            dest_hash=client.node_hash,
            src_hash=self.identity.node_hash,
            mac=mac,
            ciphertext=ciphertext,
        )
        return encode_packet(
            Packet(
                header=PacketHeader(
                    RouteType.FLOOD if flood else RouteType.DIRECT, payload_type, PAYLOAD_VERSION_1
                ),
                transport_codes=None,
                # A flood reaches us having been repeated along the return hop.
                hop_count=len(self.return_hop) if flood else 0,
                hash_size=1,
                path=self.return_hop if flood else b"",
                payload=build_direct_envelope(envelope),
            )
        )


@dataclass(slots=True)
class Rig:
    collector: RepeaterCollector
    repeater: FakeRepeater
    settings: MemorySettings
    targets: MemoryTargets
    polls: MemoryPolls
    contacts: ContactStore
    paths: PathStore
    clock: TickingClock
    entity: Entity
    transmit: list[bool]


def rig(
    *,
    repeater: FakeRepeater | None = None,
    enabled: bool = True,
    selected: bool = True,
    interval_minutes: int = 60,
    contact: Contact | None = None,
) -> Rig:
    """A collector wired to one fake repeater, enabled, selected and in range."""
    repeater = repeater or FakeRepeater()
    entity = Entity("collector")
    entity.entity_id = "00000000-0000-0000-0000-00000000c011"
    contacts = ContactStore(logger=RecordingLogger())
    contacts._insert(contact or repeater.contact)
    paths = PathStore()
    clock = TickingClock()
    import uuid

    settings = MemorySettings(
        current=CollectionSettings(
            enabled=enabled,
            entity_id=uuid.UUID(entity.entity_id),
            interval_minutes=interval_minutes,
        )
    )
    targets = MemoryTargets(keys={repeater.identity.public_key} if selected else set())
    polls = MemoryPolls()
    transmit = [True]
    collector = RepeaterCollector(
        contacts=contacts,
        paths=paths,
        submit=repeater,
        settings=settings,
        targets=targets,
        polls=polls,
        transmit_enabled=lambda: transmit[0],
        entities=[entity],
        clock=clock,
        radio=EU868_NARROW,
        logger=RecordingLogger(),
    )
    path_bodies = PathBodyReader(
        paths=paths,
        contacts=contacts,
        entities=[entity],
        on_bundled_response=collector.on_bundled_response,
        logger=RecordingLogger(),
    )
    repeater.collector = collector
    repeater.path_bodies = path_bodies
    return Rig(
        collector=collector,
        repeater=repeater,
        settings=settings,
        targets=targets,
        polls=polls,
        contacts=contacts,
        paths=paths,
        clock=clock,
        entity=entity,
        transmit=transmit,
    )
