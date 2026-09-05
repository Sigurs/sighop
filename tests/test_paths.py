"""Shared reverse-path learning (milestone 3, `path-learning`, design D13).

Driven from the corpus: 997 real receptions with real paths, including the 510
frames whose hops are 2 or 3 bytes wide. Those are what make `reverse_path`
worth testing at all — reversing the byte string instead of the hop sequence
would corrupt over half the corpus and look right on the rest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import cast

import pytest

from sighop.net.paths import (
    LearnedPath,
    PathKey,
    PathStore,
    reverse_path,
    sender_key,
)
from sighop.net.rx import AdvertOutcome, Payload, RxRecord, decode_event
from sighop.protocol.packet import RouteType
from sighop.protocol.payloads import AnonRequestEnvelope, DirectEnvelope
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
    return records


@pytest.fixture(scope="module")
def flood_advert(corpus_records: list[RxRecord]) -> RxRecord:
    """A verified advert that reached us over at least one hop."""
    for record in corpus_records:
        if (
            isinstance(record.outcome, AdvertOutcome)
            and record.outcome.verified is not None
            and record.packet is not None
            and record.packet.hop_count > 0
            and record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)
        ):
            return record
    raise AssertionError("the corpus holds no multi-hop verified flood advert")


# --- Reversing -------------------------------------------------------------


def test_a_single_byte_path_reverses_hop_by_hop() -> None:
    assert reverse_path(b"\x01\x02\x03", hash_size=1) == b"\x03\x02\x01"


def test_a_multi_byte_path_reverses_hops_not_bytes() -> None:
    """Two-byte hops AB, CD reverse to CD, AB — never DC, BA."""
    assert reverse_path(b"\xaa\xbb\xcc\xdd", hash_size=2) == b"\xcc\xdd\xaa\xbb"
    assert reverse_path(b"\x01\x02\x03\x04\x05\x06", hash_size=3) == b"\x04\x05\x06\x01\x02\x03"


def test_an_empty_path_reverses_to_an_empty_path() -> None:
    assert reverse_path(b"", hash_size=2) == b""


def test_a_path_that_is_not_whole_hops_is_rejected() -> None:
    with pytest.raises(ValueError, match="whole hops"):
        reverse_path(b"\x01\x02\x03", hash_size=2)


def test_every_corpus_path_reverses_and_round_trips(corpus_records) -> None:
    checked = 0
    for record in corpus_records:
        if record.packet is None or not record.packet.path:
            continue
        hash_size = record.packet.hash_size
        reversed_once = reverse_path(record.packet.path, hash_size)
        assert len(reversed_once) == len(record.packet.path)
        assert reverse_path(reversed_once, hash_size) == record.packet.path
        checked += 1
    assert checked > 100, f"only {checked} corpus frames carried a path"


# --- Learning --------------------------------------------------------------


def test_a_flood_advert_teaches_a_reverse_path(flood_advert) -> None:
    store = PathStore()

    result = store.observe(flood_advert)

    assert result is not None
    key, learned = result
    assert key.public_key is not None
    assert not key.ambiguous
    assert flood_advert.packet is not None
    assert learned.path == reverse_path(flood_advert.packet.path, flood_advert.packet.hash_size)
    assert learned.hop_count == flood_advert.packet.hop_count
    assert learned.snr_db == flood_advert.snr_db
    assert store.lookup(key) == learned


def test_a_direct_reception_carrying_a_path_teaches_nothing(flood_advert) -> None:
    """That path is a route someone else chose; it is not a route back."""
    assert flood_advert.packet is not None
    assert flood_advert.packet.hop_count > 0
    direct = replace(
        flood_advert,
        packet=replace(
            flood_advert.packet,
            header=replace(flood_advert.packet.header, route_type=RouteType.DIRECT),
        ),
    )

    store = PathStore()
    assert store.observe(direct) is None
    assert store.destination_count == 0


def test_a_direct_reception_with_no_path_is_a_zero_hop_neighbour(corpus_records) -> None:
    """MeshCore's zero-hop adverts arrive this way, and they are real evidence.

    Learning only from flood packets discarded them: replaying
    `captures/2026-09-04-03.jsonl` learned two destinations where most of the
    file's adverts are DIRECT with `h0`.
    """
    zero_hop_direct = next(
        record
        for record in corpus_records
        if record.route_type is RouteType.DIRECT
        and record.packet is not None
        and record.packet.hop_count == 0
        and not record.packet.path
        and sender_key(record) is not None
    )

    store = PathStore()
    result = store.observe(zero_hop_direct)

    assert result is not None
    _key, learned = result
    assert learned.is_zero_hop
    assert learned.path == b""


def test_learning_from_zero_hop_direct_receptions_finds_more_neighbours(corpus_records) -> None:
    flood_only = sum(
        1
        for record in corpus_records
        if record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)
        and sender_key(record) is not None
    )
    store = PathStore()
    learned = sum(1 for record in corpus_records if store.observe(record) is not None)

    assert learned > flood_only, "zero-hop direct receptions added nothing"


def test_a_zero_hop_reception_records_a_route_and_not_an_absence(corpus_records) -> None:
    zero_hop = next(
        record
        for record in corpus_records
        if isinstance(record.outcome, AdvertOutcome)
        and record.outcome.verified is not None
        and record.packet is not None
        and record.packet.hop_count == 0
        and record.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)
    )

    store = PathStore()
    result = store.observe(zero_hop)

    assert result is not None
    key, learned = result
    assert learned.is_zero_hop
    assert learned.path == b""
    found = store.lookup(key)
    assert found is not None and found.is_zero_hop


def test_an_unknown_destination_is_unknown_not_an_empty_path() -> None:
    store = PathStore()

    assert store.lookup_node_hash(0x42) is None
    assert store.lookup_public_key(bytes(32)) is None


# --- Keying ----------------------------------------------------------------


def test_a_verified_advert_is_keyed_on_its_public_key(flood_advert) -> None:
    key = sender_key(flood_advert)

    assert key is not None
    assert key.public_key is not None
    assert not key.ambiguous
    assert key.node_hash == key.public_key[0]


def test_a_reception_with_only_a_source_hash_is_keyed_ambiguously(corpus_records) -> None:
    """An encrypted DM names its sender by one byte and nothing more (§3)."""
    record = next(
        r
        for r in corpus_records
        if isinstance(r.outcome, Payload)
        and isinstance(r.outcome.payload, DirectEnvelope)
        and r.src_hash is not None
    )

    key = sender_key(record)

    assert key is not None
    assert key.public_key is None
    assert key.ambiguous
    assert key.node_hash == record.src_hash


def test_an_anonymous_request_is_keyed_on_the_key_it_carries(corpus_records) -> None:
    """ANON_REQ hands over a full public key, so its path entry is unambiguous."""
    record = next(
        r
        for r in corpus_records
        if isinstance(r.outcome, Payload) and isinstance(r.outcome.payload, AnonRequestEnvelope)
    )

    key = sender_key(record)

    assert key is not None
    assert key.public_key == record.outcome.payload.sender_public_key
    assert not key.ambiguous


def test_a_hash_keyed_entry_is_not_merged_when_the_public_key_turns_up(flood_advert) -> None:
    """§3's ambiguity is recorded, not resolved: that needs contacts (milestone 5)."""
    assert flood_advert.packet is not None
    verified = flood_advert.outcome.verified
    assert verified is not None

    store = PathStore()
    hash_key = PathKey.for_node_hash(verified.node_hash)
    store._insert(
        hash_key,
        LearnedPath(
            path=b"\x99",
            hash_size=1,
            hop_count=1,
            snr_db=None,
            confirmed_at=flood_advert.received_at - dt.timedelta(seconds=10),
            packet_id="earlier",
        ),
    )
    store.observe(flood_advert)

    key_keyed = PathKey.for_public_key(verified.public_key)
    assert store.destination_count == 2
    ambiguous = store.lookup(hash_key)
    assert ambiguous is not None and ambiguous.path == b"\x99"
    assert store.lookup(key_keyed) is not None
    assert store.lookup(key_keyed) != ambiguous


