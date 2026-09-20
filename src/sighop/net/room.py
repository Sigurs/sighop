"""A room server: login, posts, history sync and the request surface (§7).

Shaped like `net/dm.py` and for the same reasons (design D17): policy lives in
`net/`, nothing here imports `monitor/`, nothing runs inside the decode stage,
every submission goes through the scheduler with a deadline and a priority
class, and typed events are emitted for `monitor/render.py` to format.

Every wire detail is taken from `examples/simple_room_server/MyMesh.cpp` and
`src/helpers/ClientACL.h` rather than from the payload documentation, and four
of them are not what the documentation would lead you to build:

* **A failed login is answered with silence** (`:353`). The firmware returns
  without replying when no password matches, and that is load-bearing for us: it
  is what makes "an unauthenticated stranger cannot make sighop transmit" true.
* **An existing member logging in with an *empty* password skips the timestamp
  check entirely** (`:335-342` short-circuits before it). It is how a client
  re-establishes a lost route, and diverging would break interop with every
  stock client. It is also the single most replayable packet in the protocol,
  which is exactly why the throttle below exists (design D9).
* **A wrong password with `allow_read_only` set is admitted as `PERM_ACL_GUEST`,
  which is zero** (`:350`) — the level whose posts are refused (`:479`). What
  the specs call read-only is that byte.
* **A post longer than the store is truncated and still acknowledged, never
  refused** (`:57`, `:484-488`). The receive path has no length check at all:
  `addPost` runs and `send_ack = true` whatever the length, and
  `StrHelper::strncpy(text, postData, MAX_POST_TEXT_LEN)` keeps the leading
  `MAX_POST_TEXT_LEN - 1` bytes (`TxtDataHelpers.cpp:3-9` copies while
  `buf_sz > 1`) — so 150, not the 151 the constant reads as. The acknowledgement
  is computed over the **full received** text (`:461-462`), not over what was
  kept, which is what lets a client stop retrying a post the room shortened.

We keep the truncation rule and not the firmware's number: 150 is a round chat
constant minus a prefix minus an off-by-one, and a stock client can compose 156.
See `STORED_POST_TEXT_LEN`.

Two rules of our own sit on top of the firmware's:

* **A post is acknowledged only once it is stored** (design D6). The firmware
  acknowledges after putting the post in a RAM ring, which cannot fail; ours
  can. An acknowledgement is a promise the client will not retry, so sending it
  before the row lands would make sighop lie about the one property this
  milestone exists to provide.
* **Answering is throttled, counted and reported** (design D8). A successful
  login makes sighop transmit at a stranger's request, so the reply path is
  bounded per source and in total, and every refusal is counted by reason —
  a throttle that drops silently is indistinguishable from a mesh that went
  quiet.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sighop.config import DEFAULT_PATH_HASH_SIZE
from sighop.db.engine import Outcome, Succeeded
from sighop.db.repositories import (
    MemberRecord,
    PostRecord,
    RoomRecord,
)
from sighop.logging import Logger, get_logger
from sighop.net.acks import AckMatch, AckRegistry
from sighop.net.airtime import NoRadioReadback, require_params, time_on_air_ms
from sighop.net.bus import NetworkBus, PriorityClass, Submission, Subscription, TxHandle
from sighop.net.dm import (
    LocalEntity,
    Route,
    ack_timeout_ms,
    build_ack_packet,
)
from sighop.net.pathbodies import adopt_path_body, deliver_bundled_ack
from sighop.net.paths import PathStore
from sighop.net.readback import wait_for_readback
from sighop.net.rx import Payload, RxRecord
from sighop.net.tx import Clock, SystemClock
from sighop.passwords import PasswordHasher, PasswordPolicy, evaluate
from sighop.protocol.crypto import (
    SharedSecretCache,
    ack_checksum,
    ack_checksum_for,
    encrypt_then_mac,
    mac_then_decrypt,
)
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
    ClientKind,
    DirectEnvelope,
    Permission,
    RequestBody,
    RequestType,
    ReturnedPathBody,
    RoomLoginBody,
    RoomLoginResponseBody,
    ServerStats,
    StatusBody,
    TelemetryEntry,
    TextMessageBody,
    TextType,
    WireText,
    build_direct_envelope,
    build_returned_path_body,
    build_room_login_response_body,
    build_status_body,
    build_telemetry_frame,
    build_text_message_body,
    parse_request_body,
    parse_returned_path_body,
    parse_room_login_body,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import RadioParams

MAX_PUSHABLE_TEXT_LEN = 167
"""The hard ceiling, from the packet format alone. A push payload is
`dest_hash(1) + src_hash(1) + MAC(2) + ciphertext` within `MAX_PACKET_PAYLOAD`
(184), so the ciphertext may be 180 bytes rounded down to a whole cipher block —
176. The push plaintext spends 4 bytes on the post timestamp, 1 on flags and 4
on the author's key prefix before the text starts (`:72-85`), leaving 167. Above
this no push can be built at all, whatever anyone would like to store."""

MAX_POST_TEXT_LEN = 151
"""What the firmware's own constant reads as: `MAX_POST_TEXT_LEN (160-9)`
(`MyMesh.h:84`), where the 160 is `MAX_TEXT_LEN = 10 * CIPHER_BLOCK_SIZE`
(`BaseChatMesh.h:8`) — a round ten blocks chosen for *chat* messages, whose own
comment asks only that it stay under 177. It is a convention, not a limit, and
we do not use it. Kept named because the firmware truncates against it."""

STORED_POST_TEXT_LEN = 156
"""What we keep: exactly what a stock client can send and can be shown.

`queueMessage` (`companion_radio/MyMesh.cpp:432`) builds the frame the radio
hands the phone app and bounds it against `MAX_FRAME_SIZE` (176,
`BaseSerialInterface.h:5`). For a signed post to a v3 app the prefix is
`4 (header) + 6 (author pubkey prefix) + 1 (path len) + 1 (text type)
+ 4 (timestamp) + 4 (author prefix) = 20`, leaving **156** — which is why a
stock client's composer stops there, as observed in the milestone 6 live
exercise. Storing this much means nothing a stock client can compose is ever
shortened, and it stays inside `MAX_PUSHABLE_TEXT_LEN` with room to spare.

A longer post is shortened to this and **still acknowledged**: the firmware's
receive path has no length check (`:484-488`), and refusing reads to a client as
a lost packet rather than as a rule — it retries until it gives up, and its user
is told nothing. Above 156 nothing stock composes, so the truncation guards only
the range between here and what a push can carry."""

POST_SYNC_DELAY_SECS = 6
"""`MyMesh.cpp:11`. A new post is not eligible for delivery until it has been
stored this long, so the author's own client is not raced by the push."""

SYNC_PUSH_INTERVAL = 1.2
"""`SYNC_PUSH_INTERVAL` 1200 ms (`:5`). Copied as-is: whether it is right when
several members are far behind at once is not observable until several members
are, and the airtime ceiling bounds the damage meanwhile."""

IDLE_PUSH_INTERVAL = SYNC_PUSH_INTERVAL / 8
"""The firmware polls the next member eight times faster when the current one
had nothing to send (`:1035`), so an idle round-robin does not add a second of
latency per member."""

PUSH_ACK_TIMEOUT_FLOOD_MS = 12_000.0
PUSH_TIMEOUT_BASE_MS = 4_000.0
PUSH_ACK_TIMEOUT_FACTOR_MS = 2_000.0
"""`:7-9`. A pushed post's own acknowledgement window, which is longer than a
direct message's because a push may be the first packet a returning member has
seen in a week."""

MAX_PUSH_FAILURES = 3
"""`:1012`. Consecutive unacknowledged deliveries before a member is left alone
until it is next heard from."""

