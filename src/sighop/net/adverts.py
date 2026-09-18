"""Advert scheduling and the in-memory entity stubs that drive it.

Adverts are the one traffic class entirely under our control, and the only one
whose cost lands on other people's networks: a flood advert is rebroadcast by
every repeater in the mesh, so its true cost is mesh-wide rather than local
(DESIGN.md §4.3). The policy here is therefore conservative on purpose, and
more conservative than the firmware's own defaults:

* a **24 h floor** on the flood interval, against the firmware's 12 h
* **zero-hop adverts off**, matching community practice
* **±25% jitter** per entity, and a **minimum gap between flood adverts from any
  two local entities**, so N entities never burst together
* **startup stagger** across the interval, rather than N adverts at boot
* a faster interval only through an override that **must** carry an expiry
  (default 1 h, maximum 24 h) and reverts by itself (DESIGN.md §13)

Entities come in two kinds and the policy treats them identically. A **stub**
(milestone 3 design D15) has real Ed25519 keys, real signed adverts and real
payload lengths — so the budget sees honest load — but is generated per process
and gone when it exits. A **persistent** entity is one whose keypair was loaded
from a keyfile (`sighop/keystore.py`, milestone 4 design D1), so a peer that
stored its public key stays able to reach it across restarts. The distinction is
reported, never acted on: the floor, jitter, stagger and inter-entity gap are
the same for both.

`request_zero_hop` is the one advert that is not scheduled at all: one packet,
on explicit request, reaching direct neighbours and stopping there. It is how
the first-transmit exercise introduces an identity to a board on the same desk
without giving the wider mesh anything to repeat.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field, replace

from sighop.logging import Logger, get_logger
from sighop.net.bus import PriorityClass, Submission, TxHandle
from sighop.net.tx import Clock, SystemClock
from sighop.protocol.crypto import sign_advert
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import NodeType, build_advert, build_appdata

HOUR = 3600.0
DAY = 24 * HOUR

FLOOD_INTERVAL_FLOOR_SECONDS = DAY
"""DESIGN.md §4.3. MeshCore Switzerland recommends 24 h as an absolute minimum;
repeaters and room servers commonly run 47-49 h."""

DEFAULT_JITTER_FRACTION = 0.25
DEFAULT_MIN_ENTITY_GAP_SECONDS = 600.0
"""Judgement, not measurement: with a 24 h floor and 2-5 entities this is nearly
unbindable. It exists for the scale failure §4.3 designs against."""

DEFAULT_OVERRIDE_SECONDS = HOUR
MAX_OVERRIDE_SECONDS = DAY

DEFAULT_ADVERT_DEADLINE_SECONDS = 300.0
"""An advert that has waited five minutes has missed its slot; the next one is
already scheduled, and a stale advert is worth less than the airtime it costs."""


class AdvertPolicyError(ValueError):
    """A configuration the policy refuses — below the floor, or a bad override."""


@dataclass(frozen=True, slots=True)
class AdvertOverride:
    """A faster interval that expires by itself. There is no permanent one."""

    interval_seconds: float
    expires_at: dt.datetime

    def active(self, now: dt.datetime) -> bool:
        return now < self.expires_at

    def as_json(self) -> dict[str, object]:
        return {
            "override_interval_seconds": self.interval_seconds,
            "override_expires_at": self.expires_at.isoformat(),
        }


@dataclass(slots=True)
class EntityStub:
    """An identity that adverts.

    Ephemeral by default — a keypair generated per process, which is what
    milestone 3 needed. Milestone 4 adds the other kind: `persistent` marks an
    identity loaded from a keyfile, whose public key another node may already
    hold. Every interval, jitter, gap and override rule applies identically to
    both; the flag exists so an operator can tell which identities outlive the
    process, not so the policy can treat them differently.
    """

    entity_id: str
    name: str
    identity: LocalIdentity
    node_type: NodeType = NodeType.CHAT
    persistent: bool = False
    keyfile: str = ""
    """The file this identity was loaded from, for the startup listing."""

    flood_interval_seconds: float = FLOOD_INTERVAL_FLOOR_SECONDS
    zero_hop_interval_seconds: float = 0.0
    """0 means disabled, which is the default and community practice."""

    latitude: int | None = None
    longitude: int | None = None
    override: AdvertOverride | None = None
    next_flood_at: dt.datetime | None = None
    last_flood_at: dt.datetime | None = None
    adverts_sent: int = 0

    @property
    def node_hash(self) -> int:
        return self.identity.node_hash

    def effective_interval(self, now: dt.datetime) -> float:
        """The override's interval while it lives, else the configured one."""
        if self.override is not None and self.override.active(now):
            return self.override.interval_seconds
        return self.flood_interval_seconds

    def as_json(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "entity_name": self.name,
            "entity_type": "entity" if self.persistent else "stub",
            "ephemeral": not self.persistent,
            "keyfile": self.keyfile or None,
            "node_hash": self.node_hash,
            "public_key": self.identity.public_key.hex(),
            "flood_interval_seconds": self.flood_interval_seconds,
            "zero_hop_interval_seconds": self.zero_hop_interval_seconds,
            "adverts_sent": self.adverts_sent,
            "next_flood_at": (
                None if self.next_flood_at is None else self.next_flood_at.isoformat()
            ),
            **({} if self.override is None else self.override.as_json()),
        }


