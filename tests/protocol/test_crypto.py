"""Cryptography tests, including the fixed known-answer vectors.

Per the design's "KAT provenance" risk, a round-trip against ourselves is
self-consistent even when it is wrong. Each vector below therefore either comes
from the firmware source directly — `Identity.cpp::validatePrivateKey` embeds a
complete known-good keypair — or is recomputed inline from primitives with the
firmware line it was read from cited beside it, so a misreading is reviewable
rather than buried in an opaque constant.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from sighop.protocol.crypto import (
    CIPHER_BLOCK_SIZE,
    ChannelKey,
    MacCandidateMatch,
    SharedSecretCache,
    VerifiedAdvert,
    AdvertVerificationFailure,
    ack_checksum,
    ack_checksum_for,
    advert_signed_message,
    calc_shared_secret,
    channel_key_from_hashtag,
    shared_secret_from_scalar,
    compute_mac,
    decrypt,
    encrypt,
    encrypt_then_mac,
    mac_then_decrypt,
    sign_advert,
    verify_advert,
    verify_mac,
)
from sighop.protocol.identity import (
    RESERVED_NODE_HASHES,
    Identity,
    IdentityGenerationError,
    LocalIdentity,
    generate_identity,
)
from sighop.protocol.payloads import (
    Advert,
    NodeType,
    TextType,
    WireText,
    TextMessageBody,
    build_appdata,
)

# --- Known-answer material -------------------------------------------------

# `Identity.cpp::validatePrivateKey` embeds this keypair verbatim as its
# "known good test client keypair". It is firmware-authored, not generated
# here, which is what makes it usable as an independent vector.
FIRMWARE_TEST_PRV = bytes.fromhex(
    "7065e18fd9fabb70c1ed90dca19907de698c88b709ea146eafd93d9b830c7b60"
    "c4681193c79bbc39945ba8064104bb618f8fd7a84a0af6f57033d6e8ddcd6471"
)
FIRMWARE_TEST_PUB = bytes.fromhex(
    "1ec77175b0918ed206f9ae04ec136d6d5d4315bb26305427f645b492e9350c10"
)

ALICE = LocalIdentity.from_seed(bytes(range(32)))
BOB = LocalIdentity.from_seed(bytes(range(100, 132)))


# --- Identity --------------------------------------------------------------


def test_node_hash_is_the_first_byte_of_the_public_key() -> None:
    assert Identity(FIRMWARE_TEST_PUB).node_hash == FIRMWARE_TEST_PUB[0] == 0x1E
    assert ALICE.node_hash == ALICE.public_key[0]


def test_meshcore_private_key_is_the_clamped_sha512_expansion() -> None:
    """`ed25519_create_keypair`: sha512(seed), then clamp."""
    expanded = bytearray(hashlib.sha512(ALICE.seed).digest())
    expanded[0] &= 248
    expanded[31] &= 63
    expanded[31] |= 64
    assert ALICE.meshcore_private_key == bytes(expanded)
    assert ALICE.private_scalar == bytes(expanded)[:32]


def test_the_firmware_test_scalar_is_already_clamped() -> None:
    """The reason derivation must not re-hash or re-clamp: it is done already."""
    scalar = FIRMWARE_TEST_PRV[:32]
    assert scalar[0] & 0b111 == 0
    assert scalar[31] & 0b1000_0000 == 0
    assert scalar[31] & 0b0100_0000 == 0b0100_0000


def test_keypair_generation_avoids_a_local_hash_collision() -> None:
    taken = {ALICE.node_hash, BOB.node_hash}
    for _ in range(20):
        assert generate_identity(avoid_node_hashes=taken).node_hash not in taken


def test_keypair_generation_avoids_the_reserved_prefixes() -> None:
    """`validatePrivateKey` rejects 0x00 and 0xFF public key prefixes."""
    for _ in range(50):
        assert generate_identity().node_hash not in RESERVED_NODE_HASHES


def test_keypair_generation_fails_cleanly_when_every_hash_is_taken() -> None:
    with pytest.raises(IdentityGenerationError, match="every value"):
        generate_identity(avoid_node_hashes=frozenset(range(256)), max_attempts=32)


def test_sign_and_verify_round_trip() -> None:
    signature = ALICE.sign(b"a message")
    assert ALICE.identity.verify(signature, b"a message")
    assert not ALICE.identity.verify(signature, b"a different message")
    assert not BOB.identity.verify(signature, b"a message")


def test_verify_rejects_a_wrong_length_signature() -> None:
    assert not ALICE.identity.verify(b"\x00" * 63, b"m")


# --- Shared secrets --------------------------------------------------------


def test_both_parties_derive_the_same_secret() -> None:
    secret = calc_shared_secret(ALICE, BOB.public_key)
    assert secret == calc_shared_secret(BOB, ALICE.public_key)
    assert len(secret) == 32


def test_firmware_test_public_key_derives_from_its_private_scalar() -> None:
    """KAT, firmware-authored. `Identity.cpp::validatePrivateKey` embeds a
    matched private/public pair, and `ed25519_derive_pub` obtains the public
    key by scalar-multiplying the base point with the stored scalar directly.
    Reproducing that pairing proves we treat `prv_key[0:32]` the way MeshCore
    does — pre-clamped, never re-hashed — against key material we did not
    generate.
    """
    from nacl import bindings

    derived = bindings.crypto_scalarmult_ed25519_base_noclamp(FIRMWARE_TEST_PRV[:32])
    assert derived == FIRMWARE_TEST_PUB


def test_edwards_to_montgomery_matches_the_formula_in_key_exchange_c() -> None:
    """KAT by independent reimplementation. `key_exchange.c` states the
    conversion it performs:

        montgomeryX = (edwardsY + 1) * inverse(1 - edwardsY) mod p

    Recomputing it here with plain modular arithmetic checks PyNaCl's
    `crypto_sign_ed25519_pk_to_curve25519` against the firmware's own stated
    construction rather than against itself.
    """
    from nacl import bindings

    p = 2**255 - 19
    for public_key in (FIRMWARE_TEST_PUB, ALICE.public_key, BOB.public_key):
        y = int.from_bytes(public_key, "little") & ((1 << 255) - 1)
        expected = (1 + y) * pow(1 - y, -1, p) % p
        actual = int.from_bytes(
            bindings.crypto_sign_ed25519_pk_to_curve25519(public_key), "little"
        )
        assert actual == expected


def test_shared_secret_agrees_with_the_firmware_key_material_both_ways() -> None:
    """`validatePrivateKey` asserts that a secret derived from (its embedded
    private key, our public key) equals one derived from (our private key, its
    embedded public key). The firmware's expanded 64-byte key has no
    recoverable seed, so the firmware side goes through
    `shared_secret_from_scalar` — the same path a MeshCore-exported key takes.
    """
    theirs = shared_secret_from_scalar(FIRMWARE_TEST_PRV[:32], ALICE.public_key)
    ours = calc_shared_secret(ALICE, FIRMWARE_TEST_PUB)
    assert theirs == ours
    assert theirs != bytes(32)  # validatePrivateKey rejects an all-zero secret


def test_shared_secret_for_the_firmware_keypair_is_pinned() -> None:
    """Regression pin over the vector above, so a change in the derivation path
    shows up as a failing constant rather than as a still-symmetric wrong answer.
    """
    secret = shared_secret_from_scalar(FIRMWARE_TEST_PRV[:32], FIRMWARE_TEST_PUB)
    assert secret.hex() == (
        "b981cf37cd88bb0728e3a30f51bd12d26ba27df6e2ba06179fd8bca83efe286d"
    )


def test_shared_secret_is_not_the_all_zero_secret() -> None:
    """`validatePrivateKey` rejects an all-zero shared secret outright."""
    assert calc_shared_secret(ALICE, BOB.public_key) != bytes(32)


def test_shared_secret_cache_multiplies_once_per_peer() -> None:
    cache = SharedSecretCache()
    first = cache.get(ALICE, BOB.public_key)
    second = cache.get(ALICE, BOB.public_key)
    assert first == second
    assert (cache.hits, cache.misses) == (1, 1)
    assert len(cache) == 1


def test_shared_secret_cache_keys_on_the_local_entity_too() -> None:
    cache = SharedSecretCache()
    cache.get(ALICE, BOB.public_key)
    cache.get(BOB, ALICE.public_key)
    assert len(cache) == 2
    assert cache.misses == 2


def test_shared_secret_rejects_a_wrong_length_peer_key() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        calc_shared_secret(ALICE, bytes(31))


# --- Cipher ----------------------------------------------------------------


def test_encrypt_decrypt_round_trip_pads_with_zeros() -> None:
    secret = calc_shared_secret(ALICE, BOB.public_key)
    ciphertext = encrypt(secret, b"hello")
    assert len(ciphertext) == CIPHER_BLOCK_SIZE
    assert decrypt(secret, ciphertext) == b"hello" + bytes(11)


def test_plaintext_exactly_filling_a_block_gains_no_extra_block() -> None:
    """Zero padding, never PKCS#7: a full block gets no trailing block."""
    secret = calc_shared_secret(ALICE, BOB.public_key)
    plaintext = bytes(range(16))
    ciphertext = encrypt(secret, plaintext)
    assert len(ciphertext) == 16
    assert decrypt(secret, ciphertext) == plaintext


