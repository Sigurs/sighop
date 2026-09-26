"""Direct-message decryption and acknowledgements, end to end over the synthetic corpus.

**This is a self-consistency test, and says so.** The corpus is written by sighop's
own encryptor, so what passes here is that the shared-secret derivation, the cipher
and the MAC agree with each other and with the generator's known plaintext. It does
not show interoperability with any other MeshCore implementation. Interoperability
is anchored only by the firmware-embedded signing keypair and the fixed known-answer
vectors transcribed from the firmware source in `test_crypto.py`. A recorded
exchange with a stock implementation once stood in this module, and was withdrawn
with the recorded corpus (`CORPUS.md`, DESIGN.md §12).

What is still worth having is the negative half: the two key slices
(`Utils::encrypt` keys AES on the first 16 bytes of the 32-byte secret, the HMAC on
all 32) stay distinguishable by evidence rather than by comment, and the helpers
that decide so are shown to be able to fail by aiming them at a message built the
wrong way.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sighop.protocol.crypto import (
    CIPHER_KEY_SIZE,
    PUBLIC_CHANNEL_KEY,
    ack_checksum_for,
    calc_shared_secret,
    compute_mac,
    mac_then_decrypt,
)
from sighop.protocol.packet import PayloadType, decode
from sighop.protocol.payloads import (
    DirectEnvelope,
    TextMessageBody,
    TextType,
    parse_payload,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure
from tests.protocol.corpus import EXCHANGE, CorpusFrame, load_corpus
from tests.protocol.synthetic import (
    EXCHANGE_MESSAGES,
    ROOM_POSTS,
    ROOM_PUSHES,
    Cast,
    CastNode,
    build_cast,
    text_payload,
)

# The vectors name their corpus and what they expect of it, so a corpus change
# that alters a count fails here instead of silently narrowing the test.
EXPECTED_TEXT_FRAMES = 16
"""`TXT_MSG` frames in `synthetic-exchange.jsonl`: 9 between sighop and its peer
(one of them retransmitted, so 8 distinct) and 7 through the room sighop hosts —
three posts by the peer, two refused by a guest, two pushes."""
EXPECTED_DISTINCT_TEXT_FRAMES = 15
EXPECTED_DIRECT_FRAMES = 9
EXPECTED_ROOM_FRAMES = 7
EXPECTED_ACK_FRAMES = 12
"""`ACK` frames in the exchange file, 4-byte and 6-byte forms both."""

EXPECTED_DIRECT_TEXTS = (
    ("syn-sighop", "syn-peer", EXCHANGE_MESSAGES["sighop_first"]),
    ("syn-peer", "syn-sighop", EXCHANGE_MESSAGES["peer_first"]),
    ("syn-peer", "syn-sighop", EXCHANGE_MESSAGES["peer_flooded"]),
    ("syn-sighop", "syn-peer", EXCHANGE_MESSAGES["sighop_reply"]),
    ("syn-sighop", "syn-peer", EXCHANGE_MESSAGES["sighop_routed"]),
    ("syn-peer", "syn-sighop", EXCHANGE_MESSAGES["peer_long"]),
    ("syn-peer", "syn-sighop", EXCHANGE_MESSAGES["peer_long"]),
    ("syn-peer", "syn-sighop", EXCHANGE_MESSAGES["peer_after"]),
    ("syn-peer", "syn-sighop", EXCHANGE_MESSAGES["peer_scan"]),
)
EXPECTED_ROOM_TEXTS = (
    ("syn-peer", "syn-lounge-room", ROOM_POSTS[0][1]),
    ("syn-peer", "syn-lounge-room", ROOM_POSTS[1][1]),
    ("syn-alder", "syn-lounge-room", ROOM_POSTS[2][1]),
    ("syn-alder", "syn-lounge-room", ROOM_POSTS[3][1]),
    ("syn-peer", "syn-lounge-room", ROOM_POSTS[4][1]),
    ("syn-lounge-room", "syn-peer", ROOM_PUSHES[0][1]),
    ("syn-lounge-room", "syn-peer", ROOM_PUSHES[1][1]),
)


@pytest.fixture(scope="module")
def cast() -> Cast:
    return build_cast()


def _exchange() -> list[CorpusFrame]:
    return [frame for frame in load_corpus() if frame.capture_file == EXCHANGE]


def _packet(frame: CorpusFrame):
    packet = decode(frame.raw)
    assert not isinstance(packet, DecodeFailure), frame.describe()
    return packet


def _text_frames() -> list[tuple[CorpusFrame, DirectEnvelope]]:
    out = []
    for frame in _exchange():
        packet = _packet(frame)
        if packet.payload_type is PayloadType.TXT_MSG:
            envelope = parse_payload(packet.payload_type, packet.payload)
            assert isinstance(envelope, DirectEnvelope), frame.describe()
            out.append((frame, envelope))
    return out


def _nodes(cast: Cast, envelope: DirectEnvelope) -> tuple[CastNode, CastNode]:
    by_hash = {node.identity.node_hash: node for node in cast.everyone}
    return by_hash[envelope.src_hash], by_hash[envelope.dest_hash]


def _decrypt(cast: Cast, envelope: DirectEnvelope) -> tuple[CastNode, CastNode, TextMessageBody]:
    source, dest = _nodes(cast, envelope)
    secret = calc_shared_secret(dest.identity, source.public_key)
    candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    assert candidate.matched, f"{source.name} -> {dest.name}: the MAC does not verify"
    assert plaintext is not None
    body = parse_text_message_body(plaintext)
    assert isinstance(body, TextMessageBody)
    return source, dest, body


# --- Decryption over the corpus ----------------------------------------------


def test_the_exchange_holds_the_text_frames_these_vectors_name() -> None:
    frames = _text_frames()
    assert len(frames) == EXPECTED_TEXT_FRAMES
    assert len({frame.raw for frame, _ in frames}) == EXPECTED_DISTINCT_TEXT_FRAMES
    assert EXPECTED_DIRECT_FRAMES + EXPECTED_ROOM_FRAMES == EXPECTED_TEXT_FRAMES
    assert len(EXPECTED_DIRECT_TEXTS) == EXPECTED_DIRECT_FRAMES
    assert len(EXPECTED_ROOM_TEXTS) == EXPECTED_ROOM_FRAMES


def test_every_corpus_direct_message_decrypts_to_the_generators_text(cast) -> None:
    """Every MAC verifies, and every plaintext parses as a text message body equal
    to the one the generator composed."""
    decrypted = []
    for frame, envelope in _text_frames():
        source, dest, body = _decrypt(cast, envelope)
        assert body.text.is_valid_utf8, frame.describe()
        decrypted.append((source.name, dest.name, body.text.text))

    direct = [row for row in decrypted if "syn-lounge-room" not in row[:2]]
    room = [row for row in decrypted if "syn-lounge-room" in row[:2]]
    assert sorted(direct) == sorted(EXPECTED_DIRECT_TEXTS)
    assert sorted(room) == sorted(EXPECTED_ROOM_TEXTS)


def test_a_room_push_is_a_signed_message_carrying_its_authors_key_prefix(cast) -> None:
    pushes = []
    for _frame, envelope in _text_frames():
        source, _dest, body = _decrypt(cast, envelope)
        if source is cast.lounge:
            pushes.append(body)
    assert len(pushes) == len(ROOM_PUSHES)
    authors = {node.public_key[:4]: node.name for node in cast.chat}
    for body, (author_name, _text) in zip(pushes, ROOM_PUSHES, strict=True):
        assert body.txt_type is TextType.SIGNED_PLAIN
        assert authors[body.sender_key_prefix] == author_name


def test_the_retransmitted_message_is_byte_identical_and_decrypts_both_times(cast) -> None:
    frames = [
        (frame, envelope)
        for frame, envelope in _text_frames()
        if _decrypt(cast, envelope)[2].text.text == EXCHANGE_MESSAGES["peer_long"]
    ]
    assert len(frames) == 2
    assert frames[0][0].raw == frames[1][0].raw


# --- Acknowledgements ---------------------------------------------------------


def test_every_corpus_acknowledgement_matches_the_message_it_answers(cast) -> None:
    """`BaseChatMesh.cpp:243`: a truncated SHA-256 over the message's body prefix and
    the *sender's* public key. Both the 4-byte form and the 6-byte form (a 2-byte
    tail keeps the packet hash unique) compare on their first four bytes."""
    expected: dict[bytes, tuple[str, TextMessageBody]] = {}
    for _frame, envelope in _text_frames():
        source, _dest, body = _decrypt(cast, envelope)
        expected[ack_checksum_for(body, source.public_key)] = (source.name, body)

    acks = [frame for frame in _exchange() if _packet(frame).payload_type is PayloadType.ACK]
    assert len(acks) == EXPECTED_ACK_FRAMES
    forms = set()
    for frame in acks:
        payload = _packet(frame).payload
        forms.add(len(payload))
        assert payload[:4] in expected, f"{frame.describe()} answers no message in the corpus"
        if len(payload) == 6:
            assert payload[4] == 0x00, "the extended attempt byte for attempt 0"
    assert forms == {4, 6}


def test_a_flipped_message_changes_the_expected_acknowledgement(cast) -> None:
    _frame, envelope = _text_frames()[1]
    source, _dest, body = _decrypt(cast, envelope)
    other = TextMessageBody(
        timestamp=body.timestamp + 1,
        txt_type=body.txt_type,
        attempt=body.attempt,
        text=body.text,
    )
    assert ack_checksum_for(body, source.public_key) != ack_checksum_for(other, source.public_key)
    assert ack_checksum_for(body, source.public_key) != ack_checksum_for(body, bytes(32))


# --- The negative half: the two key slices stay distinguishable ---------------


def full_secret_as_cipher_key_recovers(secret: bytes, envelope: DirectEnvelope, text: str) -> bool:
    """Whether AES-256 keyed on the whole secret opens `envelope` to `text`."""
    decryptor = Cipher(algorithms.AES256(secret), modes.ECB()).decryptor()
    wrong = decryptor.update(envelope.ciphertext) + decryptor.finalize()
    return text.encode() in wrong


def truncated_key_mac_verifies(secret: bytes, envelope: DirectEnvelope) -> bool:
    """Whether an HMAC keyed on only the first 16 bytes matches the envelope's MAC."""
    mac = hmac.new(secret[:CIPHER_KEY_SIZE], envelope.ciphertext, hashlib.sha256).digest()[:2]
    return mac == envelope.mac