def build_advert_packet(stub: EntityStub, timestamp: int, *, zero_hop: bool = False) -> bytes:
    """A real, signed advert for `stub`, encoded to wire bytes.

    Signed through `protocol/`'s existing path — no new cryptography here, and
    no import in the other direction: `protocol/` stays free of `net/`.
    """
    appdata = build_appdata(
        stub.node_type,
        latitude=stub.latitude,
        longitude=stub.longitude,
        name=stub.name,
    )
    advert = sign_advert(stub.identity, timestamp, appdata)
    return encode_packet(
        Packet(
            header=PacketHeader(
                # A zero-hop advert is deliberately not flooded: it reaches
                # direct neighbours and stops there.
                route_type=RouteType.DIRECT if zero_hop else RouteType.FLOOD,
                payload_type=PayloadType.ADVERT,
                payload_version=PAYLOAD_VERSION_1,
            ),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_advert(advert),
        )
    )


@dataclass(slots=True)
class AdvertScheduler:
    """Decides when each stub adverts, and submits it as ordinary class 3 traffic.

    It never touches the modem: an advert goes through the transmit scheduler
    like everything else, which is what subjects it to the duty-cycle ceiling
    and to being dropped when the budget is spent.
    """

    submit: object
    """A callable taking a `Submission` and returning a `TxHandle` — the bus."""

    clock: Clock = field(default_factory=SystemClock)
    min_entity_gap_seconds: float = DEFAULT_MIN_ENTITY_GAP_SECONDS
    jitter_fraction: float = DEFAULT_JITTER_FRACTION
    deadline_seconds: float = DEFAULT_ADVERT_DEADLINE_SECONDS
    rng: random.Random = field(default_factory=random.Random)
    logger: Logger | None = None
    stubs: list[EntityStub] = field(default_factory=list)
    last_global_flood_at: dt.datetime | None = None
    deferrals: int = 0
    zero_hop_requests: int = 0
    flood_requests: int = 0

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="adverts")

    # --- Stubs -------------------------------------------------------------

    def add_stub(
        self,
        name: str,
        *,
        node_type: NodeType = NodeType.CHAT,
        flood_interval_seconds: float = FLOOD_INTERVAL_FLOOR_SECONDS,
        zero_hop_interval_seconds: float = 0.0,
        entity_id: str | None = None,
    ) -> EntityStub:
        """Generate an identity and register it, staggered into the schedule."""
        if flood_interval_seconds < FLOOD_INTERVAL_FLOOR_SECONDS:
            raise AdvertPolicyError(
                f"flood interval {flood_interval_seconds:.0f}s is below the "
                f"{FLOOD_INTERVAL_FLOOR_SECONDS:.0f}s floor; use an override, "
                "which must carry an expiry"
            )
        identity = generate_identity(avoid_node_hashes=self._taken_hashes())
        return self._register(
            EntityStub(
                entity_id=entity_id or f"stub-{len(self.stubs) + 1}",
                name=name,
                identity=identity,
                node_type=node_type,
                flood_interval_seconds=flood_interval_seconds,
                zero_hop_interval_seconds=zero_hop_interval_seconds,
            )
        )

    def add_identity(
        self,
        name: str,
        identity: LocalIdentity,
        *,
        node_type: NodeType = NodeType.CHAT,
        flood_interval_seconds: float = FLOOD_INTERVAL_FLOOR_SECONDS,
        zero_hop_interval_seconds: float = 0.0,
        entity_id: str | None = None,
        keyfile: str = "",
    ) -> EntityStub:
        """Register an identity loaded from storage as an advert source.

        The same floor, jitter, stagger and inter-entity gap apply as to a
        generated stub — this is the *same* scheduler with a key that outlives
        the process, not a second policy (milestone 4, `advert-policy`).
        """
        if flood_interval_seconds < FLOOD_INTERVAL_FLOOR_SECONDS:
            raise AdvertPolicyError(
                f"flood interval {flood_interval_seconds:.0f}s is below the "
                f"{FLOOD_INTERVAL_FLOOR_SECONDS:.0f}s floor; use an override, "
                "which must carry an expiry"
            )
        if identity.node_hash in self._taken_hashes():
            raise AdvertPolicyError(
                f"an entity with node hash 0x{identity.node_hash:02x} is already "
                "registered; two local entities may not share one (DESIGN.md §3)"
            )
        return self._register(
            EntityStub(
                entity_id=entity_id or name,
                name=name,
                identity=identity,
                node_type=node_type,
                persistent=True,
                keyfile=keyfile,
                flood_interval_seconds=flood_interval_seconds,
                zero_hop_interval_seconds=zero_hop_interval_seconds,
            )
        )

    def _register(self, stub: EntityStub) -> EntityStub:
        self.stubs.append(stub)
        self._stagger(stub)
        return stub

    def _taken_hashes(self) -> set[int]:
        return {stub.node_hash for stub in self.stubs}

    def _stagger(self, stub: EntityStub) -> None:
        """First advert somewhere in the interval, never at startup.

        DESIGN.md §4.3: staggering at startup is what stops N entities firing
        back-to-back the moment the process comes up.
        """
        now = self.clock.now()
        offset = self.rng.uniform(0.0, stub.effective_interval(now))
        stub.next_flood_at = now + dt.timedelta(seconds=offset)

    # --- Overrides ---------------------------------------------------------

    def set_override(
        self,
        stub: EntityStub,
        interval_seconds: float,
        *,
        expires_in: float = DEFAULT_OVERRIDE_SECONDS,
    ) -> AdvertOverride:
        """A faster interval, with a mandatory expiry (DESIGN.md §13)."""
        if interval_seconds <= 0:
            raise AdvertPolicyError("override interval must be positive")
        if expires_in <= 0:
            raise AdvertPolicyError("override expiry must be in the future")
        if expires_in > MAX_OVERRIDE_SECONDS:
            raise AdvertPolicyError(
                f"override expiry of {expires_in:.0f}s exceeds the "
                f"{MAX_OVERRIDE_SECONDS:.0f}s maximum; there is no permanent override"
            )
        now = self.clock.now()
        override = AdvertOverride(
            interval_seconds=interval_seconds,
            expires_at=now + dt.timedelta(seconds=expires_in),
        )
        stub.override = override
        stub.next_flood_at = now + dt.timedelta(seconds=self._jittered(interval_seconds))
        assert self.logger is not None
        self.logger.info(
            "advert_override_set",
            entity_id=stub.entity_id,
            entity_name=stub.name,
            **override.as_json(),
        )
        return override

    def _expire_overrides(self, now: dt.datetime) -> None:
        assert self.logger is not None
        for stub in self.stubs:
            if stub.override is not None and not stub.override.active(now):
                expired, stub.override = stub.override, None
                stub.next_flood_at = now + dt.timedelta(
                    seconds=self._jittered(stub.flood_interval_seconds)
                )
                self.logger.info(
                    "advert_override_expired",
                    entity_id=stub.entity_id,
                    entity_name=stub.name,
                    reverted_to_seconds=stub.flood_interval_seconds,
                    **expired.as_json(),
                )

    def active_overrides(self, now: dt.datetime | None = None) -> list[EntityStub]:
        moment = now or self.clock.now()
        return [
            stub
            for stub in self.stubs
            if stub.override is not None and stub.override.active(moment)
        ]

    # --- Scheduling --------------------------------------------------------

    def _jittered(self, interval_seconds: float) -> float:
        spread = interval_seconds * self.jitter_fraction
        return interval_seconds + self.rng.uniform(-spread, spread)

    def due(self, now: dt.datetime) -> list[EntityStub]:
        return [
            stub
            for stub in self.stubs
            if stub.next_flood_at is not None and stub.next_flood_at <= now
        ]

    def tick(self) -> list[TxHandle]:
        """Submit every advert now due. Returns a handle per submission."""
        assert self.logger is not None
        now = self.clock.now()
        self._expire_overrides(now)
        handles: list[TxHandle] = []

        for stub in self.due(now):
            gap = self.flood_gap_remaining(now)
            if gap > 0:
                # Two local entities must not burst together, so the later one
                # waits rather than being dropped.
                stub.next_flood_at = now + dt.timedelta(seconds=gap)
                self.deferrals += 1
                self.logger.info(
                    "advert_deferred_for_entity_gap",
                    entity_id=stub.entity_id,
                    entity_name=stub.name,
                    deferred_seconds=round(gap, 3),
                )
                continue
            handles.append(self._submit_advert(stub, now))

        return handles

    def request_zero_hop(self, stub: EntityStub) -> TxHandle:
        """Emit one zero-hop advert for `stub`, on explicit request only.

        A zero-hop advert reaches direct neighbours and stops there, so it is
        the cheapest way to introduce ourselves to a board on the same desk
        without giving the wider mesh anything to repeat (design D3). It goes
        through the scheduler as ordinary class 3 traffic and is charged against
        the budget like any other advert.

        It deliberately leaves the entity's flood schedule alone: this is one
        packet on request, not a second recurring cadence, and the zero-hop
        interval stays disabled.
        """
        assert self.logger is not None
        now = self.clock.now()
        packet = build_advert_packet(stub, int(now.timestamp()), zero_hop=True)
        handle: TxHandle = self.submit(  # type: ignore[operator]
            Submission(
                packet=packet,
                priority=PriorityClass.ADVERT,
                entity_id=stub.entity_id,
                entity_name=stub.name,
                entity_type="entity" if stub.persistent else "stub",
                deadline=now + dt.timedelta(seconds=self.deadline_seconds),
                origin="advert_zero_hop",
            )
        )
        self.zero_hop_requests += 1
        self.logger.info(
            "advert_zero_hop_requested",
            entity_id=stub.entity_id,
            entity_name=stub.name,
            size_bytes=len(packet),
            next_flood_at=(None if stub.next_flood_at is None else stub.next_flood_at.isoformat()),
        )
        return handle

    def request_flood(self, stub: EntityStub) -> TxHandle:
        """Emit one flood advert for `stub` now, and move its schedule with it.

        The expensive counterpart to `request_zero_hop`, and it exists for one
        reason: a direct message can only be decrypted by a node that already
        holds the sender's public key, so a peer we heard *through a repeater*
        cannot be introduced to by a zero-hop advert — it is not a direct
        neighbour, and a zero-hop advert stops at direct neighbours.

        **This is repeated by every repeater in the mesh**, on everybody's
        airtime, which is why the flood interval floor is a day. So this
        deliberately advances the entity's own flood schedule: from the mesh's
        point of view this *is* the entity's flood advert, and the next
        scheduled one moves a full interval out rather than arriving on top of
        it. What bounds how often a caller may ask is the caller's own limit —
        for a bot, its rate limit — and nothing here.
        """
        assert self.logger is not None
        now = self.clock.now()
        packet = build_advert_packet(stub, int(now.timestamp()))
        handle: TxHandle = self.submit(  # type: ignore[operator]
            Submission(
                packet=packet,
                priority=PriorityClass.ADVERT,
                entity_id=stub.entity_id,
                entity_name=stub.name,
                entity_type="entity" if stub.persistent else "stub",
                deadline=now + dt.timedelta(seconds=self.deadline_seconds),
                origin="advert_flood_requested",
            )
        )
        stub.adverts_sent += 1
        stub.last_flood_at = now
        self.last_global_flood_at = now
        stub.next_flood_at = now + dt.timedelta(
            seconds=self._jittered(stub.effective_interval(now))
        )
        self.flood_requests += 1
        self.logger.info(
            "advert_flood_requested",
            entity_id=stub.entity_id,
            entity_name=stub.name,
            size_bytes=len(packet),
            next_flood_at=(None if stub.next_flood_at is None else stub.next_flood_at.isoformat()),
            detail="repeated by every repeater in the mesh; the schedule moved with it",
        )
        return handle

    def flood_gap_remaining(self, now: dt.datetime) -> float:
        """Seconds until another flood advert from this run is outside the gap.

        Every flood counts — scheduled, requested by a bot, requested by an
        operator — because the gap exists so local entities never burst
        together, whoever asked. Zero when no flood has been submitted this run.
        A caller that refuses to flood inside the gap reads this; `tick()`
        defers by it.
        """
        return self._gap_remaining(now)

    def _gap_remaining(self, now: dt.datetime) -> float:
        if self.last_global_flood_at is None:
            return 0.0
        elapsed = (now - self.last_global_flood_at).total_seconds()
        return max(0.0, self.min_entity_gap_seconds - elapsed)

    def _submit_advert(self, stub: EntityStub, now: dt.datetime) -> TxHandle:
        packet = build_advert_packet(stub, int(now.timestamp()))
        submission = Submission(
            packet=packet,
            priority=PriorityClass.ADVERT,
            entity_id=stub.entity_id,
            entity_name=stub.name,
            entity_type="stub",
            deadline=now + dt.timedelta(seconds=self.deadline_seconds),
            origin="advert",
        )
        handle = self.submit(submission)  # type: ignore[operator]
        stub.adverts_sent += 1
        stub.last_flood_at = now
        self.last_global_flood_at = now
        stub.next_flood_at = now + dt.timedelta(
            seconds=self._jittered(stub.effective_interval(now))
        )
        return handle  # type: ignore[no-any-return]

    def as_json(self) -> dict[str, object]:
        now = self.clock.now()
        return {
            "stubs": [stub.as_json() for stub in self.stubs],
            "active_overrides": len(self.active_overrides(now)),
            "deferrals": self.deferrals,
            "zero_hop_requests": self.zero_hop_requests,
            "flood_requests": self.flood_requests,
            "min_entity_gap_seconds": self.min_entity_gap_seconds,
        }


def configure_interval(stub: EntityStub, seconds: float) -> EntityStub:
    """Reconfigure a stub's flood interval, refusing anything below the floor."""
    if seconds < FLOOD_INTERVAL_FLOOR_SECONDS:
        raise AdvertPolicyError(
            f"flood interval {seconds:.0f}s is below the {FLOOD_INTERVAL_FLOOR_SECONDS:.0f}s floor"
        )
    return replace(stub, flood_interval_seconds=seconds)
