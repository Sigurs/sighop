"""MeshCore's wire cryptography, reproduced exactly.

Reference: `related-repos/MeshCore/src/Utils.cpp`, `src/Identity.cpp`,
`src/helpers/BaseChatMesh.cpp`, `src/Mesh.h` and `src/MeshCore.h`. The
published payload documentation does not describe the cryptography; the
firmware source is what interoperates.

**These constructions are weak.** AES-128-ECB with no nonce and no integrity
beyond a 2-byte MAC; acknowledgements that are unkeyed truncated hashes;
1-byte destination hashes. Reproducing them exactly is a hard interoperability
requirement, not an endorsement. The types here are shaped so the rest of the
system cannot quietly trust them more than they deserve (design D7):
`MacCandidateMatch` is a candidate, not an authentication; `VerifiedAdvert` is
the only route to trustworthy advert content; a hashtag-derived channel key
carries its own weakness flag.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from nacl import bindings

from sighop.protocol.identity import Identity, LocalIdentity
from sighop.protocol.payloads import (
    Advert,
    AdvertAppData,
    TextMessageBody,
    parse_appdata,
)
from sighop.protocol.result import DecodeFailure

# MeshCore.h
CIPHER_KEY_SIZE = 16
CIPHER_BLOCK_SIZE = 16
CIPHER_MAC_SIZE = 2
PUB_KEY_SIZE = 32
SHARED_SECRET_SIZE = 32

ACK_CHECKSUM_SIZE = 4
HASHTAG_KEY_SIZE = 16


# --- Shared secrets --------------------------------------------------------


def shared_secret_from_scalar(private_scalar: bytes, peer_public_key: bytes) -> bytes:
    """`ed25519_key_exchange(secret, peer_public_key, private_scalar)`.

    Converts the peer's Ed25519 public key to its Montgomery form and runs the
    X25519 ladder with the given pre-clamped scalar. libsodium's
    `crypto_scalarmult` re-applies clamping, which is a no-op on an
    already-clamped scalar — the two implementations' clamp masks differ in
    spelling (`&= 63 | 64` against `&= 127 | 64`) but agree in effect.

    Takes the raw scalar so a private key imported from MeshCore's 64-byte
    `prv_key`, whose seed is unrecoverable, can still derive secrets.
    """
    if len(private_scalar) != PUB_KEY_SIZE:
        raise ValueError(f"private scalar must be {PUB_KEY_SIZE} bytes")
    if len(peer_public_key) != PUB_KEY_SIZE:
        raise ValueError(f"peer public key must be {PUB_KEY_SIZE} bytes")
    montgomery = bindings.crypto_sign_ed25519_pk_to_curve25519(peer_public_key)
    return bindings.crypto_scalarmult(private_scalar, montgomery)


def calc_shared_secret(local: LocalIdentity, peer_public_key: bytes) -> bytes:
    """Derive the 32-byte shared secret `LocalIdentity::calcSharedSecret` produces."""
    return shared_secret_from_scalar(local.private_scalar, peer_public_key)


@dataclass(slots=True)
class SharedSecretCache:
    """Per-(local entity, peer) shared secret cache (DESIGN.md §5).

    Scalar multiplication is the expensive step in the receive path, and the
    RX pipeline repeats it for every packet from a known peer. Mutable by
    design — it is a cache, not a decoded value.
    """

    _secrets: dict[tuple[bytes, bytes], bytes] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, local: LocalIdentity, peer_public_key: bytes) -> bytes:
        key = (local.public_key, peer_public_key)
        cached = self._secrets.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        secret = calc_shared_secret(local, peer_public_key)
        self._secrets[key] = secret
        return secret

    def clear(self) -> None:
        self._secrets.clear()

    def __len__(self) -> int:
        return len(self._secrets)


# --- Cipher ----------------------------------------------------------------


def _cipher(secret: bytes) -> Cipher:
    if len(secret) < CIPHER_KEY_SIZE:
        raise ValueError(f"shared secret must be at least {CIPHER_KEY_SIZE} bytes")
    # `Utils::encrypt` keys AES on the FIRST 16 bytes of the 32-byte secret,
    # while the HMAC below keys on all 32. Different slices, same secret.
    return Cipher(algorithms.AES128(secret[:CIPHER_KEY_SIZE]), modes.ECB())


def encrypt(secret: bytes, plaintext: bytes) -> bytes:
    """AES-128-ECB with zero padding to the next block — never PKCS#7.

    A plaintext that already fills a whole number of blocks gets no extra
    block, so the padding is not removable by inspection. That ambiguity is
    resolved by the body parsers (design D6), not here.
    """
    remainder = len(plaintext) % CIPHER_BLOCK_SIZE
    if remainder:
        plaintext = plaintext + bytes(CIPHER_BLOCK_SIZE - remainder)
    encryptor = _cipher(secret).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def decrypt(secret: bytes, ciphertext: bytes) -> bytes:
    """Decrypt, returning the padded buffer verbatim.

    The original plaintext length is not recoverable from the ciphertext, and
    a plaintext that genuinely ended in zeros is indistinguishable from one
    that was padded. This layer therefore does not guess.
    """
    if not ciphertext or len(ciphertext) % CIPHER_BLOCK_SIZE:
        raise ValueError(
            f"ciphertext of {len(ciphertext)} bytes is not a positive multiple of "
            f"{CIPHER_BLOCK_SIZE}"
        )
    decryptor = _cipher(secret).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


# --- MAC -------------------------------------------------------------------


def compute_mac(secret: bytes, ciphertext: bytes) -> bytes:
    """The 2-byte cipher MAC: HMAC-SHA256 over the ciphertext, keyed on the
    FULL 32-byte secret (`Utils::encryptThenMAC` passes `PUB_KEY_SIZE`, where
    `encrypt()` passes `CIPHER_KEY_SIZE`), truncated to its first 2 bytes.
    """
    return hmac.new(secret, ciphertext, hashlib.sha256).digest()[:CIPHER_MAC_SIZE]


@dataclass(frozen=True, slots=True)
class MacCandidateMatch:
    """The outcome of checking a 2-byte MAC — a candidate, not an authentication.

    A 2-byte MAC gives roughly a 1-in-2^16 false match per candidate key, and
    the 1-byte destination hash that selected the candidate collides at 1 in
    256. A true `matched` means "this key decrypts this payload", which is
    useful for routing a packet to the right local entity and is *not* proof of
    who sent it. Nothing downstream may treat it as sender authentication.
    """

    matched: bool
    expected: bytes
    observed: bytes


def verify_mac(secret: bytes, ciphertext: bytes, mac: bytes) -> MacCandidateMatch:
    """Check a payload's MAC in constant time."""
    expected = compute_mac(secret, ciphertext)
    return MacCandidateMatch(
        matched=hmac.compare_digest(expected, mac),
        expected=expected,
        observed=mac,
    )


