"""The shared acknowledgement registry (design D11, `direct-messaging`).

The property under test is a *negative* one and it is easy to lose: once two
components wait on acknowledgements, `ack_unmatched` must keep meaning "nobody
in this process was waiting for that" rather than "I personally was not". A
registry that dispatched to the wrong owner, or reported a match as unmatched,
would still pass every test the direct messenger has on its own.
"""

from __future__ import annotations

import datetime as dt

from sighop.net.acks import AckDispatcher, AckMatch, AckRegistry, AckUnowned
from sighop.protocol.payloads import Acknowledgement
from tests.test_dm import ack_record
from tests.test_tx import RecordingLogger

FIRST = b"\x01\x02\x03\x04"
SECOND = b"\x05\x06\x07\x08"


def registry() -> AckRegistry:
    return AckRegistry(logger=RecordingLogger())


# --- 4.2 Two owners, one table ----------------------------------------------


def test_each_match_reaches_its_own_owner_and_neither_is_unmatched() -> None:
    """4.2: the whole point of the shared table."""
    table = registry()
    delivered: list[AckMatch] = []
    table.register(FIRST, owner="direct-messages", on_match=delivered.append)
    table.register(SECOND, owner="room:lounge", on_match=delivered.append)

    assert table.owners() == {"direct-messages": 1, "room:lounge": 1}

    for checksum in (FIRST, SECOND):
        result = table.deliver(Acknowledgement(checksum=checksum), packet_id="pkt")
        assert isinstance(result, AckMatch), f"{checksum.hex()} was reported unmatched"

    assert [match.owner for match in delivered] == ["direct-messages", "room:lounge"]
    assert table.matched == 2
    assert table.unowned == 0


def test_an_acknowledgement_nobody_registered_is_reported_once() -> None:
    table = registry()
    table.register(FIRST, owner="direct-messages", on_match=lambda match: None)

    result = table.deliver(Acknowledgement(checksum=b"\xde\xad\xbe\xef"), packet_id="pkt")

    assert isinstance(result, AckUnowned)
    assert result.outstanding == 1, "the count reports the whole table, not one owner's share"
    assert table.unowned == 1


def test_only_the_first_four_bytes_are_compared() -> None:
    """`BaseChatMesh.cpp:740`: the 2-byte tail is an attempt and a random byte."""
    table = registry()
    matched: list[AckMatch] = []
    table.register(FIRST, owner="room:lounge", on_match=matched.append)

    result = table.deliver(Acknowledgement(checksum=FIRST, tail=b"\x02\x9f"), packet_id="pkt")

    assert isinstance(result, AckMatch)
    assert matched[0].payload_bytes == 6


def test_a_match_does_not_release_its_expectation() -> None:
    """The owner knows when its exchange is over; the registry does not.

    A send has up to four attempts outstanding at once, and releasing on the
    first match would drop the three that are still legitimately registered.
    """
    table = registry()
    table.register(FIRST, owner="direct-messages", on_match=lambda match: None)
    table.deliver(Acknowledgement(checksum=FIRST), packet_id="pkt")
    assert table.owner_of(FIRST) == "direct-messages"

    table.release(FIRST)
    assert table.owner_of(FIRST) is None
    # Releasing what was never registered is not an error: an owner unwinding
    # its attempts should not have to remember which it managed to register.
    table.release(FIRST)


def test_a_bundled_acknowledgement_is_carried_as_bundled() -> None:
    """Design D12: an ACK inside a decrypted PATH body reaches the same table."""
    table = registry()
    matched: list[AckMatch] = []
    table.register(FIRST, owner="room:lounge", on_match=matched.append)

    table.deliver(Acknowledgement(checksum=FIRST), packet_id="pkt", bundled=True)

    assert matched[0].bundled is True


# --- The dispatcher, which is the one bus subscriber ------------------------


async def test_the_dispatcher_delivers_a_reception_to_the_registry() -> None:
    table = registry()
    matched: list[AckMatch] = []
    unowned: list[AckUnowned] = []
    table.register(FIRST, owner="room:lounge", on_match=matched.append)
    dispatcher = AckDispatcher(registry=table, on_unowned=unowned.append)

    record = ack_record(FIRST, at=dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC))
    await dispatcher.handle(record)

    assert matched[0].checksum == FIRST
    assert matched[0].received_at == record.received_at
    assert unowned == []


async def test_the_dispatcher_reports_one_nobody_wanted() -> None:
    table = registry()
    unowned: list[AckUnowned] = []
    dispatcher = AckDispatcher(registry=table, on_unowned=unowned.append)

    await dispatcher.handle(ack_record(b"\xde\xad\xbe\xef"))

    assert unowned[0].checksum == b"\xde\xad\xbe\xef"


async def test_the_dispatcher_ignores_a_reception_that_is_not_an_acknowledgement() -> None:
    """Every subscriber sees every reception; this one acts on one kind."""
    from sighop.protocol.crypto import SharedSecretCache
    from tests.test_dm import Entity, _packet_for, message_packet

    table = registry()
    dispatcher = AckDispatcher(registry=table)
    us, them = Entity("us"), Entity("them")
    secret = SharedSecretCache().get(them.identity, us.identity.public_key)
    packet, _ = message_packet(
        sender=them, recipient_node_hash=us.node_hash, secret=secret, text=b"hi"
    )

    await dispatcher.handle(_packet_for(packet))

    assert (table.matched, table.unowned) == (0, 0)


def test_the_registry_reports_itself_for_the_status_line() -> None:
    table = registry()
    table.register(FIRST, owner="direct-messages", on_match=lambda match: None)
    table.deliver(Acknowledgement(checksum=FIRST), packet_id="pkt")
    table.deliver(Acknowledgement(checksum=SECOND), packet_id="pkt")

    assert table.as_json() == {
        "acks_outstanding": 1,
        "acks_matched": 1,
        "acks_unmatched": 1,
        "ack_owners": {"direct-messages": 1},
    }
