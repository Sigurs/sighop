"""Corpus replay: the protocol layer verified against recorded reality.

1093 frame records captured off the live mesh and the air sighop itself used,
over six sessions (DESIGN.md §12) — the 351 milestone 0 frames, the 91 the
milestone 2 live runs recorded, which is where the first
`ROUTE_TYPE_TRANSPORT_FLOOD` and the first CONTROL payloads came from, the 555
from milestone 3's long receive-only session, the 6 from milestone 4's
first-transmit exercise, and the 90 from milestone 6's live room-server
exercise. What this proves and — just as importantly — what it does not, is
written down in `CORPUS.md`. In short: it proves framing, adverts and signature
verification; it cannot prove decryption, because every encrypted payload in it
is addressed to a third party or — for the room-server session — decryptable
only with a key that lives outside the repository.
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
    SIDECAR_PROVENANCE_FILES,
    CorpusError,
    CorpusFrame,
    load_corpus,
)
from tests.protocol.generate_golden import GOLDEN_PATH
from tests.protocol.golden import render_corpus

# Recorded expectations, spot-checked against the independent analysis in the
# proposal before being committed as fixtures (design D10).
#
# Milestone 4 added the first-transmit session: 6 frames, every one DIRECT with
# an empty path, of which 3 are frames sighop itself transmitted. The deltas
# against milestone 3's figures are therefore TXT_MSG +2, ACK +2, ADVERT +2,
# DIRECT +6, hop count 0 +6, hash size 1 +6 — and nothing else moved.
#
# Milestone 6 added the live room-server exercise: 90 frames (58 received, 32
# sighop transmitted) — a room login and its acknowledgement carried as
# ANON_REQ, posts and refusals as TXT_MSG, pushes and their acknowledgements as
# ACK, PATH exchanges either side learned a route from, a telemetry REQ, and
# both boards' adverts. The deltas against milestone 4's figures are TXT_MSG
# +36, ADVERT +4, ACK +19, ANON_REQ +15, PATH +14, REQ +2, FLOOD +51, DIRECT
# +39, hop count 0 +67, hop count 1 +23, hash size 1 +26, hash size 3 +64.
EXPECTED_PAYLOAD_TYPES = {
    PayloadType.GRP_TXT: 341,
    PayloadType.TXT_MSG: 229,
    PayloadType.CONTROL: 168,
    PayloadType.ADVERT: 98,
    PayloadType.ACK: 88,
    PayloadType.ANON_REQ: 57,
    PayloadType.PATH: 52,
    PayloadType.RESPONSE: 36,
    PayloadType.REQ: 14,
    PayloadType.GRP_DATA: 5,
    PayloadType.TRACE: 5,
}
EXPECTED_ROUTE_TYPES = {
    RouteType.FLOOD: 553,
    RouteType.DIRECT: 539,
    RouteType.TRANSPORT_FLOOD: 1,
}
EXPECTED_HOP_COUNTS = {0: 491, 1: 349, 2: 177, 3: 69, 4: 5, 5: 2}
EXPECTED_HASH_SIZES = {1: 519, 2: 109, 3: 465}
EXPECTED_ADVERT_COUNT = 98
# The one transport-routed frame in the corpus, sighted on 2026-09-04: a
# TRANSPORT_FLOOD advert. Its codes are recorded here so a codec change that
# stops reading them fails on evidence rather than on a synthetic fixture.
EXPECTED_TRANSPORT_FRAMES = 1
EXPECTED_TRANSPORT_CODES = (117, 0)


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
    """The captures and their provenance are evidence, not fixtures the suite
    may rewrite. Recording their digests here makes a stray write fail.
    """
    import hashlib

    for name in CAPTURE_FILES:
        path = CAPTURES_DIR / name
        assert path.is_file(), f"corpus evidence missing: {path}"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        load_corpus()
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert before == after, f"the harness modified {name}"


def test_every_corpus_file_carries_provenance(
    frames: tuple[CorpusFrame, ...],
) -> None:
    """A corpus file without recorded recording conditions is a fixture of
    unknown origin (DESIGN.md §12). Milestone 2 files carry a `capture_meta`
    header; the two milestone 0 files, which predate it, carry a sidecar.
    """
    import json

    for name in CAPTURE_FILES:
        path = CAPTURES_DIR / name
        if name in SIDECAR_PROVENANCE_FILES:
            sidecar = CAPTURES_DIR / name.replace(".jsonl", ".meta.json")
            assert sidecar.is_file(), f"provenance sidecar missing: {sidecar}"
            continue
        with path.open("r", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
        assert first.get("kind") == "capture_meta", (
            f"{name} has no capture_meta header and no provenance sidecar"
        )
        assert first["device_name"]["value"], f"{name} header names no device"


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


def test_the_one_transport_routed_frame_decodes_with_its_codes(
    decoded: list[tuple[CorpusFrame, Packet]],
) -> None:
    """Transport routing stopped being a synthetic-only shape on 2026-09-04.

    One frame, so this asserts it exactly rather than a distribution: a codec
    change that drops the transport-code field, or reads it as path bytes,
    fails here against recorded air rather than against a fixture we wrote.
    `TRANSPORT_DIRECT` is still unsighted and still synthetic-only.
    """
    routed = [(frame, packet) for frame, packet in decoded if packet.transport_codes is not None]

    assert len(routed) == EXPECTED_TRANSPORT_FRAMES
    frame, packet = routed[0]
    assert packet.route_type is RouteType.TRANSPORT_FLOOD, frame.describe()
    assert packet.transport_codes == EXPECTED_TRANSPORT_CODES, frame.describe()
    assert packet.payload_type is PayloadType.ADVERT, frame.describe()


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