# --- Resolution ------------------------------------------------------------


def test_the_most_recently_confirmed_route_wins_and_both_are_kept(flood_advert) -> None:
    assert flood_advert.packet is not None
    store = PathStore()
    store.observe(flood_advert)

    other_route = replace(
        flood_advert,
        packet_id="second-route",
        received_at=flood_advert.received_at + dt.timedelta(seconds=30),
        packet=replace(
            flood_advert.packet,
            path=b"\x77" * flood_advert.packet.hash_size * flood_advert.packet.hop_count,
        ),
    )
    store.observe(other_route)

    key = sender_key(flood_advert)
    assert key is not None
    assert len(store.candidates(key)) == 2
    winner = store.lookup(key)
    assert winner is not None
    assert winner.packet_id == "second-route"


def test_re_hearing_a_known_route_reconfirms_rather_than_duplicating(flood_advert) -> None:
    store = PathStore()
    store.observe(flood_advert)
    again = replace(
        flood_advert,
        packet_id="again",
        received_at=flood_advert.received_at + dt.timedelta(seconds=60),
    )
    store.observe(again)

    key = sender_key(flood_advert)
    assert key is not None
    assert len(store.candidates(key)) == 1
    winner = store.lookup(key)
    assert winner is not None and winner.packet_id == "again"