def test_decrypt_returns_the_padded_buffer_without_guessing() -> None:
    secret = calc_shared_secret(ALICE, BOB.public_key)
    decrypted = decrypt(secret, encrypt(secret, b"abc"))
    assert len(decrypted) == CIPHER_BLOCK_SIZE
    assert decrypted.endswith(bytes(13))


def test_cipher_key_is_the_first_sixteen_bytes_of_the_secret() -> None:
    """KAT. `Utils::encrypt` calls `aes.setKey(shared_secret, CIPHER_KEY_SIZE)`,
    so a secret differing only after byte 16 encrypts identically.
    """
    secret = calc_shared_secret(ALICE, BOB.public_key)
    altered = secret[:16] + bytes(16)
    assert encrypt(secret, b"same") == encrypt(altered, b"same")


def test_aes_128_ecb_matches_a_fixed_vector() -> None:
    """KAT. AES-128-ECB of an all-zero block under the FIPS-197 sample key."""
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    assert encrypt(key, bytes(16)).hex() == "c6a13b37878f5b826f4f8162a1c8d879"


@pytest.mark.parametrize("length", [0, 15, 17, 31])
def test_decrypt_rejects_ciphertext_that_is_not_block_aligned(length: int) -> None:
    with pytest.raises(ValueError, match="positive multiple"):
        decrypt(bytes(32), bytes(length))


