"""Channel decryption, end to end over the synthetic corpus (`mesh-crypto`).

**This is a self-consistency test, and says so.** Every `GRP_TXT` frame in the
corpus was written by sighop's own encryptor: the frames on the stock Public
channel open under the key every stock client ships with, and the frames on a second
synthetic channel open under a key derived from a public name. What passes here is
that the channel key handling, the cipher and the MAC agree with the generator's
known plaintext. It does not show interoperability with another MeshCore
implementation — the 85 Public-channel frames that once stood in this module, from
real nodes, were withdrawn with the recorded corpus (`CORPUS.md`).

**Provenance.** The `GRP_TXT` frames are in `synthetic-channels.jsonl`, and
`GRP_TXT` payloads are deduplicated by their bytes (a repeated flood is one frame).
Measured from a reviewed run of the generator and asserted here:

* 32 distinct frames on `0x11`, the Public channel,
* 22 distinct frames on the second channel's hash (`0x33`), and
* 4 `GRP_DATA` frames on the second channel, which are not text.

The negative that keeps the key-length rule falsifiable: the Public hash over the
zero-extended 32-byte buffer is `0x17`, which no frame here carries. HMAC cannot
supply that negative, because it zero-pads a short key itself (DESIGN.md §5).
"""

from __future__ import annotations

import hashlib
from functools import cache

import pytest

from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, mac_then_decrypt
from sighop.protocol.packet import PayloadType, decode
from sighop.protocol.payloads import (
    GroupEnvelope,
    TextType,
    build_group_text_body,
    parse_group_text_body,
    parse_payload,
)
from sighop.protocol.result import DecodeFailure
from tests.protocol.corpus import CHANNELS, CORPUS_FILES, _load_file
from tests.protocol.synthetic import (
    CLUB_MESSAGES,
    GROUP_DATA_PAYLOADS,
    PUBLIC_MESSAGES,
    build_cast,
    club_key,
)

PUBLIC_HASH = 0x11
ZERO_EXTENDED_PUBLIC_HASH = 0x17
CLUB_HASH = 0x33

EXPECTED_PUBLIC_FRAMES = 32
EXPECTED_CLUB_TEXT_FRAMES = 22
EXPECTED_CLUB_DATA_FRAMES = 4
EXPECTED_DISTINCT_PER_FILE = {
    (CHANNELS, PayloadType.GRP_TXT, PUBLIC_HASH): EXPECTED_PUBLIC_FRAMES,
    (CHANNELS, PayloadType.GRP_TXT, CLUB_HASH): EXPECTED_CLUB_TEXT_FRAMES,
    (CHANNELS, PayloadType.GRP_DATA, CLUB_HASH): EXPECTED_CLUB_DATA_FRAMES,
}


@cache
def distinct_group_frames() -> tuple[tuple[str, PayloadType, GroupEnvelope], ...]:
    """Every distinct group envelope across the corpus, with its first file."""
    seen: dict[tuple[int, bytes], tuple[str, PayloadType, GroupEnvelope]] = {}
    for name in CORPUS_FILES:
        for frame in _load_file(name):
            packet = decode(frame.raw)
            if isinstance(packet, DecodeFailure):
                continue
            if packet.payload_type not in (PayloadType.GRP_TXT, PayloadType.GRP_DATA):
                continue
            key = (int(packet.payload_type), packet.payload)
            if key in seen:
                continue
            envelope = parse_payload(packet.payload_type, packet.payload)
            assert isinstance(envelope, GroupEnvelope), frame.describe()
            seen[key] = (name, packet.payload_type, envelope)
    return tuple(seen.values())


def _text(hash_: int) -> list[tuple[str, GroupEnvelope]]:
    return [
        (name, envelope)
        for name, kind, envelope in distinct_group_frames()
        if kind is PayloadType.GRP_TXT and envelope.channel_hash == hash_
    ]


def test_the_corpus_holds_the_group_frames_this_vector_names() -> None:
    counts: dict[tuple[str, PayloadType, int], int] = {}
    for name, kind, envelope in distinct_group_frames():
        key = (name, kind, envelope.channel_hash)
        counts[key] = counts.get(key, 0) + 1
    assert counts == EXPECTED_DISTINCT_PER_FILE


def test_the_second_channels_hash_is_neither_publics_nor_the_mistaken_one() -> None:
    assert club_key().channel_hash == CLUB_HASH
    assert CLUB_HASH not in (PUBLIC_HASH, ZERO_EXTENDED_PUBLIC_HASH)


