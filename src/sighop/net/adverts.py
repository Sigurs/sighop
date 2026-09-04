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

The entities here are **stubs** (design D15): real Ed25519 keys, real signed
adverts, real payload lengths — so the budget sees honest load — but generated
per process and gone when it exits. Milestone 5 introduces persisted entities;
a stub that quietly wrote its key somewhere would be that milestone's design
decision made by accident.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Container
from dataclasses import dataclass, field, replace

import structlog

from sighop.logging import get_logger
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
    """An in-memory identity that adverts. Ephemeral by construction."""

    entity_id: str
    name: str
    identity: LocalIdentity
    node_type: NodeType = NodeType.CHAT
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
            "entity_type": "stub",
            "ephemeral": True,
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
    logger: structlog.stdlib.BoundLogger | None = None
    stubs: list[EntityStub] = field(default_factory=list)
    last_global_flood_at: dt.datetime | None = None
    deferrals: int = 0

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
        stub = EntityStub(
            entity_id=entity_id or f"stub-{len(self.stubs) + 1}",
            name=name,
            identity=identity,
            node_type=node_type,
            flood_interval_seconds=flood_interval_seconds,
            zero_hop_interval_seconds=zero_hop_interval_seconds,
        )
        self.stubs.append(stub)
        self._stagger(stub)
        return stub

    def _taken_hashes(self) -> Container[int]:
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
            gap = self._gap_remaining(now)
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
            "min_entity_gap_seconds": self.min_entity_gap_seconds,
        }


def configure_interval(stub: EntityStub, seconds: float) -> EntityStub:
    """Reconfigure a stub's flood interval, refusing anything below the floor."""
    if seconds < FLOOD_INTERVAL_FLOOR_SECONDS:
        raise AdvertPolicyError(
            f"flood interval {seconds:.0f}s is below the "
            f"{FLOOD_INTERVAL_FLOOR_SECONDS:.0f}s floor"
        )
    return replace(stub, flood_interval_seconds=seconds)
