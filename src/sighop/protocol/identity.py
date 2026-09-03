"""Ed25519 identities and node hashes, in MeshCore's key representation.

MeshCore stores a 64-byte private key that is the SHA-512 expansion of a
32-byte seed with the standard Ed25519 clamping already applied
(`lib/ed25519/keypair.c::ed25519_create_keypair`), and derives the public key
by scalar-multiplying the base point with the first 32 bytes of that buffer —
never re-hashing or re-clamping it (`Identity.cpp::LocalIdentity`). The shared
secret derivation in `crypto.py` uses that pre-clamped scalar directly, which
is the single fiddliest detail in the milestone.

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
    """A local entity's keypair, held as the seed MeshCore expands from.

    The seed is the canonical private material: signing needs it, and MeshCore's
    64-byte `prv_key` is recoverable from it via `meshcore_private_key`. The
    reverse is not true — a foreign 64-byte key imported from MeshCore has no
    recoverable seed, so it can derive shared secrets and verify but not sign.
    Nothing in this milestone needs that direction; milestone 5's persistence is
    where it would come up.
    """

    seed: bytes
    public_key: bytes

    def __post_init__(self) -> None:
        if len(self.seed) != SEED_SIZE:
            raise ValueError(f"seed must be {SEED_SIZE} bytes")
        if len(self.public_key) != PUB_KEY_SIZE:
            raise ValueError(f"public key must be {PUB_KEY_SIZE} bytes")

    @classmethod
    def from_seed(cls, seed: bytes) -> LocalIdentity:
        public_key, _ = bindings.crypto_sign_seed_keypair(seed)
        return cls(seed=seed, public_key=public_key)

    @property
    def identity(self) -> Identity:
        return Identity(public_key=self.public_key)

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

    @property
    def meshcore_private_key(self) -> bytes:
        """The 64-byte `prv_key` MeshCore stores and exports."""
        return _expand_seed(self.seed)

    @property
    def private_scalar(self) -> bytes:
        """The pre-clamped 32-byte scalar `calcSharedSecret` multiplies with."""
        return self.meshcore_private_key[:PUB_KEY_SIZE]

    def sign(self, message: bytes) -> bytes:
        """Sign `message`, producing a signature MeshCore's `Identity::verify` accepts."""
        signed = bindings.crypto_sign(
            message, self.seed + self.public_key
        )
        return signed[:SIGNATURE_SIZE]


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