SERVER_RESPONSE_DELAY_MS = 1_500.0
"""`REPLY_DELAY_MILLIS` (`:3`), used as the deadline margin on a reply."""

DEFAULT_SOURCE_REPLY_LIMIT = 6
DEFAULT_SOURCE_REPLY_WINDOW = 60.0
DEFAULT_GLOBAL_REPLY_LIMIT = 30
DEFAULT_GLOBAL_REPLY_WINDOW = 60.0
"""Design D8's two buckets. Chosen over trusting the duty-cycle ceiling alone:
the ceiling bounds *airtime*, not how much of it one stranger may claim, and it
would let a login flood crowd out the acknowledgements the whole system depends
on."""


class RefusalReason(StrEnum):
    """Why an exchange produced no transmission. Every one of these is counted.

    They are a closed set on purpose: a refusal reported as free text is a
    refusal nobody can count, and design D8 requires that a refusal be silent on
    the air and never silent in the operator's view.
    """

    MAC_FAILED = "mac_failed"
    UNPARSABLE = "unparsable"
    BAD_PASSWORD = "bad_password"
    REPLAY = "replay"
    THROTTLED_SOURCE = "throttled_source"
    THROTTLED_GLOBAL = "throttled_global"
    NOT_A_MEMBER = "not_a_member"
    NO_PERMISSION = "no_permission"
    STORAGE_DEGRADED = "storage_degraded"
    STORAGE_FAILED = "storage_failed"
    NO_ROUTE = "no_route"
    UNSUPPORTED_REQUEST = "unsupported_request"
    UNSUPPORTED_TEXT_TYPE = "unsupported_text_type"
    NO_RADIO_READBACK = "no_radio_readback"


# --- Events the renderer formats (design D17, task 11.4) --------------------


@dataclass(frozen=True, slots=True)
class LoginAdmitted:
    room_name: str
    public_key: bytes
    permission: Permission
    new_member: bool
    flooded: bool
    packet_id: str


@dataclass(frozen=True, slots=True)
class LoginRefused:
    room_name: str
    reason: RefusalReason
    packet_id: str
    detail: str = ""
    public_key: bytes | None = None


@dataclass(frozen=True, slots=True)
class PostStored:
    room_name: str
    author: bytes
    post_timestamp: int
    text: WireText
    retry: bool
    acknowledged: bool
    packet_id: str
    truncated_from: int | None = None
    """Length of the text as received, when it was longer than the store keeps.
    `None` when nothing was dropped."""


@dataclass(frozen=True, slots=True)
class PostRefused:
    room_name: str
    reason: RefusalReason
    packet_id: str
    detail: str = ""
    author: bytes | None = None


@dataclass(frozen=True, slots=True)
class DeliverySent:
    room_name: str
    member: bytes
    post_timestamp: int
    route: Route
    expected_ack: bytes
    transmitted: bool
    packet_id: str


@dataclass(frozen=True, slots=True)
class DeliveryAcknowledged:
    room_name: str
    member: bytes
    post_timestamp: int
    bundled: bool
    packet_id: str


@dataclass(frozen=True, slots=True)
class MemberBackedOff:
    room_name: str
    member: bytes
    failures: int


@dataclass(frozen=True, slots=True)
class RequestAnswered:
    room_name: str
    member: bytes
    request_type: RequestType | int
    reply_bytes: int
    packet_id: str


@dataclass(frozen=True, slots=True)
class RequestRefused:
    room_name: str
    reason: RefusalReason
    request_type: RequestType | int
    packet_id: str
    member: bytes | None = None


@dataclass(frozen=True, slots=True)
class RetentionPruned:
    room_name: str
    deleted: int
    deleted_unsynced: int


type RoomEvent = (
    LoginAdmitted
    | LoginRefused
    | PostStored
    | PostRefused
    | DeliverySent
    | DeliveryAcknowledged
    | MemberBackedOff
    | RequestAnswered
    | RequestRefused
    | RetentionPruned
)


# --- The throttle (design D8) ----------------------------------------------


@dataclass(slots=True)
class ReplyThrottle:
    """Bounds how often a room server answers, per source and in total.

    The Argon2id concurrency semaphore in `passwords.py` is the third bound and
    lives there, because it is a memory guard as much as a rate one; this object
    is deliberately in front of it, so a login storm is refused before it is
    allowed to allocate 64 MiB apiece.
    """

    source_limit: int = DEFAULT_SOURCE_REPLY_LIMIT
    source_window: float = DEFAULT_SOURCE_REPLY_WINDOW
    global_limit: int = DEFAULT_GLOBAL_REPLY_LIMIT
    global_window: float = DEFAULT_GLOBAL_REPLY_WINDOW
    refusals: dict[str, int] = field(default_factory=dict)
    _per_source: dict[int, list[dt.datetime]] = field(default_factory=dict, init=False)
    _global: list[dt.datetime] = field(default_factory=list, init=False)

    def check(self, source_hash: int, now: dt.datetime) -> RefusalReason | None:
        """Whether an answer may be sent, or the reason it may not.

        Nothing is consumed by a refusal: a source that is over its limit does
        not push the global bucket down for everybody else, which is what would
        turn one noisy peer into an outage.
        """
        recent = [
            at
            for at in self._per_source.get(source_hash, ())
            if (now - at).total_seconds() < self.source_window
        ]
        self._per_source[source_hash] = recent
        if len(recent) >= self.source_limit:
            return RefusalReason.THROTTLED_SOURCE
        self._global = [
            at for at in self._global if (now - at).total_seconds() < self.global_window
        ]
        if len(self._global) >= self.global_limit:
            return RefusalReason.THROTTLED_GLOBAL
        return None

    def spend(self, source_hash: int, now: dt.datetime) -> None:
        """Record an answer actually being sent. Only a reply costs a token."""
        self._per_source.setdefault(source_hash, []).append(now)
        self._global.append(now)

    def refuse(self, reason: RefusalReason) -> None:
        self.refusals[str(reason)] = self.refusals.get(str(reason), 0) + 1

    def as_json(self) -> dict[str, object]:
        return {
            "replies_refused": dict(sorted(self.refusals.items())),
            "replies_refused_total": sum(self.refusals.values()),
        }


# --- What the server needs from storage ------------------------------------


class MemberSink(Protocol):
    """The ACL's write side. `RoomMemberRepository` satisfies it."""

    async def upsert(self, member: MemberRecord) -> Outcome[int]: ...

    async def delete(self, room_id: uuid.UUID, public_key: bytes) -> Outcome[bool]: ...


class HistoryStore(Protocol):
    """The history's read and write sides. `MessageRepository` satisfies it."""

    pruned: int
    pruned_unsynced: int

    async def store(
        self,
        *,
        room_id: uuid.UUID,
        author_public_key: bytes,
        text: bytes,
        sender_timestamp: int | None = ...,
        now: int | None = ...,
        posted_at: dt.datetime | None = ...,
    ) -> Outcome[PostRecord]: ...

    async def find_retry(
        self, room_id: uuid.UUID, author_public_key: bytes, sender_timestamp: int
    ) -> Outcome[PostRecord | None]: ...

    async def next_for_member(
        self,
        room_id: uuid.UUID,
        *,
        since: int,
        author_to_skip: bytes,
        not_after: int | None = ...,
    ) -> Outcome[PostRecord | None]: ...

    async def unsynced_count(
        self, room_id: uuid.UUID, *, since: int, author_to_skip: bytes
    ) -> Outcome[int]: ...

    async def prune(
        self,
        room_id: uuid.UUID,
        *,
        retention_days: int | None,
        retention_messages: int | None,
        now: dt.datetime | None = ...,
    ) -> Outcome[tuple[int, int]]: ...


