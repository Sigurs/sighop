"""One table of outstanding acknowledgements, shared by everyone waiting (D11).

`dm.py` used to own a private map from expected checksum to the send waiting on
it, and reported `ack_unmatched` for anything absent from it. A room server waits
on push acknowledgements too, and with two private tables every match of one
looks unmatched to the other — so "unmatched" would stop meaning *nobody in this
process was waiting for that* and start meaning *I personally was not*, which is
the one thing the counter is for.

So the table lives here, expectations carry an **owner**, and one subscriber
matches an inbound acknowledgement against all of them. It is also the single
place the acknowledgement-inside-a-`PATH` case (design D12) has to reach, rather
than two.

Matching is on the first four bytes and nothing else. `parse_ack` has already
split the 4-or-6-byte payload into a checksum and a tail — an extended attempt
byte and a random one, which current firmware appends on one path — and the tail
is never compared, exactly as `BaseChatMesh.cpp:740` does not compare it.

A match is delivery evidence and never sender authentication: the value is an
unkeyed hash over data any observer of the plaintext could reproduce (§5).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sighop.logging import Logger, get_logger
from sighop.net.bus import NetworkBus, Subscription
from sighop.net.rx import Payload, RxRecord
from sighop.protocol.payloads import Acknowledgement


@dataclass(frozen=True, slots=True)
class AckMatch:
    """An acknowledgement that reached the component waiting for it."""

    checksum: bytes
    owner: str
    packet_id: str
    payload_bytes: int
    received_at: dt.datetime | None = None
    """When the reception carrying it arrived, so an owner can report latency
    against its own send time rather than against when it got told."""

    bundled: bool = False
    """True when it arrived inside a decrypted `PATH` body rather than as its
    own packet (design D12). The distinction is worth carrying: a bundled
    acknowledgement means the peer answered a flood with a path return, which is
    the observation §13's unknown #4 is about."""


@dataclass(frozen=True, slots=True)
class AckUnowned:
    """An acknowledgement no component in this process was waiting for."""

    checksum: bytes
    packet_id: str
    outstanding: int
    bundled: bool = False


@dataclass(slots=True)
class _Expectation:
    owner: str
    on_match: Callable[[AckMatch], None]


@dataclass(slots=True)
class AckRegistry:
    """Expected checksums, each with the owner that will be told about a match.

    Registration is deliberately last-writer-wins rather than a list. Two
    components expecting the *same* four bytes means one of them computed an
    acknowledgement another peer will also compute — at 2^-32 by accident, or
    because the same plaintext is being sent to the same key twice, which is a
    retry and belongs to one owner anyway.
    """

    logger: Logger | None = None
    matched: int = field(default=0, init=False)
    unowned: int = field(default=0, init=False)
    _expectations: dict[bytes, _Expectation] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="acks")

    def register(
        self, checksum: bytes, *, owner: str, on_match: Callable[[AckMatch], None]
    ) -> None:
        self._expectations[checksum] = _Expectation(owner=owner, on_match=on_match)

    def release(self, checksum: bytes) -> None:
        """Stop waiting. Releasing one that is not registered is not an error —
        a caller unwinding its own attempts should not have to remember which of
        them it managed to register."""
        self._expectations.pop(checksum, None)

    def release_all(self, checksums: Iterable[bytes]) -> None:
        for checksum in checksums:
            self.release(checksum)

    def owner_of(self, checksum: bytes) -> str | None:
        expectation = self._expectations.get(checksum)
        return None if expectation is None else expectation.owner

    def outstanding(self) -> int:
        return len(self._expectations)

    def owners(self) -> dict[str, int]:
        """How many expectations each owner holds — the status line's version."""
        counts: dict[str, int] = {}
        for expectation in self._expectations.values():
            counts[expectation.owner] = counts.get(expectation.owner, 0) + 1
        return counts

    def deliver(
        self,
        ack: Acknowledgement,
        *,
        packet_id: str,
        received_at: dt.datetime | None = None,
        bundled: bool = False,
    ) -> AckMatch | AckUnowned:
        """Hand an acknowledgement to whoever registered it, or report nobody did.

        The expectation is **not** released here. Its owner knows when its own
        exchange is over — a send unwinds every attempt's expectation at the end,
        a push releases on the cursor advance — and releasing on first match here
        would drop the attempts that are still legitimately outstanding.
        """
        payload_bytes = len(ack.checksum) + len(ack.tail)
        expectation = self._expectations.get(ack.checksum)
        if expectation is None:
            self.unowned += 1
            result = AckUnowned(
                checksum=ack.checksum,
                packet_id=packet_id,
                outstanding=len(self._expectations),
                bundled=bundled,
            )
            assert self.logger is not None
            self.logger.info(
                "ack_unmatched",
                packet_id=packet_id,
                checksum=ack.checksum.hex(),
                outstanding=result.outstanding,
                bundled=bundled,
            )
            return result
        self.matched += 1
        match = AckMatch(
            checksum=ack.checksum,
            owner=expectation.owner,
            packet_id=packet_id,
            payload_bytes=payload_bytes,
            received_at=received_at,
            bundled=bundled,
        )
        expectation.on_match(match)
        return match

    def as_json(self) -> dict[str, object]:
        return {
            "acks_outstanding": len(self._expectations),
            "acks_matched": self.matched,
            "acks_unmatched": self.unowned,
            "ack_owners": self.owners(),
        }


@dataclass(slots=True)
class AckDispatcher:
    """The one bus subscriber that matches acknowledgements (design D11).

    Separate from the registry because the registry is also reached from
    `net/paths.py`, for an acknowledgement bundled inside a decrypted `PATH`
    body, and that one does not arrive as an `Acknowledgement` record of its own.
    """

    registry: AckRegistry
    on_unowned: Callable[[AckUnowned], None] | None = None

    async def handle(self, record: RxRecord) -> None:
        match record.outcome:
            case Payload(payload=Acknowledgement() as ack):
                result = self.registry.deliver(
                    ack, packet_id=record.packet_id, received_at=record.received_at
                )
                if isinstance(result, AckUnowned) and self.on_unowned is not None:
                    self.on_unowned(result)
            case _:
                return

    def subscribe(self, bus: NetworkBus, *, name: str = "acks") -> Subscription:
        return bus.subscribe(name, handler=self.handle)
