"""The shared duplicate cache (milestone 3, `rx-dedup`, design D10/D11).

Real repeats come from the corpus: flood traffic in it genuinely arrives more
than once, and the whole reason `net/rx.py` stayed stateless (design D12) is
that a replayed capture still shows every copy. Synthetic records appear only
where the corpus cannot supply a shape — a payload replayed after the TTL, a
cache pushed past its entry cap.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.net.dedup import DedupCache, content_key, key_for
from sighop.net.rx import RxRecord, decode_event
from sighop.protocol.packet import PayloadType
from sighop.radio.modem import RxEvent, RxMeta, UnparsedEvent
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CORPUS_DIR, CORPUS_FILES


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CORPUS_FILES:
        replay = CaptureReplay.open(CORPUS_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
    return records


def _at(record: RxRecord, when: dt.datetime, *, packet_id: str) -> RxRecord:
    """The same reception again: new id, new time, identical bytes."""
    from dataclasses import replace

    return replace(record, packet_id=packet_id, received_at=when)


def _first_decodable(records: list[RxRecord]) -> RxRecord:
    return next(r for r in records if not r.failed and r.packet is not None)


# --- The key ---------------------------------------------------------------


def test_the_same_payload_over_a_different_path_is_a_duplicate(corpus_records) -> None:
    from dataclasses import replace

    record = _first_decodable(corpus_records)
    assert record.packet is not None
    # What a repeater does to a packet: one more hop on the path, hop count up.
    rehopped = replace(
        record,
        packet_id="rehopped",
        received_at=record.received_at + dt.timedelta(seconds=1),
        packet=replace(
            record.packet,
            path=record.packet.path + b"\xaa" * record.packet.hash_size,
            hop_count=record.packet.hop_count + 1,
        ),
    )
    assert rehopped.path != record.path

    cache = DedupCache()
    cache.observe(record)

    assert cache.observe(rehopped).is_duplicate


def test_identical_payload_bytes_under_different_types_are_not_duplicates() -> None:
    payload = b"\x01\x02\x03\x04"
    assert content_key(int(PayloadType.ADVERT), payload) != content_key(
        int(PayloadType.TXT_MSG), payload
    )


def test_a_record_with_no_payload_has_no_key() -> None:
    record = decode_event(UnparsedEvent(raw=b"\x80\x01", reason="unrecognized command byte"))
    assert key_for(record) is None


# --- Verdicts --------------------------------------------------------------


def test_a_repeat_is_reported_as_a_duplicate_of_the_first_reception(corpus_records) -> None:
    record = _first_decodable(corpus_records)
    cache = DedupCache()

    first = cache.observe(record)
    repeat = cache.observe(
        _at(record, record.received_at + dt.timedelta(seconds=2), packet_id="second")
    )

    assert not first.is_duplicate
    assert repeat.is_duplicate
    assert repeat.first_packet_id == record.packet_id
    assert repeat.copy_number == 2
    assert repeat.interval_seconds == pytest.approx(2.0)


def test_a_third_copy_counts_as_the_third(corpus_records) -> None:
    record = _first_decodable(corpus_records)
    cache = DedupCache()
    cache.observe(record)

    cache.observe(_at(record, record.received_at + dt.timedelta(seconds=1), packet_id="b"))
    third = cache.observe(_at(record, record.received_at + dt.timedelta(seconds=3), packet_id="c"))

    assert third.is_duplicate
    assert third.copy_number == 3
    # Measured from the *first* reception: that is the span the TTL must cover.
    assert third.interval_seconds == pytest.approx(3.0)


def test_the_corpus_contains_real_repeats(corpus_records) -> None:
    """Not a property of the cache — a property of flood routing, worth asserting."""
    cache = DedupCache()
    verdicts = [cache.observe(record) for record in corpus_records]

    duplicates = [v for v in verdicts if v.is_duplicate]
    assert duplicates, "the corpus holds no repeats; the fixture or the key is wrong"
    assert cache.stats.hit_rate > 0


# --- Bounds ----------------------------------------------------------------


def test_a_repeat_after_the_ttl_is_a_first_reception(corpus_records) -> None:
    record = _first_decodable(corpus_records)
    cache = DedupCache(ttl_seconds=60)
    cache.observe(record)

    late = cache.observe(
        _at(record, record.received_at + dt.timedelta(seconds=61), packet_id="late")
    )

    assert not late.is_duplicate
    assert cache.stats.evictions_by_age == 1


def test_a_repeat_within_the_ttl_is_still_a_duplicate(corpus_records) -> None:
    record = _first_decodable(corpus_records)
    cache = DedupCache(ttl_seconds=60)
    cache.observe(record)

    inside = cache.observe(
        _at(record, record.received_at + dt.timedelta(seconds=59), packet_id="inside")
    )

    assert inside.is_duplicate


def test_the_entry_cap_evicts_the_least_recently_used(corpus_records) -> None:
    records = [r for r in corpus_records if not r.failed and r.packet is not None]
    distinct: list[RxRecord] = []
    seen: set[bytes] = set()
    for record in records:
        key = key_for(record)
        if key is not None and key not in seen:
            seen.add(key)
            distinct.append(record)
        if len(distinct) == 4:
            break
    assert len(distinct) == 4, "need four distinct corpus payloads"

    cache = DedupCache(max_entries=3)
    for record in distinct[:3]:
        cache.observe(record)
    # Touch the oldest so it is no longer the least recently used.
    cache.observe(
        _at(distinct[0], distinct[0].received_at + dt.timedelta(seconds=1), packet_id="touch")
    )
    cache.observe(distinct[3])

    assert cache.stats.entries == 3
    assert cache.stats.evictions_by_cap == 1
    # The touched entry survived; the one after it did not.
    assert cache.observe(
        _at(distinct[0], distinct[0].received_at + dt.timedelta(seconds=2), packet_id="x")
    ).is_duplicate
    assert not cache.observe(
        _at(distinct[1], distinct[1].received_at + dt.timedelta(seconds=2), packet_id="y")
    ).is_duplicate


@pytest.mark.parametrize(("ttl", "cap"), [(0, 10), (-1, 10), (60, 0), (60, -1)])
def test_nonsensical_bounds_are_rejected(ttl: float, cap: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        DedupCache(ttl_seconds=ttl, max_entries=cap)


# --- Undecodable frames ----------------------------------------------------


def test_two_identical_corrupt_frames_both_pass_through() -> None:
    cache = DedupCache()
    event = UnparsedEvent(raw=b"\x80\x01\x02", reason="unrecognized command byte")

    first = cache.observe(decode_event(event))
    second = cache.observe(decode_event(event))

    assert not first.is_duplicate
    assert not second.is_duplicate
    assert cache.stats.considered == 0
    assert cache.stats.passed_through == 2


def test_two_identical_truncated_frames_both_pass_through() -> None:
    cache = DedupCache()
    truncated = RxEvent(packet=b"\x04", rx_meta=RxMeta(snr_db=1.0, rssi_dbm=-90))

    assert not cache.observe(decode_event(truncated)).is_duplicate
    assert not cache.observe(decode_event(truncated)).is_duplicate


# --- Measurement -----------------------------------------------------------


def test_stats_report_what_the_sizing_decision_needs(corpus_records) -> None:
    cache = DedupCache()
    for record in corpus_records:
        cache.observe(record)

    stats = cache.stats
    assert stats.considered > 0
    assert 0.0 <= stats.hit_rate <= 1.0
    assert stats.entries <= stats.max_entries
    assert stats.widest_interval_seconds is not None
    assert set(stats.as_json()) >= {
        "hit_rate",
        "entries",
        "max_entries",
        "ttl_seconds",
        "widest_interval_seconds",
    }