class RoomStorage(Protocol):
    """The slice of `db/persistence.py` a room server uses.

    Protocols rather than the concrete repositories, so the offline exercise can
    run a room server against in-memory fixtures with no database at all — which
    is what keeps "no test added in this milestone requires Postgres" true for
    everything except the tests that are *about* storage. The real
    `Persistence` object satisfies this by having the right attributes, with no
    adapter in between.
    """

    # Read-only properties rather than plain attributes, because a protocol's
    # mutable attribute is invariant: declared as attributes, only something
    # holding *exactly* a `RoomMemberRepository` would satisfy this, which is
    # the opposite of the point.
    @property
    def members(self) -> MemberSink: ...

    @property
    def messages(self) -> HistoryStore: ...

    @property
    def degraded(self) -> bool: ...


# --- The push loop's per-member state ---------------------------------------


@dataclass(slots=True)
class _Delivery:
    """One push in flight to one member."""

    member: bytes
    post_timestamp: int
    expected_ack: bytes
    sent_at: dt.datetime
    deadline: dt.datetime


@dataclass(slots=True)
class _MemberState:
    """Push state that is per member and per process, not per row.

    None of this is persisted, and that is the firmware's shape too
    (`ClientInfo`'s `pending_ack` and `push_failures` are transient). What *is*
    persisted is the cursor, because losing it would resend a week of history or
    skip it.
    """

    delivery: _Delivery | None = None
    failures: int = 0
    backed_off: bool = False