def test_snr_is_recorded_but_does_not_decide(flood_advert) -> None:
    """§13 unknown #3 stays open: recorded, deliberately unscored."""
    assert flood_advert.packet is not None
    store = PathStore()
    strong = replace(flood_advert, snr_db=10.0)
    store.observe(strong)

    weak = replace(
        flood_advert,
        packet_id="weaker-but-newer",
        snr_db=-15.0,
        received_at=flood_advert.received_at + dt.timedelta(seconds=5),
        packet=replace(
            flood_advert.packet,
            path=b"\x55" * flood_advert.packet.hash_size * flood_advert.packet.hop_count,
        ),
    )
    store.observe(weak)

    key = sender_key(flood_advert)
    assert key is not None
    winner = store.lookup(key)
    assert winner is not None
    assert winner.packet_id == "weaker-but-newer"
    assert {c.snr_db for c in store.candidates(key)} == {10.0, -15.0}


# --- Bounds ----------------------------------------------------------------


def test_the_store_is_bounded_by_destination_count(corpus_records) -> None:
    store = PathStore(max_destinations=5)
    for record in corpus_records:
        store.observe(record)

    assert store.destination_count <= 5
    assert cast(int, store.as_json()["evictions"]) > 0


def test_candidates_per_destination_are_bounded(flood_advert) -> None:
    assert flood_advert.packet is not None
    store = PathStore(max_candidates=2)
    for index in range(4):
        store.observe(
            replace(
                flood_advert,
                packet_id=f"route-{index}",
                received_at=flood_advert.received_at + dt.timedelta(seconds=index),
                packet=replace(
                    flood_advert.packet,
                    path=bytes([0x10 + index])
                    * flood_advert.packet.hash_size
                    * max(flood_advert.packet.hop_count, 1),
                ),
            )
        )

    key = sender_key(flood_advert)
    assert key is not None
    candidates = store.candidates(key)
    assert len(candidates) == 2
    # The two newest survive; the oldest routes are what gets dropped.
    assert [c.packet_id for c in candidates] == ["route-2", "route-3"]


def test_the_whole_corpus_learns_without_error(corpus_records) -> None:
    store = PathStore()
    learned = sum(1 for record in corpus_records if store.observe(record) is not None)

    assert learned > 0
    summary = store.as_json()
    assert summary["destinations"] == store.destination_count
    assert cast(int, summary["ambiguous_destinations"]) <= summary["destinations"]
