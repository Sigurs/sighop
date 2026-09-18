"""Ed25519 identities and node hashes, in MeshCore's key representation.

MeshCore stores a 64-byte private key that is the SHA-512 expansion of a
32-byte seed with the standard Ed25519 clamping already applied
(`lib/ed25519/keypair.c::ed25519_create_keypair`), and derives the public key
by scalar-multiplying the base point with the first 32 bytes of that buffer —
never re-hashing or re-clamping it (`Identity.cpp::LocalIdentity`). The shared
secret derivation in `crypto.py` uses that pre-clamped scalar directly, which
is the single fiddliest detail in the milestone.

That 64-byte buffer is what a `LocalIdentity` holds, because it is the only
representation an operator can bring in from elsewhere: a device exports it,
and the seed it came from is behind a SHA-512 nobody can walk backwards. It is
also sufficient — the clamped scalar is its first half and the signing nonce
prefix its second, which is everything RFC 8032 consumes, so `sign` below needs
no seed. A seed remains a way to *produce* a keypair, which is what
`generate_identity` uses it for and why `from_seed` expands one and lets it go.

A node hash is the first byte of the public key, so it collides at 1 in 256:
it identifies a candidate, never a node.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Container
from dataclasses import dataclass

from nacl import bindings
from nacl.exceptions import BadSignatureError

PUB_KEY_SIZE = 32
PRV_KEY_SIZE = 64
SEED_SIZE = 32
SIGNATURE_SIZE = 64

# `Identity.cpp::validatePrivateKey` rejects these public key prefixes outright,
# so a key carrying one is useless for interoperating even though it is a valid
# Ed25519 key.
RESERVED_NODE_HASHES = frozenset({0x00, 0xFF})

DEFAULT_MAX_GENERATION_ATTEMPTS = 1024


class IdentityGenerationError(RuntimeError):
    """Keypair generation could not find an acceptable node hash in time."""


class PrivateKeyError(ValueError):
    """A supplied private key this system will not build an identity from.

    One type for every reason, because every caller does the same thing with
    it: print it and create nothing. The *message* is what differs, and the
    command line and the panel both show the one raised here rather than each
    writing their own (design D6).
    """


def _expand_seed(seed: bytes) -> bytes:
    """Seed to MeshCore's 64-byte private key (`ed25519_create_keypair`)."""
    expanded = bytearray(hashlib.sha512(seed).digest())
    expanded[0] &= 248
    expanded[31] &= 63
    expanded[31] |= 64
    return bytes(expanded)


@dataclass(frozen=True, slots=True)
class Identity:
    """A peer identity: a public key and nothing secret."""

    public_key: bytes

    def __post_init__(self) -> None:
        if len(self.public_key) != PUB_KEY_SIZE:
            raise ValueError(f"public key must be {PUB_KEY_SIZE} bytes")

    @property
    def node_hash(self) -> int:
        """The first byte of the public key — a 1-in-256 identifier, not an identity."""
        return self.public_key[0]

    def verify(self, signature: bytes, message: bytes) -> bool:
        if len(signature) != SIGNATURE_SIZE:
            return False
        try:
            bindings.crypto_sign_open(signature + message, self.public_key)
        except BadSignatureError:
            return False
        return True


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    """A local entity's keypair, held as the 64-byte key MeshCore stores.

    No seed. The private key is `prv_key` exactly as the firmware exports it, so
    an identity that came from a device and one generated here are the same
    thing, and nothing in the system has to ask which it is holding.
    """

    private_key: bytes
    public_key: bytes

    def __post_init__(self) -> None:
        if len(self.private_key) != PRV_KEY_SIZE:
            raise ValueError(f"private key must be {PRV_KEY_SIZE} bytes")
        if len(self.public_key) != PUB_KEY_SIZE:
            raise ValueError(f"public key must be {PUB_KEY_SIZE} bytes")

    @classmethod
    def from_seed(cls, seed: bytes) -> LocalIdentity:
        """Expand a seed into a keypair and keep only the expansion.

        `generate_identity` makes keys this way because a seed is how you roll
        one. The seed is not returned and not stored: see the module docstring.
        """
        if len(seed) != SEED_SIZE:
            raise ValueError(f"seed must be {SEED_SIZE} bytes")
        return cls.from_private_key(_expand_seed(seed))

    @classmethod
    def from_private_key(cls, private_key: bytes) -> LocalIdentity:
        """A keypair from MeshCore's 64-byte `prv_key`, public key derived.

        The public key is never taken as an input beside the private one. Two
        values that can disagree mean deciding which to believe, and
        `Identity.cpp` does not: it derives.
        """
        if len(private_key) != PRV_KEY_SIZE:
            raise ValueError(f"private key must be {PRV_KEY_SIZE} bytes")
        public_key = bindings.crypto_scalarmult_ed25519_base_noclamp(private_key[:PUB_KEY_SIZE])
        return cls(private_key=private_key, public_key=public_key)

    @property
    def identity(self) -> Identity:
        return Identity(public_key=self.public_key)

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

    @property
    def meshcore_private_key(self) -> bytes:
        """The 64-byte `prv_key` MeshCore stores and exports."""
        return self.private_key

    @property
    def private_scalar(self) -> bytes:
        """The pre-clamped 32-byte scalar `calcSharedSecret` multiplies with."""
        return self.private_key[:PUB_KEY_SIZE]

    def sign(self, message: bytes) -> bytes:
        """Sign `message`, producing a signature MeshCore's `Identity::verify` accepts.

        RFC 8032 signing written out, because libsodium's `crypto_sign` takes a
        seed and we do not keep one. The two halves of `prv_key` are precisely
        what the construction needs — the clamped scalar `a`, and the prefix the
        nonce is derived from — so this is the standard algorithm on standard
        primitives, not a variant. `tests/protocol/test_crypto.py` pins it to a
        firmware keypair and to `crypto_sign` itself for every key with a seed.
        """
        scalar, prefix = self.private_key[:PUB_KEY_SIZE], self.private_key[PUB_KEY_SIZE:]
        nonce = bindings.crypto_core_ed25519_scalar_reduce(
            hashlib.sha512(prefix + message).digest()
        )
        commitment = bindings.crypto_scalarmult_ed25519_base_noclamp(nonce)
        challenge = bindings.crypto_core_ed25519_scalar_reduce(
            hashlib.sha512(commitment + self.public_key + message).digest()
        )
        signature_scalar = bindings.crypto_core_ed25519_scalar_add(
            nonce, bindings.crypto_core_ed25519_scalar_mul(challenge, scalar)
        )
        return commitment + signature_scalar


