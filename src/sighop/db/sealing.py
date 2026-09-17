"""Sealing an entity's private key at rest (design D4).

DESIGN.md §6 requires encryption at rest with a key from the environment, so
that "a DB dump must not be sufficient to impersonate a room server" is a
property the schema enforces rather than a hope.

The construction is **XSalsa20-Poly1305 secretbox** (PyNaCl's `SecretBox`).
It is already a dependency — the identity code uses libsodium — and its API
generates the nonce internally, which is the one thing most likely to be got
wrong by hand. Poly1305 supplies the authentication tag the spec asks for, so a
tampered `sealed_private_key` fails loudly instead of yielding some other key.

The stored value is **a version byte followed by the sealed box**, so a future
re-key has somewhere to declare itself without guessing at the old format.

Three failure modes, deliberately distinguished, because they send an operator to
three different places:

* **The stored bytes are not in the stored format** — an unknown version byte,
  or too few bytes to hold a box. The row is corrupt; the secret is irrelevant.
* **The box did not authenticate** — either the secret is not the one it was
  sealed under, or the row was altered. Cryptography cannot separate those two
  and this does not pretend to; the message says both.
* **The box opened onto a 32-byte seed** — a row written before the private key
  became the one representation. The row is *intact* and the secret is *right*;
  the format is simply gone. Reporting that as corruption would send an operator
  after a database fault that does not exist, so it gets its own error and its
  own words (change `create-entity-with-known-key`, design D4). Nothing here
  reads such a row: the length is inspected to name it, never to use it.

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
from sighop.protocol.identity import PRV_KEY_SIZE, SEED_SIZE

SEAL_VERSION = 1
"""Version 1: `bytes([1]) + SecretBox(key).encrypt(private_key)`, the box
carrying its own nonce. A later version changes this byte and nothing else has
to guess.

Unchanged by the move off seeds: the envelope is the same construction and the
same version, and only the plaintext inside it grew from 32 bytes to 64. Bumping
it would have claimed a crypto change that did not happen, and left the three
failure modes above sharing two messages."""

MINIMUM_SEALED_SIZE = 1 + SecretBox.NONCE_SIZE + SEED_SIZE + SecretBox.MACBYTES
"""Still measured against the *seed* length: a row of that size is one this code
refuses by name, and a row too short even for that is corrupt. Measuring against
the private key would collapse the two into one message."""


class SealError(RuntimeError):
    """A sealed private key that could not be produced or opened."""


class SealFormatError(SealError):
    """The stored bytes are not a sealed key at all — corrupt, not mis-keyed."""


class SealAuthenticationError(SealError):
    """The box did not authenticate: a wrong secret, or an altered row."""


class SealRemovedFormatError(SealError):
    """The box opened onto a seed: a row from before the private key format.

    Not a `SealFormatError`, because nothing about this row is malformed. It
    decrypted cleanly under the right secret; what it holds is a representation
    this system dropped.
    """


def seal_private_key(private_key: bytes, secret: bytes) -> bytes:
    """Seal a 64-byte private key under a 32-byte secret."""
    if len(private_key) != PRV_KEY_SIZE:
        raise SealError(f"a private key is {PRV_KEY_SIZE} bytes, got {len(private_key)}")
    if len(secret) != SECRET_KEY_SIZE:
        raise SealError(f"the encryption secret is {SECRET_KEY_SIZE} bytes, got {len(secret)}")
    return bytes([SEAL_VERSION]) + bytes(SecretBox(secret).encrypt(private_key))


def open_private_key(sealed: bytes, secret: bytes, *, entity: str = "the entity") -> bytes:
    """Open a sealed private key, or say which of the three things went wrong."""
    if len(secret) != SECRET_KEY_SIZE:
        raise SealError(f"the encryption secret is {SECRET_KEY_SIZE} bytes, got {len(secret)}")
    if len(sealed) < MINIMUM_SEALED_SIZE:
        raise SealFormatError(
            f"{entity}: the stored key material is {len(sealed)} bytes, too short to "
            f"be a sealed key (at least {MINIMUM_SEALED_SIZE}); the row is corrupt"
        )
    version = sealed[0]
    if version != SEAL_VERSION:
        raise SealFormatError(
            f"{entity}: the stored key material declares seal version {version}, and "
            f"this code writes and reads version {SEAL_VERSION}; the row is corrupt or "
            "was written by a later build"
        )
    try:
        private_key = SecretBox(secret).decrypt(sealed[1:])
    except CryptoError as exc:
        raise SealAuthenticationError(
            f"{entity}: the stored key material did not authenticate under "
            "SIGHOP_SECRET_KEY — either the secret is not the one it was sealed "
            "under, or the row has been altered. Nothing was decrypted"
        ) from exc
    if len(private_key) == SEED_SIZE:
        # The row is fine and the secret is right. Say that, so nobody goes
        # looking for a database fault (design D4).
        raise SealRemovedFormatError(
            f"{entity}: the stored key material is a {SEED_SIZE}-byte seed, written "
            "before this build. The row is intact and SIGHOP_SECRET_KEY opened it; "
            "seeds are simply no longer a format this system reads, and no command "
            "converts one. Re-import the identity with its 64-byte private key"
        )
    if len(private_key) != PRV_KEY_SIZE:
        raise SealFormatError(
            f"{entity}: the sealed value opened to {len(private_key)} bytes, not a "
            f"{PRV_KEY_SIZE}-byte private key; the row is corrupt"
        )
    return private_key


VALUE_SEAL_VERSION = 2
"""Version 2: `bytes([2]) + SecretBox(key).encrypt(value)` for a variable-length
value such as a webhook URL (webhook-notifications design D6). The distinct byte
is what keeps a seed from ever opening as a URL, or a URL as a seed: each opener
refuses the other's version as a format error before any decryption."""

MINIMUM_SEALED_VALUE_SIZE = 1 + SecretBox.NONCE_SIZE + SecretBox.MACBYTES


def seal_value(value: bytes, secret: bytes) -> bytes:
    """Seal a variable-length secret value under a 32-byte secret."""
    if len(secret) != SECRET_KEY_SIZE:
        raise SealError(f"the encryption secret is {SECRET_KEY_SIZE} bytes, got {len(secret)}")
    return bytes([VALUE_SEAL_VERSION]) + bytes(SecretBox(secret).encrypt(value))


def open_value(sealed: bytes, secret: bytes, *, what: str = "the value") -> bytes:
    """Open a sealed value, or say which of the two things went wrong."""
    if len(secret) != SECRET_KEY_SIZE:
        raise SealError(f"the encryption secret is {SECRET_KEY_SIZE} bytes, got {len(secret)}")
    if len(sealed) < MINIMUM_SEALED_VALUE_SIZE:
        raise SealFormatError(
            f"{what}: the stored value is {len(sealed)} bytes, too short to be a "
            f"sealed value (at least {MINIMUM_SEALED_VALUE_SIZE}); the row is corrupt"
        )
    version = sealed[0]
    if version != VALUE_SEAL_VERSION:
        raise SealFormatError(
            f"{what}: the stored value declares seal version {version}, and this "
            f"code reads sealed values as version {VALUE_SEAL_VERSION}; the row is "
            "corrupt, holds a different kind of secret, or was written by a later build"
        )
    try:
        return bytes(SecretBox(secret).decrypt(sealed[1:]))
    except CryptoError as exc:
        raise SealAuthenticationError(
            f"{what}: the stored value did not authenticate under SIGHOP_SECRET_KEY "
            "— either the secret is not the one it was sealed under, or the row has "
            "been altered. Nothing was decrypted"
        ) from exc