def test_the_full_secret_as_the_cipher_key_does_not_recover_the_plaintext(cast) -> None:
    """`Utils::encrypt` keys AES on the FIRST 16 bytes of the 32-byte secret. The
    HMAC keys on all 32. A comment cannot enforce that distinction; the corpus
    can: taking the whole secret as the cipher key must fail on every message."""
    for _frame, envelope in _text_frames():
        source, dest, body = _decrypt(cast, envelope)
        secret = calc_shared_secret(dest.identity, source.public_key)
        assert not full_secret_as_cipher_key_recovers(secret, envelope, body.text.text)


def test_the_mac_keyed_on_only_the_first_sixteen_bytes_does_not_verify(cast) -> None:
    """`Utils::encryptThenMAC` passes `PUB_KEY_SIZE`, not `CIPHER_KEY_SIZE`."""
    for _frame, envelope in _text_frames():
        source, dest, _body = _decrypt(cast, envelope)
        secret = calc_shared_secret(dest.identity, source.public_key)
        assert compute_mac(secret, envelope.ciphertext) == envelope.mac
        assert not truncated_key_mac_verifies(secret, envelope)


def test_the_slice_checks_can_fail(cast) -> None:
    """A negative that cannot fail proves nothing: aim each check at a message built
    with the wrong slice and it must report the wrong construction."""
    source, dest = cast.peer, cast.sighop
    secret = calc_shared_secret(dest.identity, source.public_key)
    text = "built with the wrong slice"

    # Wrong cipher slice: AES-256 on the whole secret, MAC as normal.
    plaintext = ((1_788_000_000).to_bytes(4, "little") + bytes([0]) + text.encode()).ljust(
        48, b"\x00"
    )
    encryptor = Cipher(algorithms.AES256(secret), modes.ECB()).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    wrong_cipher = DirectEnvelope(
        PayloadType.TXT_MSG,
        dest.identity.node_hash,
        source.identity.node_hash,
        compute_mac(secret, ciphertext),
        ciphertext,
    )
    assert full_secret_as_cipher_key_recovers(secret, wrong_cipher, text)

    # Wrong MAC slice: HMAC keyed on the first 16 bytes only.
    good, _ = text_payload(source, dest, 1_788_000_000, text)
    envelope = parse_payload(PayloadType.TXT_MSG, good)
    assert isinstance(envelope, DirectEnvelope)
    wrong_mac = DirectEnvelope(
        envelope.payload_type,
        envelope.dest_hash,
        envelope.src_hash,
        hmac.new(secret[:CIPHER_KEY_SIZE], envelope.ciphertext, hashlib.sha256).digest()[:2],
        envelope.ciphertext,
    )
    assert truncated_key_mac_verifies(secret, wrong_mac)
    assert not truncated_key_mac_verifies(secret, envelope)


def test_public_channel_key_is_shorter_than_the_secret_it_is_extended_to() -> None:
    """Context for the channel-hash negative in `test_corpus_channel_decrypt.py`."""
    assert len(PUBLIC_CHANNEL_KEY.key) == 16
    assert len(PUBLIC_CHANNEL_KEY.secret) == 32