class RoomServer:
    """One room, on one entity. A bus subscriber, exactly like `net/dm.py`."""

    def __init__(
        self,
        *,
        entity: LocalEntity,
        room: RoomRecord,
        storage: RoomStorage,
        paths: PathStore,
        submit: Callable[[Submission], TxHandle],
        acks: AckRegistry,
        members: Iterable[MemberRecord] = (),
        hasher: PasswordHasher | None = None,
        throttle: ReplyThrottle | None = None,
        secrets_cache: SharedSecretCache | None = None,
        clock: Clock | None = None,
        radio: RadioParams | None = None,
        radio_ready: asyncio.Event | None = None,
        telemetry: Sequence[TelemetryEntry] = (),
        runtime_stats: Callable[[], ServerStats] | None = None,
        on_event: Callable[[RoomEvent], None] | None = None,
        logger: Logger | None = None,
        path_hash_size: int = DEFAULT_PATH_HASH_SIZE,
    ) -> None:
        self.entity = entity
        self.room = room
        self.storage = storage
        self.paths = paths
        self.submit = submit
        self.acks = acks
        self.hasher = hasher or PasswordHasher()
        self.throttle = throttle or ReplyThrottle()
        self.secrets = secrets_cache or SharedSecretCache()
        self.clock = clock or SystemClock()
        self.radio = radio
        self.radio_ready = radio_ready
        """Set while the board's parameters are current, cleared by a reconnect
        that loses them. `None` where nobody supplied one — a unit test, or the
        replay path, which has the radio before it starts — and then a reply
        never waits."""
        self.path_hash_size = path_hash_size
        """Width of the floods this room originates; a learned route keeps its own."""
        self.telemetry = list(telemetry)
        self.runtime_stats = runtime_stats
        self._on_event = on_event
        self._log = logger or get_logger(component="room", room=room.name)

        # Design D5: the ACL is read inside the MAC trial, once per packet, so
        # it is answered from memory and written through on change. History is
        # not mirrored here at all — it is unbounded and read by a background
        # task nothing waits on.
        self.members: dict[bytes, MemberRecord] = {member.public_key: member for member in members}
        self._state: dict[bytes, _MemberState] = {key: _MemberState() for key in self.members}
        self._order: list[bytes] = list(self.members)
        self._next_index = 0

        self.logins_admitted = 0
        self.posts_stored = 0
        self.posts_refused = 0
        self.pushes_sent = 0
        self.pushes_acknowledged = 0
        self._push_task: asyncio.Task[None] | None = None

    # --- Identity and reporting --------------------------------------------

    @property
    def name(self) -> str:
        return self.room.name

    @property
    def subscriber_name(self) -> str:
        return f"room:{self.room.name}"

    @property
    def policy(self) -> PasswordPolicy:
        return PasswordPolicy(
            admin_hash=self.room.admin_password_hash,
            guest_hash=self.room.guest_password_hash,
            guest_open=self.room.guest_open,
            allow_read_only=self.room.allow_read_only,
        )

    @property
    def accepting_posts(self) -> bool:
        """A room is exactly as available as its history (design D6)."""
        return not self.storage.degraded

    @property
    def deliveries_outstanding(self) -> int:
        return sum(1 for state in self._state.values() if state.delivery is not None)

    def members_behind(self) -> int:
        return sum(1 for state in self._state.values() if state.backed_off)

    def as_json(self) -> dict[str, object]:
        return {
            "room_name": self.room.name,
            "members": len(self.members),
            "logins_admitted": self.logins_admitted,
            "posts_stored": self.posts_stored,
            "posts_refused": self.posts_refused,
            "pushes_sent": self.pushes_sent,
            "pushes_acknowledged": self.pushes_acknowledged,
            "deliveries_outstanding": self.deliveries_outstanding,
            "members_backed_off": self.members_behind(),
            "accepting_posts": self.accepting_posts,
            "retention": self.room.retention,
            "messages_pruned": self.storage.messages.pruned,
            "messages_pruned_unsynced": self.storage.messages.pruned_unsynced,
            **self.throttle.as_json(),
        }

    def _emit(self, event: RoomEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    def _refuse(self, reason: RefusalReason) -> None:
        self.throttle.refuse(reason)

    # --- The bus subscriber (design D17) -----------------------------------

    def subscribe(self, bus: NetworkBus, *, name: str | None = None) -> Subscription:
        return bus.subscribe(name or self.subscriber_name, handler=self.handle)

    async def handle(self, record: RxRecord) -> None:
        """Everything addressed to this entity's node hash.

        Holds no state in the decode stage and never runs inside `net/rx.py`: a
        replayed capture must keep reproducing every reception exactly (design
        D6), so this is a subscriber and nothing more.
        """
        match record.outcome:
            case Payload(payload=AnonRequestEnvelope() as anon) if (
                anon.dest_hash == self.entity.node_hash
            ):
                await self._handle_login(record, anon)
            case Payload(payload=DirectEnvelope() as envelope) if (
                envelope.dest_hash == self.entity.node_hash
            ):
                await self._handle_direct(record, envelope)
            case _:
                return

    async def _handle_direct(self, record: RxRecord, envelope: DirectEnvelope) -> None:
        match envelope.payload_type:
            case PayloadType.TXT_MSG:
                await self._handle_text(record, envelope)
            case PayloadType.REQ:
                await self._handle_request(record, envelope)
            case PayloadType.PATH:
                self._handle_path(record, envelope)
            case _:
                return

    # --- Login (task 6.2 - 6.7) --------------------------------------------

    async def _handle_login(self, record: RxRecord, envelope: AnonRequestEnvelope) -> None:
        """`onAnonDataRecv` (`MyMesh.cpp:324-411`), with our own guards on top.

        The shared secret comes from the public key the envelope itself carries
        rather than from any stored contact: an `ANON_REQ` is how a sender we
        have never heard of introduces itself, and requiring a contact first
        would make a first login impossible.
        """
        public_key = envelope.sender_public_key
        secret = self.secrets.get(self.entity.identity, public_key)
        candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
        if not candidate.matched or plaintext is None:
            # The key is the sender's own, so a MAC failure here is not a wrong
            # candidate — it is a packet that was not addressed to us after all.
            self._refuse(RefusalReason.MAC_FAILED)
            self._emit(
                LoginRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.MAC_FAILED,
                    packet_id=record.packet_id,
                    public_key=public_key,
                )
            )
            return

        body = parse_room_login_body(plaintext)
        if isinstance(body, DecodeFailure):
            self._refuse(RefusalReason.UNPARSABLE)
            self._emit(
                LoginRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.UNPARSABLE,
                    packet_id=record.packet_id,
                    detail=f"{body.reason}: {body.detail}",
                    public_key=public_key,
                )
            )
            return

        now = self.clock.now()
        existing = self.members.get(public_key)
        empty_password = not body.password.raw

        refusal = self.throttle.check(envelope.sender_hash, now)
        if refusal is not None:
            self._refuse(refusal)
            self._log.info(
                "room_login_throttled",
                packet_id=record.packet_id,
                reason=str(refusal),
                source_hash=envelope.sender_hash,
            )
            self._emit(
                LoginRefused(
                    room_name=self.room.name,
                    reason=refusal,
                    packet_id=record.packet_id,
                    public_key=public_key,
                )
            )
            return

        if empty_password and existing is not None:
            # `MyMesh.cpp:335-342`: an existing member with a blank password is
            # answered without a password check *and without a timestamp check*,
            # because the short-circuit happens before both. This is how a
            # client re-establishes a lost path, and a client that cannot is one
            # that has silently left the room. Matched deliberately (design D9);
            # the throttle above is what keeps it from being free.
            permission = existing.permission
            await self._admit(record, envelope, existing=existing, permission=permission, body=body)
            return

        # Once. Each call is an Argon2id run against 64 MiB, so evaluating twice
        # would double both the cost and what a login storm can claim of it.
        admitted = await evaluate(self.policy, body.password.raw, hasher=self.hasher)
        if admitted is None:
            # No reply at all — not a refusal payload, not an error, nothing.
            self._refuse(RefusalReason.BAD_PASSWORD)
            self._log.info(
                "room_login_refused",
                packet_id=record.packet_id,
                reason=str(RefusalReason.BAD_PASSWORD),
                detail="no password matched and read-only access is not allowed",
            )
            self._emit(
                LoginRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.BAD_PASSWORD,
                    packet_id=record.packet_id,
                    public_key=public_key,
                )
            )
            return

        if existing is not None and body.timestamp <= existing.last_timestamp:
            # `:358-361`, persisted here so a restart does not reopen the window
            # the firmware's transient copy opens on every reboot (design D9).
            self._refuse(RefusalReason.REPLAY)
            self._log.info(
                "room_login_refused",
                packet_id=record.packet_id,
                reason=str(RefusalReason.REPLAY),
                detail=(
                    f"login timestamp {body.timestamp} is not newer than the recorded "
                    f"{existing.last_timestamp}; `sighop room revoke` allows a fresh login"
                ),
            )
            self._emit(
                LoginRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.REPLAY,
                    packet_id=record.packet_id,
                    detail=(
                        f"recorded timestamp {existing.last_timestamp}; revoking the "
                        "member allows a fresh login"
                    ),
                    public_key=public_key,
                )
            )
            return

        await self._admit(record, envelope, existing=existing, permission=admitted, body=body)

    async def _admit(
        self,
        record: RxRecord,
        envelope: AnonRequestEnvelope,
        *,
        existing: MemberRecord | None,
        permission: Permission,
        body: RoomLoginBody,
    ) -> None:
        now = self.clock.now()
        timestamp = body.timestamp
        sync_timestamp = body.sync_timestamp
        public_key = envelope.sender_public_key

        member = MemberRecord(
            room_id=self.room.id,
            public_key=public_key,
            node_hash=public_key[0],
            permissions=int(permission),
            # The member names its own starting position, so a client that reset
            # its history resynchronises from where it says it is (task 8.7).
            sync_since=sync_timestamp
            if sync_timestamp
            else (existing.sync_since if existing else 0),
            last_timestamp=max(timestamp, existing.last_timestamp if existing else 0),
            first_login=existing.first_login if existing else now,
            last_activity=now,
        )
        self._remember(member)
        outcome = await self.storage.members.upsert(member)
        if not isinstance(outcome, Succeeded):
            # The ACL write failed. The member is admitted in memory anyway and
            # the failure is reported: refusing the login would be worse — the
            # client would retry forever — and the write-through is retried on
            # the next login or activity.
            self._log.error(
                "room_member_not_persisted",
                packet_id=record.packet_id,
                member=public_key.hex()[:16],
                error=str(outcome.error),
            )

        self.logins_admitted += 1
        self.throttle.spend(envelope.sender_hash, now)
        flooded = record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)
        self._log.info(
            "room_login_admitted",
            packet_id=record.packet_id,
            member=public_key.hex()[:16],
            permission=permission.name.lower(),
            new_member=existing is None,
            flooded=flooded,
            sync_since=member.sync_since,
        )
        self._emit(
            LoginAdmitted(
                room_name=self.room.name,
                public_key=public_key,
                permission=permission,
                new_member=existing is None,
                flooded=flooded,
                packet_id=record.packet_id,
            )
        )
        await self._send_login_reply(record, member, permission)

    def _remember(self, member: MemberRecord) -> None:
        first = member.public_key not in self.members
        self.members[member.public_key] = member
        if first:
            self._state[member.public_key] = _MemberState()
            self._order.append(member.public_key)
        else:
            # Heard from: a backed-off member resumes (`MyMesh.cpp:544`).
            state = self._state.setdefault(member.public_key, _MemberState())
            state.failures = 0
            state.backed_off = False

    async def _send_login_reply(
        self, record: RxRecord, member: MemberRecord, permission: Permission
    ) -> None:
        """A returned path when the login arrived flooded, a datagram otherwise.

        Design D7: milestone 4's "flooding requires an explicit flag" governs
        *originated* traffic, where the failure mode was an operator mistyping a
        peer name. A reply to a request that arrived flooded has no other way
        home, and the firmware does the same; the safety that gives up is bought
        back by the throttle rather than by refusing to answer.
        """
        response = build_room_login_response_body(
            RoomLoginResponseBody(
                server_timestamp=int(self.clock.now().timestamp()),
                client_kind=ClientKind.for_permissions(int(permission)),
                permissions=int(permission),
                # Four random bytes, whose only job is to keep a retried reply
                # from being deduplicated as the first one (`:391`).
                blob=secrets.token_bytes(4),
            )
        )
        secret = self.secrets.get(self.entity.identity, member.public_key)
        flooded = record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)

        if flooded and record.packet is not None:
            plaintext = build_returned_path_body(
                ReturnedPathBody(
                    hop_count=record.packet.hop_count,
                    hash_size=record.packet.hash_size,
                    path=record.packet.path,
                    extra_type=PayloadType.RESPONSE,
                    extra_raw=response,
                )
            )
            flood = Route(flood=True, hash_size=self.path_hash_size)
            packet = self._encrypted_packet(
                PayloadType.PATH,
                member.node_hash,
                secret,
                plaintext,
                flood,
            )
            await self._submit_reply(record, packet, flood, origin="room_login")
            return

        route = self._route_to(member)
        packet = self._encrypted_packet(
            PayloadType.RESPONSE, member.node_hash, secret, response, route
        )
        await self._submit_reply(record, packet, route, origin="room_login")

    # --- Posts (task 7.1 - 7.7) --------------------------------------------

    async def _handle_text(self, record: RxRecord, envelope: DirectEnvelope) -> None:
        """A `TXT_MSG` from a member is a post (`MyMesh.cpp:441-537`)."""
        found = self._decrypt_from_member(envelope)
        if found is None:
            self._refuse(RefusalReason.NOT_A_MEMBER)
            self._log.info(
                "room_post_refused",
                packet_id=record.packet_id,
                reason=str(RefusalReason.NOT_A_MEMBER),
                src_hash=envelope.src_hash,
                candidates_tried=len(self.members),
            )
            self._emit(
                PostRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.NOT_A_MEMBER,
                    packet_id=record.packet_id,
                    detail=f"no member key opened it ({len(self.members)} tried)",
                )
            )
            return
        member, plaintext = found

        body = parse_text_message_body(plaintext)
        if isinstance(body, DecodeFailure):
            self._post_refused(
                record, RefusalReason.UNPARSABLE, member, f"{body.reason}: {body.detail}"
            )
            return
        if body.txt_type is not TextType.PLAIN:
            # `:445`: CLI_DATA is admin remote administration, which is out of
            # scope for this milestone and deliberately not answered.
            self._post_refused(
                record,
                RefusalReason.UNSUPPORTED_TEXT_TYPE,
                member,
                f"text type {body.txt_type!r} is not handled by a room server",
            )
            return

        self._note_activity(member)
        if body.timestamp < member.last_timestamp:
            self._post_refused(
                record,
                RefusalReason.REPLAY,
                member,
                f"post timestamp {body.timestamp} is below the recorded {member.last_timestamp}",
            )
            return
        retry = body.timestamp == member.last_timestamp

        if not member.permission.may_post:
            # `:479`: refused silently from a `PERM_ACL_GUEST` member — no
            # acknowledgement, so the client's own UI reports the failure.
            self._post_refused(
                record,
                RefusalReason.NO_PERMISSION,
                member,
                f"{member.permission.name.lower()} members may receive history and not post",
            )
            return

        text = body.text.raw
        # `:57` + `:484-488`: over-long posts are shortened, never refused, and
        # the acknowledgement below still covers the text as received. A stock
        # client composes past this limit (156 bytes observed), and silence
        # reads to it as a lost packet rather than as a rule.
        truncated_from = len(text) if len(text) > STORED_POST_TEXT_LEN else None
        text = text[:STORED_POST_TEXT_LEN]
        if not self.accepting_posts:
            self._post_refused(
                record,
                RefusalReason.STORAGE_DEGRADED,
                member,
                "durable storage is unavailable, so nothing can be promised",
            )
            return

        self._record_last_timestamp(member, body.timestamp)
        # Design D6: the insert runs off the bus handler, bounded by the
        # sender's own acknowledgement window, and the acknowledgement is
        # submitted only after the row lands.
        await self._store_then_acknowledge(
            record, member, body, text=text, retry=retry, truncated_from=truncated_from
        )

    def _post_refused(
        self,
        record: RxRecord,
        reason: RefusalReason,
        member: MemberRecord | None,
        detail: str,
    ) -> None:
        self.posts_refused += 1
        self._refuse(reason)
        self._log.info(
            "room_post_refused",
            packet_id=record.packet_id,
            reason=str(reason),
            detail=detail,
            member=None if member is None else member.public_key.hex()[:16],
        )
        self._emit(
            PostRefused(
                room_name=self.room.name,
                reason=reason,
                packet_id=record.packet_id,
                detail=detail,
                author=None if member is None else member.public_key,
            )
        )

    async def _store_then_acknowledge(
        self,
        record: RxRecord,
        member: MemberRecord,
        body: TextMessageBody,
        *,
        text: bytes,
        retry: bool,
        truncated_from: int | None,
    ) -> None:
        window = self._post_ack_window(record)
        try:
            stored = await asyncio.wait_for(
                self._store_post(member, body, text=text, retry=retry), timeout=window
            )
        except TimeoutError:
            # Nothing is sent, the client retries, and its own UI reports the
            # failure honestly (design D6). A promise we cannot keep is worse
            # than a message the sender knows did not land.
            self._post_refused(
                record,
                RefusalReason.STORAGE_FAILED,
                member,
                f"the post did not land within the sender's {window:.1f}s window",
            )
            return
        if stored is None:
            self._post_refused(
                record, RefusalReason.STORAGE_FAILED, member, "the post was not stored"
            )
            return

        self.posts_stored += 1
        acknowledged = await self._acknowledge_post(record, member, body)
        self._log.info(
            "room_post_stored",
            packet_id=record.packet_id,
            member=member.public_key.hex()[:16],
            post_timestamp=stored.post_timestamp,
            text_bytes=len(stored.text),
            retry=retry,
            acknowledged=acknowledged,
            truncated_from=truncated_from,
        )
        self._emit(
            PostStored(
                room_name=self.room.name,
                author=member.public_key,
                post_timestamp=stored.post_timestamp,
                text=stored.rendered,
                retry=retry,
                acknowledged=acknowledged,
                packet_id=record.packet_id,
                truncated_from=truncated_from,
            )
        )

    async def _store_post(
        self, member: MemberRecord, body: TextMessageBody, *, text: bytes, retry: bool
    ) -> PostRecord | None:
        """Store once, however many times it is sent (`:481`).

        A retry is recognised by the sender's own timestamp, which is what makes
        the second copy answerable without a second row: the acknowledgement is
        the same, because it is computed over the same plaintext — the text as
        *received*, which is why `text` here may be shorter than `body.text`.
        """
        existing = await self.storage.messages.find_retry(
            self.room.id, member.public_key, body.timestamp
        )
        if isinstance(existing, Succeeded) and existing.value is not None:
            return existing.value
        outcome = await self.storage.messages.store(
            room_id=self.room.id,
            author_public_key=member.public_key,
            text=text,
            sender_timestamp=body.timestamp,
            now=int(self.clock.now().timestamp()),
            posted_at=self.clock.now(),
        )
        return outcome.value if isinstance(outcome, Succeeded) else None

    def _post_ack_window(self, record: RxRecord) -> float:
        """The sender's own acknowledgement window, in seconds (design D6)."""
        route = self._route_for_record(record)
        try:
            airtime = time_on_air_ms(record.size_bytes, require_params(self.radio))
        except NoRadioReadback:
            airtime = 100.0
        return ack_timeout_ms(airtime, route) / 1000.0

    async def _acknowledge_post(
        self, record: RxRecord, member: MemberRecord, body: TextMessageBody
    ) -> bool:
        """`sha256(plaintext ‖ sender pubkey)[:4]`, at class 0 (`:461-463`).

        Over `body` as it arrived, never over the possibly-shortened stored text
        (`:461-462` hashes the received buffer). A client that composed past
        `STORED_POST_TEXT_LEN` computed its expectation over what it sent, so
        acknowledging the truncation would not match and it would retry forever.
        """
        checksum = ack_checksum_for(body, member.public_key)
        route = self._route_to(member)
        packet = build_ack_packet(checksum=checksum, route=route)
        return await self._submit_reply(
            record, packet, route, origin="room_post_ack", priority=PriorityClass.ACK
        )

    # --- Requests (task 9.1 - 9.5) -----------------------------------------

    async def _handle_request(self, record: RxRecord, envelope: DirectEnvelope) -> None:
        found = self._decrypt_from_member(envelope)
        if found is None:
            self._refuse(RefusalReason.NOT_A_MEMBER)
            self._emit(
                RequestRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.NOT_A_MEMBER,
                    request_type=0,
                    packet_id=record.packet_id,
                )
            )
            return
        member, plaintext = found

        request = parse_request_body(plaintext)
        if isinstance(request, DecodeFailure):
            self._refuse(RefusalReason.UNPARSABLE)
            self._emit(
                RequestRefused(
                    room_name=self.room.name,
                    reason=RefusalReason.UNPARSABLE,
                    request_type=0,
                    packet_id=record.packet_id,
                    member=member.public_key,
                )
            )
            return

        self._note_activity(member)
        if request.timestamp < member.last_timestamp:
            self._request_refused(record, RefusalReason.REPLAY, request, member)
            return
        self._record_last_timestamp(member, request.timestamp)

        match request.request_type:
            case RequestType.KEEP_ALIVE:
                await self._answer_keep_alive(record, member, request, plaintext)
            case RequestType.GET_STATUS:
                await self._answer_status(record, member, request)
            case RequestType.GET_TELEMETRY_DATA:
                await self._answer_telemetry(record, member, request)
            case _:
                # `:216`: an unknown command returns zero and nothing is sent.
                self._request_refused(record, RefusalReason.UNSUPPORTED_REQUEST, request, member)

    def _request_refused(
        self,
        record: RxRecord,
        reason: RefusalReason,
        request: RequestBody,
        member: MemberRecord | None,
    ) -> None:
        self._refuse(reason)
        self._log.info(
            "room_request_refused",
            packet_id=record.packet_id,
            reason=str(reason),
            request_type=str(request.request_type),
            member=None if member is None else member.public_key.hex()[:16],
        )
        self._emit(
            RequestRefused(
                room_name=self.room.name,
                reason=reason,
                request_type=request.request_type,
                packet_id=record.packet_id,
                member=None if member is None else member.public_key,
            )
        )

    async def _answer_keep_alive(
        self,
        record: RxRecord,
        member: MemberRecord,
        request: RequestBody,
        plaintext: bytes,
    ) -> None:
        """`:556-576`. Acknowledged over the request, with the unsynced count.

        The acknowledgement is computed over the first **nine** bytes of the
        request whether or not the sender supplied a position: the firmware
        zero-fills them when it has to (`:562`), so the two ends hash the same
        thing either way.
        """
        forced = request.keep_alive_since
        if forced is not None:
            self._set_cursor(member, forced)

        # RULE: only answered along a known route (`:570`). A keep-alive answer
        # has no meaning without one, and flooding it would put a periodic
        # packet on every repeater in the mesh.
        route = self._known_route_to(member)
        if route is None:
            self._request_refused(record, RefusalReason.NO_ROUTE, request, member)
            return

        hashed = (plaintext + b"\x00" * 9)[:9]
        checksum = ack_checksum(hashed, member.public_key)
        unsynced = await self.storage.messages.unsynced_count(
            self.room.id, since=member.sync_since, author_to_skip=member.public_key
        )
        count = min(unsynced.value, 0xFF) if isinstance(unsynced, Succeeded) else 0
        packet = self._ack_packet_with_tail(checksum, bytes([count]), route)
        sent = await self._submit_reply(
            record, packet, route, origin="room_keep_alive", priority=PriorityClass.ACK
        )
        if sent:
            self._emit(
                RequestAnswered(
                    room_name=self.room.name,
                    member=member.public_key,
                    request_type=RequestType.KEEP_ALIVE,
                    reply_bytes=len(packet),
                    packet_id=record.packet_id,
                )
            )

    async def _answer_status(
        self, record: RxRecord, member: MemberRecord, request: RequestBody
    ) -> None:
        """`:157-180`, with what sighop can honestly fill (design D13).

        The radio counters describe the **whole runtime**, because one modem
        serves every entity in the process and there is no per-entity radio to
        report. `n_posted` and `n_post_push` are this room's.
        """
        stats = self.runtime_stats() if self.runtime_stats is not None else ServerStats()
        body = build_status_body(
            StatusBody(
                tag=request.timestamp,
                stats=_with_room_counters(stats, posted=self.posts_stored, pushed=self.pushes_sent),
            )
        )
        await self._answer_with(record, member, request, body)

    async def _answer_telemetry(
        self, record: RxRecord, member: MemberRecord, request: RequestBody
    ) -> None:
        """`:181-201`. Only what the board actually reported (§4.1).

        The request's second byte is an inverse permission mask for *external
        sensors*. sighop has none, so the mask is parsed, has nothing to gate,
        and a member at the lowest permission level gets the same base frame —
        which is stated rather than left looking like an oversight (design D14).
        """
        body = request.timestamp.to_bytes(4, "little") + build_telemetry_frame(self.telemetry)
        await self._answer_with(record, member, request, body)

    async def _answer_with(
        self,
        record: RxRecord,
        member: MemberRecord,
        request: RequestBody,
        body: bytes,
    ) -> None:
        secret = self.secrets.get(self.entity.identity, member.public_key)
        flooded = record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)
        if flooded and record.packet is not None:
            plaintext = build_returned_path_body(
                ReturnedPathBody(
                    hop_count=record.packet.hop_count,
                    hash_size=record.packet.hash_size,
                    path=record.packet.path,
                    extra_type=PayloadType.RESPONSE,
                    extra_raw=body,
                )
            )
            route = Route(flood=True, hash_size=self.path_hash_size)
            packet = self._encrypted_packet(
                PayloadType.PATH, member.node_hash, secret, plaintext, route
            )
        else:
            route = self._route_to(member)
            packet = self._encrypted_packet(
                PayloadType.RESPONSE, member.node_hash, secret, body, route
            )
        sent = await self._submit_reply(record, packet, route, origin="room_request")
        if sent:
            self._emit(
                RequestAnswered(
                    room_name=self.room.name,
                    member=member.public_key,
                    request_type=request.request_type,
                    reply_bytes=len(body),
                    packet_id=record.packet_id,
                )
            )

    # --- Path returns (task 6.1, design D12) -------------------------------

    def _handle_path(self, record: RxRecord, envelope: DirectEnvelope) -> None:
        """A member's explicit route home, and whatever it bundled.

        This entity's `PATH` packets belong here rather than to
        `net/pathbodies.py` for the same reason its `TXT_MSG` packets do not
        belong to the direct messenger (design D10): one component decrypts a
        given packet. The candidate set is this room's members, which is both
        smaller and exactly right.
        """
        found = self._decrypt_from_member(envelope)
        if found is None:
            self._refuse(RefusalReason.NOT_A_MEMBER)
            return
        member, plaintext = found
        body = parse_returned_path_body(plaintext)
        if isinstance(body, DecodeFailure):
            self._refuse(RefusalReason.UNPARSABLE)
            return
        adopt_path_body(self.paths, body, public_key=member.public_key, record=record)
        self._note_activity(member)
        deliver_bundled_ack(body, record=record, acks=self.acks)

    # --- Decryption over the member set ------------------------------------

    def _decrypt_from_member(self, envelope: DirectEnvelope) -> tuple[MemberRecord, bytes] | None:
        """Trial this room's members, hash-matched first (`searchPeersByHash`).

        The hash narrows the set and never decides it: one byte collides at 1 in
        256 (§3), so a failure against the hash-matching members falls back to
        every member rather than concluding the packet was not ours.
        """
        ordered = [
            member for member in self.members.values() if member.node_hash == envelope.src_hash
        ]
        ordered += [
            member for member in self.members.values() if member.node_hash != envelope.src_hash
        ]
        for member in ordered:
            secret = self.secrets.get(self.entity.identity, member.public_key)
            candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
            if candidate.matched and plaintext is not None:
                return member, plaintext
        return None

    # --- Member state ------------------------------------------------------

    def _note_activity(self, member: MemberRecord) -> None:
        """Heard from: a backed-off member resumes (`MyMesh.cpp:456`, `:544`)."""
        state = self._state.setdefault(member.public_key, _MemberState())
        if state.backed_off:
            self._log.info("room_member_resumed", member=member.public_key.hex()[:16])
        state.failures = 0
        state.backed_off = False
        self._replace(member, last_activity=self.clock.now())

    def _record_last_timestamp(self, member: MemberRecord, timestamp: int) -> None:
        self._replace(member, last_timestamp=max(timestamp, member.last_timestamp))

    def _set_cursor(self, member: MemberRecord, since: int) -> None:
        self._replace(member, sync_since=since)

    def _replace(self, member: MemberRecord, **changes: object) -> MemberRecord:
        from dataclasses import replace

        updated = replace(member, **changes)  # type: ignore[arg-type]
        self.members[updated.public_key] = updated
        return updated

    async def _persist(self, member: MemberRecord) -> None:
        outcome = await self.storage.members.upsert(member)
        if not isinstance(outcome, Succeeded):
            self._log.error(
                "room_member_not_persisted",
                member=member.public_key.hex()[:16],
                error=str(outcome.error),
            )

    async def revoke(self, public_key: bytes) -> bool:
        """Remove membership, permissions, sync position and replay guard.

        All four live in one row, so there is no partial revocation to get
        wrong. Afterwards the member is unknown and is admitted again only by a
        successful login (`room-acl`).
        """
        outcome = await self.storage.members.delete(self.room.id, public_key)
        self.members.pop(public_key, None)
        self._state.pop(public_key, None)
        if public_key in self._order:
            index = self._order.index(public_key)
            self._order.remove(public_key)
            if self._next_index > index:
                self._next_index -= 1
        return isinstance(outcome, Succeeded) and bool(outcome.value)

    # --- The push loop (task 8.1 - 8.6) ------------------------------------

    def start(self) -> None:
        if self._push_task is None:
            self._push_task = asyncio.create_task(
                self.run_push_loop(), name=f"room-push-{self.room.name}"
            )

    async def stop(self) -> None:
        task, self._push_task = self._push_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def run_push_loop(self) -> None:
        while True:
            pushed = await self.push_once()
            await self.clock.sleep(SYNC_PUSH_INTERVAL if pushed else IDLE_PUSH_INTERVAL)

    async def push_once(self) -> bool:
        """One round-robin step (`MyMesh.cpp:995-1039`).

        Round-robin rather than draining one member is the whole point: §7's
        *"a member returning after a week must not monopolize the channel"* is a
        property of taking members in turn, not of the priority class.
        """
        self._expire_deliveries()
        if not self._order:
            return False

        member_key = self._order[self._next_index % len(self._order)]
        self._next_index = (self._next_index + 1) % len(self._order)
        member = self.members.get(member_key)
        state = self._state.get(member_key)
        if member is None or state is None:
            return False
        if state.delivery is not None or state.backed_off:
            return False

        now = self.clock.now()
        eligible = await self.storage.messages.next_for_member(
            self.room.id,
            since=member.sync_since,
            # An author is never sent its own post (`:1023`).
            author_to_skip=member.public_key,
            # Held for `POST_SYNC_DELAY_SECS` before it is eligible at all.
            not_after=int(now.timestamp()) - POST_SYNC_DELAY_SECS,
        )
        if not isinstance(eligible, Succeeded) or eligible.value is None:
            return False
        return await self.deliver(member, eligible.value)

    async def deliver(self, member: MemberRecord, post: PostRecord) -> bool:
        """Push one post to one member and wait for exactly one acknowledgement."""
        body = _push_body(post)
        plaintext = build_text_message_body(body)
        expected = ack_checksum(plaintext, member.public_key)
        route = self._route_to(member)
        secret = self.secrets.get(self.entity.identity, member.public_key)
        packet = self._encrypted_packet(
            PayloadType.TXT_MSG, member.node_hash, secret, plaintext, route
        )

        try:
            # Not used for the timeout — a push's window is the firmware's own
            # formula, not an airtime multiple — but a push composed while the
            # radio has not answered its readback would be submitted with no way
            # to price it, and §4.1's rule is that nothing is inferred. Waited
            # for rather than refused: a push dropped at startup loses the
            # delivery without telling the member anything is missing.
            await wait_for_readback(self.radio_ready)
            require_params(self.radio)
        except NoRadioReadback as error:
            self._refuse(RefusalReason.NO_RADIO_READBACK)
            self._log.error(
                "room_push_not_sent", member=member.public_key.hex()[:16], reason=str(error)
            )
            return False

        now = self.clock.now()
        timeout_ms = _push_timeout_ms(route)
        state = self._state.setdefault(member.public_key, _MemberState())
        state.delivery = _Delivery(
            member=member.public_key,
            post_timestamp=post.post_timestamp,
            expected_ack=expected,
            sent_at=now,
            deadline=now + dt.timedelta(milliseconds=timeout_ms),
        )
        self.acks.register(expected, owner=self.subscriber_name, on_match=self._on_delivery_ack)

        handle = self.submit(
            Submission(
                packet=packet,
                # §7: history sync is priority 1, so it yields to
                # acknowledgements and never to a periodic advert.
                priority=PriorityClass.REPLY,
                entity_id=self.entity.entity_id,
                entity_name=self.entity.name,
                entity_type="room_server",
                deadline=now + dt.timedelta(milliseconds=timeout_ms),
                origin="room_push",
            )
        )
        outcome = await handle
        self.pushes_sent += 1
        self._log.info(
            "room_push_sent",
            member=member.public_key.hex()[:16],
            post_timestamp=post.post_timestamp,
            route=route.label,
            expected_ack=expected.hex(),
            **outcome.as_json(),
        )
        self._emit(
            DeliverySent(
                room_name=self.room.name,
                member=member.public_key,
                post_timestamp=post.post_timestamp,
                route=route,
                expected_ack=expected,
                transmitted=outcome.sent,
                packet_id=outcome.packet_id,
            )
        )
        if not outcome.sent:
            # Suppressed or dropped: the delivery never happened, so nothing is
            # outstanding and no cursor moves. It is retried on the next round.
            self._clear_delivery(member.public_key, advance=False)
        return bool(outcome.sent)

    def _on_delivery_ack(self, match: AckMatch) -> None:
        """A member acknowledged its delivery: advance and persist its cursor.

        Advancing only here is what makes the cursor mean *confirmed delivered*
        rather than *attempted*, which is the difference between a member that
        returns after a week getting what it missed and getting a gap.
        """
        for key, state in self._state.items():
            delivery = state.delivery
            if delivery is None or delivery.expected_ack != match.checksum:
                continue
            member = self.members.get(key)
            if member is None:  # pragma: no cover - revoked mid-flight
                return
            self.pushes_acknowledged += 1
            state.failures = 0
            state.backed_off = False
            updated = self._replace(member, sync_since=delivery.post_timestamp)
            self.acks.release(delivery.expected_ack)
            state.delivery = None
            self._log.info(
                "room_delivery_acknowledged",
                member=key.hex()[:16],
                post_timestamp=delivery.post_timestamp,
                bundled=match.bundled,
            )
            self._emit(
                DeliveryAcknowledged(
                    room_name=self.room.name,
                    member=key,
                    post_timestamp=delivery.post_timestamp,
                    bundled=match.bundled,
                    packet_id=match.packet_id,
                )
            )
            # Persisted after the fact rather than awaited here: this runs from
            # a bus handler, and a cursor that is right in memory and late to
            # the database costs a redelivery, while a blocked handler costs a
            # reception.
            asyncio.create_task(self._persist(updated))  # noqa: RUF006
            return

    def _expire_deliveries(self) -> None:
        """Time out what was never answered, and back a member off at the bound."""
        now = self.clock.now()
        for key, state in self._state.items():
            delivery = state.delivery
            if delivery is None or now < delivery.deadline:
                continue
            self.acks.release(delivery.expected_ack)
            state.delivery = None
            state.failures += 1
            if state.failures >= MAX_PUSH_FAILURES and not state.backed_off:
                state.backed_off = True
                self._log.info(
                    "room_member_backed_off",
                    member=key.hex()[:16],
                    failures=state.failures,
                    detail="delivery resumes when the member is next heard from",
                )
                self._emit(
                    MemberBackedOff(
                        room_name=self.room.name,
                        member=key,
                        failures=state.failures,
                    )
                )

    def _clear_delivery(self, public_key: bytes, *, advance: bool) -> None:
        state = self._state.get(public_key)
        if state is None or state.delivery is None:
            return
        self.acks.release(state.delivery.expected_ack)
        state.delivery = None

    # --- Retention (task 10.1 - 10.2) --------------------------------------

    async def prune_once(self) -> tuple[int, int]:
        outcome = await self.storage.messages.prune(
            self.room.id,
            retention_days=self.room.retention_days,
            retention_messages=self.room.retention_messages,
            now=self.clock.now(),
        )
        if not isinstance(outcome, Succeeded):
            self._log.error("room_retention_failed", error=str(outcome.error))
            return 0, 0
        deleted, unsynced = outcome.value
        if deleted:
            self._log.info(
                "room_retention_pruned",
                deleted=deleted,
                deleted_unsynced=unsynced,
                retention=self.room.retention,
                detail=(
                    "retention wins over sync: messages above a member's cursor "
                    "were removed and that member's history has a gap"
                    if unsynced
                    else ""
                ),
            )
            self._emit(
                RetentionPruned(
                    room_name=self.room.name,
                    deleted=deleted,
                    deleted_unsynced=unsynced,
                )
            )
        return deleted, unsynced

    # --- Packets and routing -----------------------------------------------

    def _route_for_record(self, record: RxRecord) -> Route:
        if record.packet is None:
            return Route(flood=True)
        return Route(
            flood=record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD),
            path=record.packet.path,
            hash_size=record.packet.hash_size,
            hop_count=record.packet.hop_count,
        )

    def _known_route_to(self, member: MemberRecord) -> Route | None:
        """The member's stored route, or None — never a flood.

        A route found by node hash is used when that is all there is and is
        marked ambiguous rather than presented as certain: one byte of identity
        collides at 1 in 256 (§3).
        """
        learned = self.paths.lookup_public_key(member.public_key)
        ambiguous = False
        if learned is None:
            learned = self.paths.lookup_node_hash(member.node_hash)
            ambiguous = learned is not None
        if learned is None:
            return None
        return Route(
            flood=False,
            path=learned.path,
            hash_size=learned.hash_size,
            hop_count=learned.hop_count,
            ambiguous=ambiguous,
        )

    def _route_to(self, member: MemberRecord) -> Route:
        """The member's known route, or a flood when it has none.

        Design D7: this is a *reply*, so a flood needs no operator flag. That
        rule governs originated traffic, where the failure mode was mistyping a
        peer name; a reply to a request that arrived flooded has no other way
        home, and the throttle is what buys the safety back.
        """
        return self._known_route_to(member) or Route(flood=True, hash_size=self.path_hash_size)

    def _encrypted_packet(
        self,
        payload_type: PayloadType,
        dest_hash: int,
        secret: bytes,
        plaintext: bytes,
        route: Route,
    ) -> bytes:
        mac, ciphertext = encrypt_then_mac(secret, plaintext)
        envelope = DirectEnvelope(
            payload_type=payload_type,
            dest_hash=dest_hash,
            src_hash=self.entity.node_hash,
            mac=mac,
            ciphertext=ciphertext,
        )
        return encode_packet(
            Packet(
                header=PacketHeader(
                    route_type=route.route_type,
                    payload_type=payload_type,
                    payload_version=PAYLOAD_VERSION_1,
                ),
                transport_codes=None,
                hop_count=route.hop_count,
                hash_size=route.hash_size,
                path=route.path,
                payload=build_direct_envelope(envelope),
            )
        )

    def _ack_packet_with_tail(self, checksum: bytes, tail: bytes, route: Route) -> bytes:
        """An ACK with the unsynced count appended (`MyMesh.cpp:574`)."""
        return encode_packet(
            Packet(
                header=PacketHeader(
                    route_type=route.route_type,
                    payload_type=PayloadType.ACK,
                    payload_version=PAYLOAD_VERSION_1,
                ),
                transport_codes=None,
                hop_count=route.hop_count,
                hash_size=route.hash_size,
                path=route.path,
                payload=checksum + tail,
            )
        )

    async def _submit_reply(
        self,
        record: RxRecord,
        packet: bytes,
        route: Route,
        *,
        origin: str,
        priority: PriorityClass = PriorityClass.REPLY,
    ) -> bool:
        try:
            # A client whose login or keep-alive falls in the run's first
            # moments is answered, not met with the silence this specification
            # reserves for an unauthorised request.
            await wait_for_readback(self.radio_ready)
            airtime = time_on_air_ms(len(packet), require_params(self.radio))
        except NoRadioReadback as error:
            self._refuse(RefusalReason.NO_RADIO_READBACK)
            self._log.error("room_reply_not_sent", packet_id=record.packet_id, reason=str(error))
            return False
        now = self.clock.now()
        deadline = now + dt.timedelta(
            milliseconds=ack_timeout_ms(airtime, route) + SERVER_RESPONSE_DELAY_MS
        )
        handle = self.submit(
            Submission(
                packet=packet,
                priority=priority,
                entity_id=self.entity.entity_id,
                entity_name=self.entity.name,
                entity_type="room_server",
                deadline=deadline,
                packet_id=record.packet_id,
                origin=origin,
            )
        )
        outcome = await handle
        return bool(outcome.sent)