# --- MAC -------------------------------------------------------------------


def test_mac_is_hmac_over_the_ciphertext_keyed_on_the_full_secret() -> None:
    """KAT. `Utils::encryptThenMAC` HMACs `dest + CIPHER_MAC_SIZE` (the
    ciphertext) with `PUB_KEY_SIZE` (32) key bytes, then keeps 2.
    """
    secret = calc_shared_secret(ALICE, BOB.public_key)
    ciphertext = encrypt(secret, b"payload")
    expected = hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2]
    assert compute_mac(secret, ciphertext) == expected
    assert len(expected) == 2


def test_mac_key_is_the_full_secret_not_the_cipher_key() -> None:
    """The two keys are different slices: changing bytes 16-31 changes the MAC
    but not the ciphertext.
    """
    secret = calc_shared_secret(ALICE, BOB.public_key)
    altered = secret[:16] + bytes(16)
    ciphertext = encrypt(secret, b"payload")
    assert compute_mac(secret, ciphertext) != compute_mac(altered, ciphertext)


def test_mac_verifies_for_a_correctly_keyed_ciphertext() -> None:
    secret = calc_shared_secret(ALICE, BOB.public_key)
    mac, ciphertext = encrypt_then_mac(secret, b"hello there")
    candidate, plaintext = mac_then_decrypt(secret, mac, ciphertext)
    assert isinstance(candidate, MacCandidateMatch)
    assert candidate.matched
    assert plaintext is not None
    assert plaintext.startswith(b"hello there")


def test_mac_fails_for_a_wrong_key_and_nothing_is_decrypted() -> None:
    secret = calc_shared_secret(ALICE, BOB.public_key)
    wrong = calc_shared_secret(ALICE, generate_identity().public_key)
    mac, ciphertext = encrypt_then_mac(secret, b"hello there")
    candidate, plaintext = mac_then_decrypt(wrong, mac, ciphertext)
    assert not candidate.matched
    assert plaintext is None


