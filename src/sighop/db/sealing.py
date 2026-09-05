"""Sealing an entity's seed at rest (design D4).

DESIGN.md §6 requires encryption at rest with a key from the environment, so
that "a DB dump must not be sufficient to impersonate a room server" is a
property the schema enforces rather than a hope.

The construction is **XSalsa20-Poly1305 secretbox** (PyNaCl's `SecretBox`).
It is already a dependency — the identity code uses libsodium — and its API
generates the nonce internally, which is the one thing most likely to be got
wrong by hand. Poly1305 supplies the authentication tag the spec asks for, so a
tampered `sealed_seed` fails loudly instead of yielding some other key.

The stored value is **a version byte followed by the sealed box**, so a future
re-key has somewhere to declare itself without guessing at the old format.

Two failure modes, deliberately distinguished, because they send an operator to
different places:

* **The stored bytes are not in the stored format** — an unknown version byte,
  or too few bytes to hold a box. The row is corrupt; the secret is irrelevant.
* **The box did not authenticate** — either the secret is not the one it was
  sealed under, or the row was altered. Cryptography cannot separate those two
  and this does not pretend to; the message says both.

*Alternatives rejected:* AES-256-GCM via `cryptography` (equally sound, also
already a dependency, but the caller owns nonce generation and there is no upside
to buy that risk); `pgcrypto` (server-side encryption puts the key in the query
and the server's log, exactly backwards); a KEK/DEK envelope (correct for key
rotation at scale, unjustified for 2-5 identities — the version byte leaves the
door open).
"""

from __future__ import annotations

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from sighop.config import SECRET_KEY_SIZE
from sighop.protocol.identity import SEED_SIZE

SEAL_VERSION = 1
"""Version 1: `bytes([1]) + SecretBox(key).encrypt(seed)`, the box carrying its
own nonce. A later version changes this byte and nothing else has to guess."""

MINIMUM_SEALED_SIZE = 1 + SecretBox.NONCE_SIZE + SEED_SIZE + SecretBox.MACBYTES


class SealError(RuntimeError):
    """A sealed seed that could not be produced or opened."""


class SealFormatError(SealError):
    """The stored bytes are not a sealed seed at all — corrupt, not mis-keyed."""


class SealAuthenticationError(SealError):
    """The box did not authenticate: a wrong secret, or an altered row."""


def seal_seed(seed: bytes, secret: bytes) -> bytes:
    """Seal a 32-byte seed under a 32-byte secret."""
    if len(seed) != SEED_SIZE:
        raise SealError(f"a seed is {SEED_SIZE} bytes, got {len(seed)}")
    if len(secret) != SECRET_KEY_SIZE:
        raise SealError(f"the encryption secret is {SECRET_KEY_SIZE} bytes, got {len(secret)}")
    return bytes([SEAL_VERSION]) + bytes(SecretBox(secret).encrypt(seed))


def open_seed(sealed: bytes, secret: bytes, *, entity: str = "the entity") -> bytes:
    """Open a sealed seed, or say which of the two things went wrong."""
    if len(secret) != SECRET_KEY_SIZE:
        raise SealError(f"the encryption secret is {SECRET_KEY_SIZE} bytes, got {len(secret)}")
    if len(sealed) < MINIMUM_SEALED_SIZE:
        raise SealFormatError(
            f"{entity}: the stored key material is {len(sealed)} bytes, too short to "
            f"be a sealed seed (at least {MINIMUM_SEALED_SIZE}); the row is corrupt"
        )
    version = sealed[0]
    if version != SEAL_VERSION:
        raise SealFormatError(
            f"{entity}: the stored key material declares seal version {version}, and "
            f"this code writes and reads version {SEAL_VERSION}; the row is corrupt or "
            "was written by a later build"
        )
    try:
        seed = SecretBox(secret).decrypt(sealed[1:])
    except CryptoError as exc:
        raise SealAuthenticationError(
            f"{entity}: the stored key material did not authenticate under "
            "SIGHOP_SECRET_KEY — either the secret is not the one it was sealed "
            "under, or the row has been altered. Nothing was decrypted"
        ) from exc
    if len(seed) != SEED_SIZE:
        raise SealFormatError(
            f"{entity}: the sealed value opened to {len(seed)} bytes, not a "
            f"{SEED_SIZE}-byte seed; the row is corrupt"
        )
    return seed
