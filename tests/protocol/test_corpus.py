"""Corpus replay: the protocol layer verified against recorded reality.

351 frames captured off the live mesh over two nights (DESIGN.md §12). What
this proves and — just as importantly — what it does not, is written down in
`CORPUS.md`. In short: it proves framing, adverts and signature verification;
it cannot prove decryption, because every encrypted payload in it is addressed
to a third party.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from sighop.protocol.crypto import VerifiedAdvert, verify_advert
from sighop.protocol.packet import Packet, PayloadType, RouteType, decode, encode
from sighop.protocol.payloads import (
    Advert,
    NodeType,
    build_payload,
    parse_payload,
)
from sighop.protocol.result import DecodeFailure

from tests.protocol.corpus import (
    CAPTURE_FILES,
    CAPTURES_DIR,
    EXPECTED_FRAME_COUNT,
    CorpusError,
    CorpusFrame,
    load_corpus,
)
from tests.protocol.generate_golden import GOLDEN_PATH
from tests.protocol.golden import render_corpus

# Recorded expectations, spot-checked against the independent analysis in the
# proposal before being committed as fixtures (design D10).
EXPECTED_PAYLOAD_TYPES = {
    PayloadType.TXT_MSG: 108,
    PayloadType.GRP_TXT: 91,
    PayloadType.ADVERT: 53,
    PayloadType.ACK: 42,
    PayloadType.ANON_REQ: 16,
    PayloadType.PATH: 15,
    PayloadType.RESPONSE: 11,
    PayloadType.REQ: 6,
    PayloadType.GRP_DATA: 5,
    PayloadType.TRACE: 4,
}
EXPECTED_ROUTE_TYPES = {RouteType.FLOOD: 171, RouteType.DIRECT: 180}
EXPECTED_HOP_COUNTS = {0: 119, 1: 66, 2: 95, 3: 64, 4: 5, 5: 2}
EXPECTED_HASH_SIZES = {1: 152, 2: 106, 3: 93}
EXPECTED_ADVERT_COUNT = 53


@pytest.fixture(scope="module")
def frames() -> tuple[CorpusFrame, ...]:
    return load_corpus()


@pytest.fixture(scope="module")
def decoded(frames: tuple[CorpusFrame, ...]) -> list[tuple[CorpusFrame, Packet]]:
    out: list[tuple[CorpusFrame, Packet]] = []
    for frame in frames:
        packet = decode(frame.raw)
        assert isinstance(packet, Packet), (
            f"frame failed to decode: {frame.describe()} -> {packet}"
        )
        out.append((frame, packet))
    return out


# --- Replay ----------------------------------------------------------------


def test_corpus_holds_the_recorded_number_of_frames(
    frames: tuple[CorpusFrame, ...],
) -> None:
    assert len(frames) == EXPECTED_FRAME_COUNT


def test_every_corpus_frame_decodes(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    assert len(decoded) == EXPECTED_FRAME_COUNT


def test_every_corpus_payload_parses(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    for frame, packet in decoded:
        parsed = parse_payload(packet.payload_type, packet.payload)
        assert not isinstance(parsed, DecodeFailure), (
            f"payload failed to parse: {frame.describe()} -> {parsed}"
        )


def test_a_missing_capture_file_fails_loudly(tmp_path: Path) -> None:
    """Never a silent pass on an empty corpus."""
    from tests.protocol import corpus

    with pytest.raises(CorpusError, match="missing"):
        original = corpus.CAPTURES_DIR
        try:
            corpus.CAPTURES_DIR = tmp_path
            corpus._load_file("2026-09-02.jsonl")
        finally:
            corpus.CAPTURES_DIR = original


def test_corpus_files_are_treated_as_read_only(
    frames: tuple[CorpusFrame, ...],
) -> None:
    """The captures and their provenance sidecars are evidence, not fixtures the
    suite may rewrite. Recording their digests here makes a stray write fail.
    """
    import hashlib

    for name in CAPTURE_FILES:
        for path in (CAPTURES_DIR / name, CAPTURES_DIR / name.replace(".jsonl", ".meta.json")):
            assert path.is_file(), f"corpus evidence missing: {path}"
        before = hashlib.sha256((CAPTURES_DIR / name).read_bytes()).hexdigest()
        load_corpus()
        after = hashlib.sha256((CAPTURES_DIR / name).read_bytes()).hexdigest()
        assert before == after, f"the harness modified {name}"


# --- Round-trip ------------------------------------------------------------


def test_every_corpus_frame_re_encodes_byte_identically(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    for frame, packet in decoded:
        assert encode(packet) == frame.raw, f"re-encode differs: {frame.describe()}"


def test_every_corpus_payload_rebuilds_byte_identically(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    for frame, packet in decoded:
        parsed = parse_payload(packet.payload_type, packet.payload)
        assert not isinstance(parsed, DecodeFailure)
        assert build_payload(parsed) == packet.payload, (
            f"payload rebuild differs: {frame.describe()}"
        )


# --- Distributions ---------------------------------------------------------


def test_payload_type_distribution_holds(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    counts = collections.Counter(packet.payload_type for _, packet in decoded)
    assert dict(counts) == EXPECTED_PAYLOAD_TYPES


def test_route_type_distribution_holds(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    counts = collections.Counter(packet.route_type for _, packet in decoded)
    assert dict(counts) == EXPECTED_ROUTE_TYPES


def test_hop_count_distribution_holds(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    counts = collections.Counter(packet.hop_count for _, packet in decoded)
    assert dict(counts) == EXPECTED_HOP_COUNTS


def test_path_hash_size_distribution_holds(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    """The evidence that multi-byte path hashes are live on this mesh, and so
    that DESIGN.md §4.2's original `path_length > 64` rule was wrong.
    """
    counts = collections.Counter(packet.hash_size for _, packet in decoded)
    assert dict(counts) == EXPECTED_HASH_SIZES


def test_no_transport_routed_frames_in_the_corpus(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    """A recorded gap, not an oversight: `TRANSPORT_*` routing gets synthetic
    tests only. See `CORPUS.md`.
    """
    assert all(packet.transport_codes is None for _, packet in decoded)


# --- Adverts ---------------------------------------------------------------


@pytest.fixture(scope="module")
def adverts(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> list[tuple[CorpusFrame, Advert]]:
    out: list[tuple[CorpusFrame, Advert]] = []
    for frame, packet in decoded:
        if packet.payload_type is not PayloadType.ADVERT:
            continue
        parsed = parse_payload(packet.payload_type, packet.payload)
        assert isinstance(parsed, Advert)
        out.append((frame, parsed))
    return out


def test_all_corpus_adverts_verify(
    adverts: list[tuple[CorpusFrame, Advert]],
) -> None:
    assert len(adverts) == EXPECTED_ADVERT_COUNT
    for frame, advert in adverts:
        result = verify_advert(advert)
        assert isinstance(result, VerifiedAdvert), (
            f"advert failed verification: {frame.describe()} -> {result}"
        )


def test_tampering_with_a_corpus_advert_breaks_verification(
    adverts: list[tuple[CorpusFrame, Advert]],
) -> None:
    _, advert = adverts[0]
    tampered = Advert(
        public_key=advert.public_key,
        timestamp=advert.timestamp ^ 1,
        signature=advert.signature,
        appdata=advert.appdata,
    )
    assert not isinstance(verify_advert(tampered), VerifiedAdvert)


def test_corpus_advert_names_and_types_decode_consistently(
    adverts: list[tuple[CorpusFrame, Advert]],
) -> None:
    """Names decode cleanly only under the multi-byte path hash encoding: read
    with 1-byte hashes, this mesh's multi-hop adverts yield truncated names and
    corrupt flags (`0xfa` / `rala Hill repeater` rather than `0x92` /
    `[redacted]`).
    """
    named = 0
    for frame, advert in adverts:
        result = verify_advert(advert)
        assert isinstance(result, VerifiedAdvert)
        assert isinstance(result.appdata.node_type, NodeType), (
            f"unknown node type in {frame.describe()}"
        )
        if result.appdata.name is not None:
            assert result.appdata.name.is_valid_utf8, (
                f"advert name is not valid UTF-8: {frame.describe()}"
            )
            named += 1
    assert named == EXPECTED_ADVERT_COUNT, "every corpus advert carries a name"


# --- Golden file -----------------------------------------------------------


def test_decoded_output_matches_the_golden_file() -> None:
    assert GOLDEN_PATH.is_file(), (
        f"{GOLDEN_PATH} is missing; regenerate it with "
        "`uv run python -m tests.protocol.generate_golden` and review the diff"
    )
    assert render_corpus() == GOLDEN_PATH.read_text()


def test_golden_file_contains_no_decrypted_content() -> None:
    """Design D8: structural fields and ciphertext digests only. sighop holds no
    key for any corpus frame, so any plaintext appearing here would be a bug —
    but the rule is asserted now so it still holds at milestone 4.
    """
    data_lines = [
        line for line in GOLDEN_PATH.read_text().splitlines() if not line.startswith("#")
    ]
    assert data_lines
    for line in data_lines:
        assert "plaintext" not in line
        assert "decrypted" not in line
        if " len=" in line:
            # Every rendered ciphertext is reduced to a length and a digest.
            assert " sha=" in line, (
                f"a ciphertext was rendered without being reduced to a digest: {line}"
            )