def test_mac_comparison_uses_a_constant_time_primitive() -> None:
    """`verify_mac` must route through `hmac.compare_digest`, not `==`."""
    import inspect

    from sighop.protocol import crypto

    assert "hmac.compare_digest" in inspect.getsource(crypto.verify_mac)


def test_mac_candidate_reports_both_values_for_logging() -> None:
    secret = calc_shared_secret(ALICE, BOB.public_key)
    ciphertext = encrypt(secret, b"x")
    result = verify_mac(secret, ciphertext, b"\x00\x00")
    assert result.observed == b"\x00\x00"
    assert result.expected == compute_mac(secret, ciphertext)


# --- Channel keys ----------------------------------------------------------


def test_hashtag_derived_key_is_sha256_of_the_hashtag() -> None:
    """KAT. `TransportKeyStore.cpp:44-47` hashes the name with its leading `#`."""
    channel = channel_key_from_hashtag("#roomname")
    assert channel.key == hashlib.sha256(b"#roomname").digest()[:16]
    assert channel_key_from_hashtag("roomname").key == channel.key


def test_hashtag_derived_keys_are_flagged_as_weak() -> None:
    assert channel_key_from_hashtag("#public").is_brute_forceable
    assert not ChannelKey(key=bytes(range(16))).is_brute_forceable


def test_channel_hash_is_the_first_byte_of_sha256_of_the_key() -> None:
    """KAT. `BaseChatMesh.cpp:907-910` hashes 16 or 32 bytes — the key's real
    length, not the zero-extended 32-byte buffer.
    """
    key16 = bytes(range(16))
    assert ChannelKey(key=key16).channel_hash == hashlib.sha256(key16).digest()[0]
    key32 = bytes(range(32))
    assert ChannelKey(key=key32).channel_hash == hashlib.sha256(key32).digest()[0]


def test_short_channel_key_is_zero_extended_for_the_secret() -> None:
    """`GroupChannel.secret` is a 32-byte buffer memset to zero first."""
    channel = ChannelKey(key=bytes(range(16)))
    assert channel.secret == bytes(range(16)) + bytes(16)
    assert len(channel.secret) == 32


@pytest.mark.parametrize("length", [0, 8, 15, 24, 33])
def test_channel_key_rejects_other_lengths(length: int) -> None:
    with pytest.raises(ValueError, match="16 or 32 bytes"):
        ChannelKey(key=bytes(length))


def test_group_payload_round_trips_through_the_channel_secret() -> None:
    channel = channel_key_from_hashtag("#public")
    mac, ciphertext = encrypt_then_mac(channel.secret, b"alice: hi")
    candidate, plaintext = mac_then_decrypt(channel.secret, mac, ciphertext)
    assert candidate.matched
    assert plaintext is not None and plaintext.startswith(b"alice: hi")


# --- Adverts ---------------------------------------------------------------


def make_signed_advert(local: LocalIdentity = ALICE) -> Advert:
    appdata = build_appdata(NodeType.CHAT, name="alice")
    return sign_advert(local, 1_756_000_000, appdata)


def test_advert_signed_message_is_pubkey_timestamp_appdata() -> None:
    """`Mesh.cpp::createAdvert` signs exactly this concatenation, in this order."""
    assert advert_signed_message(ALICE.public_key, 0x01020304, b"ad") == (
        ALICE.public_key + b"\x04\x03\x02\x01" + b"ad"
    )


def test_sign_and_verify_advert_round_trip() -> None:
    result = verify_advert(make_signed_advert())
    assert isinstance(result, VerifiedAdvert)
    assert result.identity == ALICE.identity
    assert result.timestamp == 1_756_000_000
    assert result.appdata.node_type is NodeType.CHAT
    assert result.appdata.name is not None
    assert result.appdata.name.text == "alice"


def test_verified_advert_carries_its_timestamp_for_replay_comparison() -> None:
    result = verify_advert(make_signed_advert())
    assert isinstance(result, VerifiedAdvert)
    assert result.timestamp == 1_756_000_000
    assert result.node_hash == ALICE.node_hash