def test_every_public_frame_verifies_decrypts_and_rebuilds() -> None:
    cast_names = build_cast().names
    public = _text(PUBLIC_HASH)
    assert len(public) == EXPECTED_PUBLIC_FRAMES
    lines = []
    for name, envelope in public:
        match, plaintext = mac_then_decrypt(
            PUBLIC_CHANNEL_KEY.secret, envelope.mac, envelope.ciphertext
        )
        assert match.matched, name
        assert plaintext is not None
        body = parse_group_text_body(plaintext)
        assert not isinstance(body, DecodeFailure), name
        assert body.message.txt_type is TextType.PLAIN, name
        assert body.message.attempt == 0, name
        assert body.unverified_sender_name in cast_names, name
        rebuilt = build_group_text_body(
            body.message.timestamp, body.unverified_sender_name, body.body
        )
        assert rebuilt == plaintext.rstrip(b"\x00"), name
        lines.append((body.unverified_sender_name, body.body))
    assert sorted(lines) == sorted(PUBLIC_MESSAGES), "the frames are the generator's messages"


def test_every_second_channel_frame_opens_under_its_own_key_and_only_that() -> None:
    club = club_key()
    frames = _text(CLUB_HASH)
    assert len(frames) == EXPECTED_CLUB_TEXT_FRAMES
    lines = []
    for name, envelope in frames:
        match, plaintext = mac_then_decrypt(club.secret, envelope.mac, envelope.ciphertext)
        assert match.matched, name
        assert plaintext is not None
        body = parse_group_text_body(plaintext)
        assert not isinstance(body, DecodeFailure), name
        lines.append((body.unverified_sender_name, body.body))
    assert sorted(lines) == sorted(CLUB_MESSAGES)


def test_no_frame_on_another_channel_opens_under_public() -> None:
    """The closed-channel negative: a frame whose channel hash is not `0x11` fails
    its MAC under the Public key, and opens under the second channel's key."""
    club = club_key()
    other = [(n, e) for n, _kind, e in distinct_group_frames() if e.channel_hash != PUBLIC_HASH]
    assert len(other) == EXPECTED_CLUB_TEXT_FRAMES + EXPECTED_CLUB_DATA_FRAMES
    for name, envelope in other:
        under_public, plaintext = mac_then_decrypt(
            PUBLIC_CHANNEL_KEY.secret, envelope.mac, envelope.ciphertext
        )
        assert not under_public.matched, name
        assert plaintext is None
        under_club, _ = mac_then_decrypt(club.secret, envelope.mac, envelope.ciphertext)
        assert under_club.matched, name


def test_group_data_is_opaque_to_the_text_parser_and_carries_the_generators_bytes() -> None:
    club = club_key()
    data = [(n, e) for n, kind, e in distinct_group_frames() if kind is PayloadType.GRP_DATA]
    assert len(data) == EXPECTED_CLUB_DATA_FRAMES
    recovered = []
    for _name, envelope in data:
        _match, plaintext = mac_then_decrypt(club.secret, envelope.mac, envelope.ciphertext)
        assert plaintext is not None
        recovered.append(plaintext[4:].rstrip(b"\x00"))
    assert sorted(recovered) == sorted(GROUP_DATA_PAYLOADS)


# --- The channel hash is taken over the key at its real length -----------------


def test_the_public_hash_over_the_zero_extended_buffer_is_not_the_one_the_corpus_carries() -> None:
    """`GroupChannel` holds a zero-filled 32-byte buffer, but the channel hash is
    taken over the key's real length (`BaseChatMesh.cpp:907-910`). Hashing the
    buffer instead gives `0x17`, which no corpus frame carries."""
    assert PUBLIC_CHANNEL_KEY.channel_hash == PUBLIC_HASH
    assert hashlib.sha256(PUBLIC_CHANNEL_KEY.secret).digest()[0] == ZERO_EXTENDED_PUBLIC_HASH
    hashes = {envelope.channel_hash for _n, _k, envelope in distinct_group_frames()}
    assert ZERO_EXTENDED_PUBLIC_HASH not in hashes
    assert PUBLIC_HASH in hashes


@pytest.mark.parametrize("length", [16, 32])
def test_the_hash_depends_on_the_key_length_not_only_its_bytes(length: int) -> None:
    """The two readings of the same key differ; the corpus is on the right one."""
    key = PUBLIC_CHANNEL_KEY.key
    padded = key.ljust(length, b"\x00")
    expected = PUBLIC_HASH if length == 16 else ZERO_EXTENDED_PUBLIC_HASH
    assert hashlib.sha256(padded).digest()[0] == expected