# --- Helpers ---------------------------------------------------------------


def _push_body(post: PostRecord) -> TextMessageBody:
    """The push body (`MyMesh.cpp:69-108`).

    `attempt` is **random**, not a counter: the firmware draws it precisely so a
    retried push has a different packet hash and therefore a different expected
    acknowledgement, which is what stops a repeater deduplicating the retry away.
    """
    return TextMessageBody(
        timestamp=post.post_timestamp,
        txt_type=TextType.SIGNED_PLAIN,
        attempt=secrets.randbelow(4),
        text=WireText.from_bytes(post.text),
        sender_key_prefix=post.author_public_key[:4],
    )


def _push_timeout_ms(route: Route) -> float:
    """`MyMesh.cpp:96-103`: flooded is a flat window, direct scales with hops."""
    if route.flood:
        return PUSH_ACK_TIMEOUT_FLOOD_MS
    return PUSH_TIMEOUT_BASE_MS + PUSH_ACK_TIMEOUT_FACTOR_MS * (route.hop_count + 1)


def _with_room_counters(stats: ServerStats, *, posted: int, pushed: int) -> ServerStats:
    from dataclasses import replace

    return replace(stats, n_posted=min(posted, 0xFFFF), n_post_push=min(pushed, 0xFFFF))


@dataclass(slots=True)
class RoomRetentionPruner:
    """The per-room pruner, in the shape of `db/packetlog.py`'s (design D15).

    A periodic task, a deletion count, a reported counter — and, unlike the
    packet log's, a second count of deletions that outran a member's cursor,
    because that is the trade retention makes and it must not be discovered
    later.
    """

    rooms: Sequence[RoomServer]
    interval: float = 3600.0
    logger: Logger | None = None
    passes: int = field(default=0, init=False)
    deleted: int = field(default=0, init=False)
    deleted_unsynced: int = field(default=0, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="room-retention")

    async def prune_once(self) -> int:
        self.passes += 1
        total = 0
        for room in self.rooms:
            deleted, unsynced = await room.prune_once()
            total += deleted
            self.deleted += deleted
            self.deleted_unsynced += unsynced
        return total

    async def run(self) -> None:
        await self.prune_once()
        while True:
            await asyncio.sleep(self.interval)
            await self.prune_once()

    def start(self) -> None:
        if self._task is None and self.rooms:
            self._task = asyncio.create_task(self.run(), name="room-retention")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def as_json(self) -> dict[str, object]:
        return {
            "retention_passes": self.passes,
            "retention_deleted": self.deleted,
            "retention_deleted_unsynced": self.deleted_unsynced,
        }