def encrypt_then_mac(secret: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt, then MAC the ciphertext (`Utils::encryptThenMAC`).

    Returns `(mac, ciphertext)` in the order the wire carries them.
    """
    ciphertext = encrypt(secret, plaintext)
    return compute_mac(secret, ciphertext), ciphertext


def mac_then_decrypt(
    secret: bytes, mac: bytes, ciphertext: bytes
) -> tuple[MacCandidateMatch, bytes | None]:
    """Verify the MAC and decrypt only if it matched (`Utils::MACThenDecrypt`).

    A MAC failure is the normal outcome for traffic addressed to someone else,
    not an error: it returns the unmatched candidate and no plaintext.
    """
    candidate = verify_mac(secret, ciphertext, mac)
    if not candidate.matched:
        return candidate, None
    return candidate, decrypt(secret, ciphertext)


# --- Channel keys ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelKey:
    """A group channel's pre-shared key, 16 or 32 bytes.

    `secret` is the 32-byte buffer MeshCore actually keys with: `GroupChannel`
    holds `uint8_t secret[PUB_KEY_SIZE]` zero-filled before a shorter key is
    loaded into it (`BaseChatMesh.cpp:884-885`), and the whole buffer is what
    `encryptThenMAC` HMACs. The channel *hash*, by contrast, is taken over the
    key's real length (`BaseChatMesh.cpp:907-910`), so the two must not be
    conflated.
    """

    key: bytes
    is_brute_forceable: bool = False

    def __post_init__(self) -> None:
        if len(self.key) not in (16, 32):
            raise ValueError(
                f"channel key must be 16 or 32 bytes, got {len(self.key)}"
            )

    @property
    def secret(self) -> bytes:
        """The zero-extended 32-byte buffer used as the shared secret."""
        return self.key.ljust(SHARED_SECRET_SIZE, b"\x00")

    @property
    def channel_hash(self) -> int:
        """First byte of SHA-256 over the key at its real length."""
        return hashlib.sha256(self.key).digest()[0]


def channel_key_from_hashtag(hashtag: str) -> ChannelKey:
    """Derive a channel key from a public hashtag name, e.g. `#public`.

    The `#` is part of the hashed bytes (`TransportKeyStore.cpp:44-47`, and
    `MyMesh.cpp:930` which passes `"#" DEFAULT_FLOOD_SCOPE_NAME`).

    The result is flagged brute-forceable and it means it: the input is a short
    public string, so anyone can derive the same key from a guessed name. It
    provides addressing, not confidentiality, and an operator must be told so.
    """
    name = hashtag if hashtag.startswith("#") else f"#{hashtag}"
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return ChannelKey(key=digest[:HASHTAG_KEY_SIZE], is_brute_forceable=True)


PUBLIC_CHANNEL_KEY = ChannelKey(key=base64.b64decode("izOH6cXN6mrJ5e26oRXNcg=="))
"""The stock MeshCore Public channel's pre-shared key; its channel hash is `0x11`.

Not flagged brute-forceable, because nobody has to guess it: it ships in every
stock client and is therefore exactly as private as a hashtag. It is a
pre-shared key only in the sense of its shape.
"""


# --- Adverts ---------------------------------------------------------------


def advert_signed_message(
    public_key: bytes, timestamp: int, appdata: bytes
) -> bytes:
    """The bytes an advert signature covers (`Mesh.cpp::createAdvert`)."""
    return public_key + timestamp.to_bytes(4, "little") + appdata


def sign_advert(
    local: LocalIdentity, timestamp: int, appdata: bytes
) -> Advert:
    """Build and sign an advert for `local`."""
    message = advert_signed_message(local.public_key, timestamp, appdata)
    return Advert(
        public_key=local.public_key,
        timestamp=timestamp,
        signature=local.sign(message),
        appdata=appdata,
    )


@dataclass(frozen=True, slots=True)
class VerifiedAdvert:
    """An advert whose signature verified, with its appdata parsed.

    This type is the only thing that produces parsed advert content — name,
    node type, location — and it exists only on the far side of a successful
    signature check (design D7). A consumer three milestones from here cannot
    display a node name without having unwrapped one of these.

    `timestamp` is carried so the layer that stores contacts can reject an
    advert not newer than the last seen from this identity, matching the
    firmware's replay check. This layer is stateless and does not do that
    comparison itself.
    """

    identity: Identity
    timestamp: int
    appdata: AdvertAppData

    @property
    def public_key(self) -> bytes:
        return self.identity.public_key

    @property
    def node_hash(self) -> int:
        return self.identity.node_hash


@dataclass(frozen=True, slots=True)
class AdvertVerificationFailure:
    """A failed advert verification. The contract is discard, not warn.

    Deliberately carries no advert content: an unverified advert's name and
    location are attacker-chosen, so there is nothing here to accidentally
    display.
    """

    reason: str
    node_hash: int
    detail: str = ""


type AdvertVerification = VerifiedAdvert | AdvertVerificationFailure


def verify_advert(advert: Advert) -> AdvertVerification:
    """Verify an advert's Ed25519 signature and parse its appdata.

    Verification uses the public key the advert carries, which proves only that
    whoever emitted it holds that key — it says nothing about whether that key
    belongs to anyone you trust.
    """
    identity = Identity(public_key=advert.public_key)
    message = advert_signed_message(
        advert.public_key, advert.timestamp, advert.appdata
    )
    if not identity.verify(advert.signature, message):
        return AdvertVerificationFailure(
            reason="bad_signature",
            node_hash=advert.node_hash,
            detail="Ed25519 signature does not verify against the advertised key",
        )
    appdata = parse_appdata(advert.appdata)
    if isinstance(appdata, DecodeFailure):
        return AdvertVerificationFailure(
            reason="bad_appdata",
            node_hash=advert.node_hash,
            detail=str(appdata),
        )
    return VerifiedAdvert(
        identity=identity, timestamp=advert.timestamp, appdata=appdata
    )


# --- Acknowledgements ------------------------------------------------------


def ack_checksum(body_prefix: bytes, sender_public_key: bytes) -> bytes:
    """The 4-byte acknowledgement checksum — truncated SHA-256, **not CRC32**.

    `BaseChatMesh.cpp:243`:

        Utils::sha256(ack_hash, 4, data, 5 + text_len, from.id.pub_key, PUB_KEY_SIZE)

    i.e. the first 4 bytes of SHA-256 over the decrypted body prefix (4-byte
    timestamp, the `txt_type`/attempt byte, and the unpadded message text)
    concatenated with the *sender's* public key.

    A match is delivery evidence only. The value is an unkeyed hash over data
    any observer of the plaintext could reproduce, so it authenticates nobody.
    """
    if len(sender_public_key) != PUB_KEY_SIZE:
        raise ValueError(f"sender public key must be {PUB_KEY_SIZE} bytes")
    return hashlib.sha256(body_prefix + sender_public_key).digest()[:ACK_CHECKSUM_SIZE]


def ack_checksum_for(message: TextMessageBody, sender_public_key: bytes) -> bytes:
    """The expected acknowledgement for a parsed text message body."""
    return ack_checksum(message.ack_prefix, sender_public_key)
