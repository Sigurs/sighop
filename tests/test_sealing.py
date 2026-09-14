"""Sealed values alongside sealed seeds (webhook-notifications design D6)."""

from __future__ import annotations

import os

import pytest

from sighop.db.sealing import (
    SealAuthenticationError,
    SealFormatError,
    open_seed,
    open_value,
    seal_seed,
    seal_value,
)

SECRET = bytes(range(32))
OTHER = bytes(range(1, 33))


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


def test_a_sealed_seed_does_not_open_as_a_value() -> None:
    with pytest.raises(SealFormatError, match="version 1"):
        open_value(seal_seed(os.urandom(32), SECRET), SECRET)


def test_a_sealed_value_does_not_open_as_a_seed() -> None:
    # A 32-byte value would pass the seed length check if the version byte did not.
    with pytest.raises(SealFormatError, match="version 2"):
        open_seed(seal_value(os.urandom(32), SECRET), SECRET)


def test_a_truncated_value_is_a_format_error() -> None:
    with pytest.raises(SealFormatError, match="too short"):
        open_value(b"\x02abc", SECRET)
