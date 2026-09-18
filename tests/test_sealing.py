"""Sealed values alongside sealed private keys (webhook-notifications design D6).

The private key half of this file also pins the three ways opening a stored key
can fail — corrupt, mis-keyed, and written under the removed seed format — to
three distinguishable messages. An operator told the wrong one goes looking for
the wrong problem, which is the whole reason the third exists
(`create-entity-with-known-key`, design D4).
"""

from __future__ import annotations

import os

import pytest

from sighop.db.sealing import (
    SealAuthenticationError,
    SealError,
    SealFormatError,
    SealRemovedFormatError,
    open_private_key,
    open_value,
    seal_private_key,
    seal_value,
)
from sighop.protocol.identity import generate_identity

SECRET = bytes(range(32))
OTHER = bytes(range(1, 33))


def _sealed_seed(secret: bytes = SECRET, seed: bytes | None = None) -> bytes:
    """A row as a pre-0008 build wrote it: the same envelope, a 32-byte payload.

    Built with the sealing primitives rather than kept as a fixture, so it is a
    genuine old row and not a guess at one.
    """
    from nacl.secret import SecretBox

    from sighop.db.sealing import SEAL_VERSION

    return bytes([SEAL_VERSION]) + bytes(SecretBox(secret).encrypt(seed or os.urandom(32)))


# --- Values ----------------------------------------------------------------


def test_a_value_round_trips() -> None:
    url = b"https://discord.com/api/webhooks/1/token"
    sealed = seal_value(url, SECRET)

    assert url not in sealed
    assert sealed[0] == 2
    assert open_value(sealed, SECRET) == url


def test_an_empty_value_round_trips() -> None:
    assert open_value(seal_value(b"", SECRET), SECRET) == b""


def test_a_wrong_secret_is_an_authentication_error() -> None:
    with pytest.raises(SealAuthenticationError, match="webhook 'x'"):
        open_value(seal_value(b"https://h/p", SECRET), OTHER, what="webhook 'x'")


def test_a_sealed_private_key_does_not_open_as_a_value() -> None:
    with pytest.raises(SealFormatError, match="version 1"):
        open_value(seal_private_key(generate_identity().private_key, SECRET), SECRET)


def test_a_sealed_value_does_not_open_as_a_private_key() -> None:
    # A 64-byte value would pass the length check if the version byte did not.
    with pytest.raises(SealFormatError, match="version 2"):
        open_private_key(seal_value(os.urandom(64), SECRET), SECRET)


def test_a_truncated_value_is_a_format_error() -> None:
    with pytest.raises(SealFormatError, match="too short"):
        open_value(b"\x02abc", SECRET)


# --- Private keys ----------------------------------------------------------


def test_a_private_key_round_trips_and_is_not_in_the_ciphertext() -> None:
    identity = generate_identity()

    sealed = seal_private_key(identity.private_key, SECRET)

    assert sealed[0] == 1
    assert identity.private_key not in sealed
    assert identity.private_scalar not in sealed
    assert open_private_key(sealed, SECRET) == identity.private_key


def test_sealing_takes_the_private_key_and_nothing_else() -> None:
    with pytest.raises(SealError, match="a private key is 64 bytes, got 32"):
        seal_private_key(os.urandom(32), SECRET)


def test_a_wrong_secret_does_not_open_a_private_key() -> None:
    sealed = seal_private_key(generate_identity().private_key, SECRET)

    with pytest.raises(SealAuthenticationError, match="did not authenticate"):
        open_private_key(sealed, OTHER, entity="entity 'skogen'")


def test_an_altered_row_does_not_open() -> None:
    sealed = bytearray(seal_private_key(generate_identity().private_key, SECRET))
    sealed[-1] ^= 0xFF

    with pytest.raises(SealAuthenticationError, match="has been altered"):
        open_private_key(bytes(sealed), SECRET)


def test_a_truncated_row_is_corrupt() -> None:
    with pytest.raises(SealFormatError, match="too short"):
        open_private_key(b"\x01abc", SECRET)


def test_a_row_sealed_under_the_removed_seed_format_is_refused_by_name() -> None:
    """It decrypts cleanly. What it holds is simply a format that is gone."""
    with pytest.raises(SealRemovedFormatError) as excinfo:
        open_private_key(_sealed_seed(), SECRET, entity="entity 'skogen'")

    message = str(excinfo.value)
    assert "entity 'skogen'" in message
    assert "32-byte seed" in message
    assert "intact" in message and "opened it" in message
    assert "no command converts one" in message


def test_a_payload_of_some_third_length_is_corrupt() -> None:
    """Not 32 and not 64: nothing this system ever wrote."""
    from nacl.secret import SecretBox

    sealed = bytes([1]) + bytes(SecretBox(SECRET).encrypt(os.urandom(48)))

    with pytest.raises(SealFormatError, match="opened to 48 bytes"):
        open_private_key(sealed, SECRET)


def test_the_three_failures_do_not_read_alike() -> None:
    """The point of `SealRemovedFormatError` existing at all (design D4)."""
    good = seal_private_key(generate_identity().private_key, SECRET)
    altered = bytearray(good)
    altered[-1] ^= 0xFF

    messages = {}
    for label, call in {
        "removed": lambda: open_private_key(_sealed_seed(), SECRET),
        "mis-keyed": lambda: open_private_key(good, OTHER),
        "corrupt": lambda: open_private_key(b"\x01abc", SECRET),
        "altered": lambda: open_private_key(bytes(altered), SECRET),
    }.items():
        with pytest.raises(SealError) as excinfo:
            call()
        messages[label] = str(excinfo.value)

    # Three distinguishable problems. "altered" is deliberately not a fourth:
    # it shares the mis-keyed message, because the two cannot be told apart.
    distinct = {messages["removed"], messages["mis-keyed"], messages["corrupt"]}
    assert len(distinct) == 3, messages
    # The removed format is the one an operator must not read as a fault.
    assert "corrupt" not in messages["removed"]
    assert "did not authenticate" not in messages["removed"]
    assert "seed" not in messages["corrupt"]
    assert "seed" not in messages["mis-keyed"]
    # A wrong secret and an altered row cannot be told apart, and say so.
    assert messages["mis-keyed"] == messages["altered"]