def _is_clamped(scalar: bytes) -> bool:
    """The clamping `ed25519_create_keypair` applies, checked and never applied.

    Clamping a key that arrived unclamped would derive a *different* public key
    from the one the operator's other device is advertising — the same name for
    a different identity, discovered later by a peer that cannot reach them.
    Refusing says so now.
    """
    return (
        scalar[0] & 0b0000_0111 == 0
        and scalar[31] & 0b0100_0000 == 0b0100_0000
        and scalar[31] & 0b1000_0000 == 0
    )


def private_key_from_hex(text: str) -> LocalIdentity:
    """A supplied `prv_key` as an identity, or `PrivateKeyError` saying why not.

    The one place a private key enters the system from an operator: the command
    line and the panel both come through here, so they refuse the same input for
    the same reason in the same words (design D6).
    """
    cleaned = "".join(text.split()).removeprefix("0x").removeprefix("0X")
    if not cleaned:
        raise PrivateKeyError(
            f"no private key was supplied; expected {PRV_KEY_SIZE * 2} hex "
            f"characters ({PRV_KEY_SIZE} bytes)"
        )
    try:
        private_key = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise PrivateKeyError(f"the private key is not hexadecimal: {exc}") from exc

    if len(private_key) != PRV_KEY_SIZE:
        detail = (
            f"the private key is {len(private_key)} bytes, expected {PRV_KEY_SIZE} "
            f"({PRV_KEY_SIZE * 2} hex characters)"
        )
        if len(private_key) == SEED_SIZE:
            # Overwhelmingly the mistake this will be: a 32-byte seed, from an
            # old keyfile or another tool. Naming it saves the operator working
            # out why a key they believe is theirs is the wrong size.
            detail += (
                "; a 32-byte value is a seed, which this system does not accept — "
                "supply the 64-byte private key the device stores"
            )
        raise PrivateKeyError(detail)

    if not _is_clamped(private_key[:PUB_KEY_SIZE]):
        raise PrivateKeyError(
            "the private key is not in the representation MeshCore stores: its "
            "scalar is not clamped. Refusing to clamp it, because that would "
            "derive a different public key from the one this identity has "
            "elsewhere"
        )
    return LocalIdentity.from_private_key(private_key)


def refuse_unusable_node_hash(
    identity: LocalIdentity, *, avoid_node_hashes: Container[int] = frozenset()
) -> None:
    """The two conditions `generate_identity` retries past, as refusals.

    A generated key that lands on a reserved or taken hash is thrown away and
    rolled again. A supplied key is the one the operator has, so there is
    nothing to roll: the same two conditions have to be said out loud
    (design D7).
    """
    if identity.node_hash in RESERVED_NODE_HASHES:
        raise PrivateKeyError(
            f"the private key derives the public key {identity.public_key.hex()}, "
            f"whose node hash 0x{identity.node_hash:02x} is a prefix "
            "`Identity.cpp::validatePrivateKey` rejects outright; no MeshCore node "
            "will accept this identity"
        )
    if identity.node_hash in avoid_node_hashes:
        raise PrivateKeyError(
            f"node hash 0x{identity.node_hash:02x} is already held by another local "
            "entity, which would make inbound packets ambiguous between them"
        )


def generate_identity(
    *,
    avoid_node_hashes: Container[int] = frozenset(),
    max_attempts: int = DEFAULT_MAX_GENERATION_ATTEMPTS,
    seed_source: Callable[[int], bytes] = secrets.token_bytes,
) -> LocalIdentity:
    """Generate a keypair whose node hash collides with no local entity's.

    Two local entities sharing a node hash would make inbound packets
    ambiguous between our own identities, which DESIGN.md §3 rules out. Peer
    collisions are unavoidable and handled by trying each candidate key.

    Also skips the `0x00` and `0xFF` prefixes `Identity.cpp::validatePrivateKey`
    rejects. Raises `IdentityGenerationError` rather than looping forever when
    no acceptable hash is left.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for _ in range(max_attempts):
        candidate = LocalIdentity.from_seed(seed_source(SEED_SIZE))
        node_hash = candidate.node_hash
        if node_hash in RESERVED_NODE_HASHES or node_hash in avoid_node_hashes:
            continue
        return candidate
    raise IdentityGenerationError(
        f"no keypair with an unused node hash after {max_attempts} attempts; "
        "the set of hashes to avoid may cover every value"
    )
