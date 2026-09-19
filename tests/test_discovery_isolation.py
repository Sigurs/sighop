"""A node discovery response never reaches identity state (decode-control-discovery D7).

The response carries no signature, so its key is whatever the sender chose. A
consumer that trusted it could be told any known contact is one hop away at any
signal quality; these tests pin that no identity-keyed state moves when one
arrives claiming a known contact's key.
"""

from __future__ import annotations

from sighop.net.contacts import ContactStore
from sighop.net.paths import PathStore
from sighop.net.rx import Payload, decode_event
from sighop.protocol.identity import generate_identity
from sighop.protocol.packet import PayloadType, RouteType
from sighop.protocol.payloads import DiscoverResponse
from sighop.radio.modem import RxEvent, RxMeta
from tests.botfixtures import advert_record, verified_advert


def _discover_response_claiming(public_key: bytes):
    payload = bytes([0x92, 0x7F]) + b"\x01\x02\x03\x04" + public_key
    raw = bytes([RouteType.DIRECT | (PayloadType.CONTROL << 2), 0x00]) + payload
    record = decode_event(RxEvent(packet=raw, rx_meta=RxMeta(snr_db=12.0, rssi_dbm=-20)))
    assert isinstance(record.outcome, Payload)
    assert isinstance(record.outcome.payload, DiscoverResponse)
    return record


async def test_a_response_claiming_a_known_contact_changes_no_contact() -> None:
    identity = generate_identity()
    store = ContactStore()
    await store.handle(advert_record(verified_advert(identity, name="known"), snr_db=-9.0))
    before = store.get(identity.public_key)
    assert before is not None

    await store.handle(_discover_response_claiming(identity.public_key))

    assert len(store) == 1
    assert store.get(identity.public_key) == before


def test_a_response_claiming_a_known_contact_teaches_no_path() -> None:
    identity = generate_identity()
    paths = PathStore()

    assert paths.observe(_discover_response_claiming(identity.public_key)) is None
    assert paths.keys() == ()