def test_tampered_advert_appdata_fails_verification() -> None:
    advert = make_signed_advert()
    tampered = Advert(
        public_key=advert.public_key,
        timestamp=advert.timestamp,
        signature=advert.signature,
        appdata=advert.appdata[:-1] + bytes([advert.appdata[-1] ^ 0x01]),
    )
    result = verify_advert(tampered)
    assert isinstance(result, AdvertVerificationFailure)
    assert result.reason == "bad_signature"


def test_tampered_advert_timestamp_fails_verification() -> None:
    advert = make_signed_advert()
    tampered = Advert(
        public_key=advert.public_key,
        timestamp=advert.timestamp + 1,
        signature=advert.signature,
        appdata=advert.appdata,
    )
    assert isinstance(verify_advert(tampered), AdvertVerificationFailure)


def test_advert_signed_by_another_identity_fails_verification() -> None:
    advert = make_signed_advert()
    impostor = Advert(
        public_key=BOB.public_key,
        timestamp=advert.timestamp,
        signature=advert.signature,
        appdata=advert.appdata,
    )
    assert isinstance(verify_advert(impostor), AdvertVerificationFailure)


def test_verification_failure_carries_no_advert_content() -> None:
    """A failed verification is a discard: there is nothing here to display."""
    advert = make_signed_advert()
    tampered = Advert(
        public_key=advert.public_key,
        timestamp=advert.timestamp + 1,
        signature=advert.signature,
        appdata=advert.appdata,
    )
    failure = verify_advert(tampered)
    assert isinstance(failure, AdvertVerificationFailure)
    assert not hasattr(failure, "name")
    assert not hasattr(failure, "appdata")


def test_advert_with_unparseable_appdata_fails_verification() -> None:
    advert = sign_advert(ALICE, 1, bytes([0x91]) + bytes(4))  # location truncated
    result = verify_advert(advert)
    assert isinstance(result, AdvertVerificationFailure)
    assert result.reason == "bad_appdata"


# --- Acknowledgements ------------------------------------------------------


def test_ack_checksum_matches_the_firmware_construction() -> None:
    """KAT. `BaseChatMesh.cpp:243`:

        Utils::sha256(ack_hash, 4, data, 5 + text_len, from.id.pub_key, PUB_KEY_SIZE)

    — the first 4 bytes of SHA-256 over (body prefix || sender pubkey). Note it
    is a hash, not the CRC32 the payload documentation describes; a CRC32
    implementation would never produce an acknowledgement any node accepts.
    """
    import zlib

    body_prefix = (1_700_000_000).to_bytes(4, "little") + b"\x00" + b"hello"
    expected = hashlib.sha256(body_prefix + ALICE.public_key).digest()[:4]
    assert ack_checksum(body_prefix, ALICE.public_key) == expected
    # And it is emphatically not the CRC32 the payload documentation describes.
    crc = zlib.crc32(body_prefix + ALICE.public_key).to_bytes(4, "little")
    assert ack_checksum(body_prefix, ALICE.public_key) != crc


def test_sender_and_receiver_compute_the_same_acknowledgement() -> None:
    message = TextMessageBody(
        timestamp=1_700_000_000,
        txt_type=TextType.PLAIN,
        attempt=0,
        text=WireText.from_bytes(b"on my way"),
    )
    sender_view = ack_checksum_for(message, ALICE.public_key)
    receiver_view = ack_checksum(
        (1_700_000_000).to_bytes(4, "little") + b"\x00" + b"on my way",
        ALICE.public_key,
    )
    assert sender_view == receiver_view
    assert len(sender_view) == 4


def test_ack_checksum_covers_the_attempt_byte() -> None:
    base = TextMessageBody(
        timestamp=1,
        txt_type=TextType.PLAIN,
        attempt=0,
        text=WireText.from_bytes(b"x"),
    )
    retry = TextMessageBody(
        timestamp=1,
        txt_type=TextType.PLAIN,
        attempt=1,
        text=WireText.from_bytes(b"x"),
    )
    assert ack_checksum_for(base, ALICE.public_key) != ack_checksum_for(
        retry, ALICE.public_key
    )


def test_ack_checksum_rejects_a_wrong_length_public_key() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        ack_checksum(b"prefix", bytes(31))
