"""Decryption proven against another implementation (milestone 4, `mesh-crypto`).

**This module is the milestone's exit criterion.** Every other cryptographic test
in this suite is sighop checking sighop: a round-trip against our own encryptor,
or a vector transcribed from reading `Utils.cpp`. Both would pass if we had
misread the firmware consistently — and we *had* misread it, about the
acknowledgement construction, until `BaseChatMesh.cpp` was checked (DESIGN.md §5).

What is asserted here was produced by a different implementation on different
hardware:

* **Capture**: `captures/2026-09-04-first-transmit.jsonl`, records 2-5.
* **Producer**: a Heltec V3 running stock `companion_radio` v1.17.1-d929643
  (build 14-Aug-2026, companion protocol fw ver 13), public key
  `[redacted]…`, on 869.618 MHz / BW 62.5 kHz / SF8 / CR8.
* **Key**: `tests/fixtures/burned-first-transmit.json` — a **burned** identity.
  It has been published in this repository and must never be used on air again;
  `sighop keys new` makes another in one command. Committing it is what turns one
  evening's log line into a test that runs on every commit (design D11), and the
  alternative — keeping the key out of the repository — would leave the corpus
  holding a ciphertext nothing can open, which is the situation milestone 1
  started in.

The corpus holds sighop's key for **this exchange and no other ciphertext in it**.
Every other encrypted payload in the 1003-frame corpus is addressed to a third
party and stays unopenable, exactly as `CORPUS.md` says.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sighop.keystore import load_keyfile
from sighop.protocol.crypto import (
    CIPHER_KEY_SIZE,
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
from tests.protocol.corpus import (
    CAPTURES_DIR,
    PEER_ACK_INDEX,
    PEER_DM_INDEX,
    SIGHOP_ACK_INDEX,
    SIGHOP_DM_INDEX,
    CorpusFrame,
    first_transmit_frame,
)

BURNED_KEYFILE = CAPTURES_DIR.parent / "tests" / "fixtures" / "burned-first-transmit.json"

PEER_PUBLIC_KEY = bytes.fromhex("[redacted]")
PEER_FIRMWARE = "v1.17.1-d929643"

# What the peer sent us, and what we sent it. Recorded, never derived from a run.
PEER_MESSAGE_TEXT = "hej sighop, this is the peer"
PEER_MESSAGE_TIMESTAMP = 1788558262
SIGHOP_MESSAGE_TEXT = "sighop first transmission, milestone 4"
SIGHOP_MESSAGE_TIMESTAMP = 1788558260

# The acknowledgements each side actually put on the air.
PEER_ACK = bytes.fromhex("2b03574b")
PEER_ACK_TAIL = bytes.fromhex("0091")
SIGHOP_ACK = bytes.fromhex("1df0f21a")


@pytest.fixture(scope="module")
def burned_identity():
    """The published test identity. Loading it says out loud that it is burned."""
    keyfile = load_keyfile(BURNED_KEYFILE)
    assert keyfile.burned, (
        "the committed exercise keyfile must be marked burned in the file: it is "
        "published, and nothing may mistake it for a usable identity"
    )
    return keyfile.identity


@pytest.fixture(scope="module")
def shared_secret(burned_identity) -> bytes:
    return calc_shared_secret(burned_identity, PEER_PUBLIC_KEY)


def envelope_of(frame: CorpusFrame) -> DirectEnvelope:
    packet = decode(frame.raw)
    assert packet.payload_type is PayloadType.TXT_MSG, frame.describe()
    parsed = parse_payload(packet.payload_type, packet.payload)
    assert isinstance(parsed, DirectEnvelope), frame.describe()
    return parsed


# --- The exit criterion ----------------------------------------------------


def test_the_recorded_peer_message_decrypts(shared_secret) -> None:
    """A ciphertext a foreign MeshCore produced, opened with a key we hold.

    The ECDH, the AES-128-ECB key slice and the 2-byte HMAC truncation are all
    confirmed by this one assertion passing. Before it existed they were an
    untested assumption sitting under every entity type milestones 5-8 build.
    """
    frame = first_transmit_frame(PEER_DM_INDEX)
    envelope = envelope_of(frame)

    candidate, plaintext = mac_then_decrypt(shared_secret, envelope.mac, envelope.ciphertext)

    assert candidate.matched, (
        f"the MAC did not verify for {frame.describe()}: the shared secret "
        "derivation disagrees with the peer's"
    )
    assert plaintext is not None
    body = parse_text_message_body(plaintext)
    assert body.timestamp == PEER_MESSAGE_TIMESTAMP
    assert body.txt_type is TextType.PLAIN
    assert body.attempt == 0
    assert body.text.is_valid_utf8
    assert body.text.text == PEER_MESSAGE_TEXT


def test_the_vector_is_anchored_to_its_provenance() -> None:
    """The vector and the capture that produced it must not drift apart."""
    frame = first_transmit_frame(PEER_DM_INDEX)

    assert frame.capture_file == "2026-09-04-first-transmit.jsonl"
    assert frame.index == PEER_DM_INDEX
    assert frame.transmitted is False, "the decrypt vector must be a reception"
    assert BURNED_KEYFILE.is_file(), "the key that opens the vector is not committed"
    # The producing firmware, so the vector can be re-derived rather than trusted.
    assert PEER_FIRMWARE in (CAPTURES_DIR / frame.capture_file).read_text()


def test_our_own_recorded_message_decrypts_with_the_same_secret(shared_secret) -> None:
    """The other direction of the same exchange, which the peer accepted."""
    frame = first_transmit_frame(SIGHOP_DM_INDEX)
    envelope = envelope_of(frame)

    candidate, plaintext = mac_then_decrypt(shared_secret, envelope.mac, envelope.ciphertext)

    assert candidate.matched
    assert plaintext is not None
    body = parse_text_message_body(plaintext)
    assert frame.transmitted is True, "this record is a frame sighop sent"
    assert body.timestamp == SIGHOP_MESSAGE_TIMESTAMP
    assert body.text.text == SIGHOP_MESSAGE_TEXT


# --- The negative half: the two key slices stay distinguishable ------------


def test_the_full_secret_as_the_cipher_key_does_not_recover_the_plaintext(
    shared_secret,
) -> None:
    """`Utils::encrypt` keys AES on the FIRST 16 bytes of the 32-byte secret.

    The HMAC below keys on all 32. The two slices are a documented distinction
    that a comment cannot enforce, so it is enforced by evidence: taking the
    whole secret as the cipher key must fail against real foreign ciphertext.
    """
    envelope = envelope_of(first_transmit_frame(PEER_DM_INDEX))

    decryptor = Cipher(algorithms.AES256(shared_secret), modes.ECB()).decryptor()
    wrong = decryptor.update(envelope.ciphertext) + decryptor.finalize()

    assert not wrong.startswith(PEER_MESSAGE_TIMESTAMP.to_bytes(4, "little"))
    assert PEER_MESSAGE_TEXT.encode() not in wrong


def test_the_mac_keyed_on_only_the_first_sixteen_bytes_does_not_verify(
    shared_secret,
) -> None:
    """`Utils::encryptThenMAC` passes `PUB_KEY_SIZE`, not `CIPHER_KEY_SIZE`."""
    envelope = envelope_of(first_transmit_frame(PEER_DM_INDEX))

    truncated_key_mac = hmac.new(
        shared_secret[:CIPHER_KEY_SIZE], envelope.ciphertext, hashlib.sha256
    ).digest()[:2]

    assert compute_mac(shared_secret, envelope.ciphertext) == envelope.mac
    assert truncated_key_mac != envelope.mac


# --- Acknowledgement vectors -----------------------------------------------


def test_the_peers_acknowledgement_matches_the_checksum_we_computed(
    shared_secret,
) -> None:
    """`BaseChatMesh.cpp:431` against a real peer, for a message we sent.

    The expected acknowledgement is hashed over exactly the transmitted
    plaintext — `5 + text_len` bytes, with no NUL terminator. A trailing NUL, or
    the padded buffer, would change this hash and the peer would appear never to
    acknowledge anything.
    """
    envelope = envelope_of(first_transmit_frame(SIGHOP_DM_INDEX))
    _candidate, plaintext = mac_then_decrypt(shared_secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    body = parse_text_message_body(plaintext)
    assert isinstance(body, TextMessageBody)

    computed = ack_checksum_for(body, first_transmit_identity_public_key())

    ack_frame = first_transmit_frame(PEER_ACK_INDEX)
    payload = decode(ack_frame.raw).payload
    assert payload == PEER_ACK + PEER_ACK_TAIL
    assert computed == PEER_ACK
    assert computed == payload[:4], "the peer acknowledged a different hash"


def test_the_peers_acknowledgement_is_six_bytes_and_only_four_are_compared() -> None:
    """`BaseChatMesh.cpp:245` observed on the air, not read out of the source.

    The firmware appends an extended attempt byte and a random byte to keep the
    packet hash unique. This is the first recorded instance of the 6-byte form
    arriving as an acknowledgement of something sighop sent.
    """
    payload = decode(first_transmit_frame(PEER_ACK_INDEX).raw).payload

    assert len(payload) == 6
    assert payload[:4] == PEER_ACK
    assert payload[4] == 0x00, "the extended attempt byte for attempt 0"


def test_our_acknowledgement_is_the_one_the_peer_accepted(shared_secret) -> None:
    """`BaseChatMesh.cpp:243`: the same hash, keyed with the *sender's* key.

    The peer reported `expected_ack = 1df0f21a` for its own message and marked it
    delivered on receiving this, so the receiver half of the construction is
    confirmed by a foreign implementation too.
    """
    envelope = envelope_of(first_transmit_frame(PEER_DM_INDEX))
    _candidate, plaintext = mac_then_decrypt(shared_secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    body = parse_text_message_body(plaintext)
    assert isinstance(body, TextMessageBody)

    computed = ack_checksum_for(body, PEER_PUBLIC_KEY)

    emitted = first_transmit_frame(SIGHOP_ACK_INDEX)
    payload = decode(emitted.raw).payload
    assert emitted.transmitted is True
    assert computed == SIGHOP_ACK
    assert payload == SIGHOP_ACK, "we accept 6 bytes and emit 4 (design D8)"


def first_transmit_identity_public_key() -> bytes:
    return load_keyfile(BURNED_KEYFILE).public_key
