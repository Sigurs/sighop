"""Room passwords (design D1, D2; §6, §7).

Two properties are what these are about, and both are stated as rules rather
than as hopes elsewhere:

* a database dump yields nothing — the stored value is an Argon2id hash and the
  password is not recoverable from it, and
* an unauthenticated stranger cannot make sighop transmit — `evaluate` returns
  `None` rather than a refusal, and `None` is answered with silence.

The concurrency assertions matter because 64 MiB per verification is the reason
the semaphore exists; a bound nothing tests is a bound that grows by accident.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from sighop.passwords import (
    ENCODED_PREFIX,
    PasswordError,
    PasswordHasher,
    PasswordPolicy,
    evaluate,
    hash_password,
    verify_password,
)
from sighop.protocol.payloads import Permission

PASSWORD = "correct-horse-battery-staple"


# --- 2.1 The hash itself ----------------------------------------------------


def test_a_password_round_trips_and_a_wrong_one_does_not() -> None:
    encoded = hash_password(PASSWORD)
    assert encoded.startswith(ENCODED_PREFIX)
    assert verify_password(encoded, PASSWORD)
    assert not verify_password(encoded, "hunter2")


def test_the_stored_value_carries_its_parameters_and_not_the_password() -> None:
    """2.1, §6: a database dump must not be sufficient to impersonate."""
    encoded = hash_password(PASSWORD)
    assert "$v=19$" in encoded and "m=" in encoded and "t=" in encoded and "p=" in encoded
    assert PASSWORD not in encoded
    # Nor anything derivable from it by the obvious encodings.
    for rendering in (
        PASSWORD.encode().hex(),
        PASSWORD[::-1],
        PASSWORD.upper(),
    ):
        assert rendering not in encoded


def test_two_hashes_of_one_password_differ_because_each_carries_its_own_salt() -> None:
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second
    assert verify_password(first, PASSWORD) and verify_password(second, PASSWORD)


def test_an_empty_password_hashes_like_any_other() -> None:
    """2.3: whether empty is *allowed* is configuration, not cryptography."""
    encoded = hash_password("")
    assert verify_password(encoded, "")
    assert not verify_password(encoded, "x")


def test_a_stored_value_that_is_not_one_of_ours_is_refused_not_guessed() -> None:
    with pytest.raises(PasswordError) as excinfo:
        verify_password("not-a-hash", PASSWORD)
    assert ENCODED_PREFIX in str(excinfo.value)


# --- 2.2 Off the loop, and bounded ------------------------------------------


async def test_the_loop_keeps_running_while_a_password_is_verified() -> None:
    """2.2: a login must not delay a reception (room-acl, §5)."""
    hasher = PasswordHasher()
    encoded = hash_password(PASSWORD)

    ticks = 0
    stop = False

    async def tick() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    assert await hasher.verify(encoded, PASSWORD)
    stop = True
    await ticker

    # A verification is tens of milliseconds; a loop that was blocked for it
    # would have ticked once or twice, not thousands of times.
    assert ticks > 100, f"the loop only advanced {ticks} times during a verification"


async def test_verifications_above_the_bound_queue_rather_than_run() -> None:
    """2.2, design D1: each run allocates 64 MiB, so the bound is the guard."""
    hasher = PasswordHasher(concurrency=2)
    encoded = hash_password(PASSWORD)

    await asyncio.gather(*(hasher.verify(encoded, PASSWORD) for _ in range(6)))

    assert hasher.peak_running <= 2, f"{hasher.peak_running} ran at once against a bound of 2"
    assert hasher.completed == 6
    assert hasher.waited > 0, "nothing queued, so the bound was never reached"
    assert hasher.counters()["password_peak_running"] == hasher.peak_running


async def test_a_higher_bound_actually_admits_more_at_once() -> None:
    """The counter would be worth nothing if it read 1 whatever the bound is."""
    hasher = PasswordHasher(concurrency=3)
    encoded = hash_password(PASSWORD)
    started = time.monotonic()
    await asyncio.gather(*(hasher.verify(encoded, PASSWORD) for _ in range(3)))
    assert time.monotonic() - started >= 0
    assert hasher.peak_running > 1


# --- 2.3 What a password earns ----------------------------------------------


@pytest.fixture
def hasher() -> PasswordHasher:
    return PasswordHasher()


async def test_the_admin_password_wins_over_an_identical_guest_password(
    hasher: PasswordHasher,
) -> None:
    """2.3: the firmware checks admin first, and so the order is the answer."""
    policy = PasswordPolicy(admin_hash=hash_password(PASSWORD), guest_hash=hash_password(PASSWORD))
    assert await evaluate(policy, PASSWORD, hasher=hasher) is Permission.ADMIN


async def test_the_guest_password_admits_at_read_write(hasher: PasswordHasher) -> None:
    policy = PasswordPolicy(admin_hash=hash_password("admin"), guest_hash=hash_password("guest"))
    assert await evaluate(policy, "guest", hasher=hasher) is Permission.READ_WRITE


async def test_a_room_with_no_guest_password_refuses_guests(hasher: PasswordHasher) -> None:
    """2.3: NULL hash with `guest_open` false is what a new room is."""
    policy = PasswordPolicy(admin_hash=hash_password("admin"))
    assert policy.guest_hash is None and policy.guest_open is False
    assert await evaluate(policy, "", hasher=hasher) is None
    assert await evaluate(policy, "anything", hasher=hasher) is None


async def test_an_explicitly_opened_room_admits_an_empty_password(
    hasher: PasswordHasher,
) -> None:
    """2.3: the same NULL hash, with the flag an operator had to set on purpose."""
    policy = PasswordPolicy(admin_hash=hash_password("admin"), guest_open=True)
    assert await evaluate(policy, "", hasher=hasher) is Permission.READ_WRITE
    assert await evaluate(policy, "whatever", hasher=hasher) is Permission.READ_WRITE


async def test_a_wrong_password_falls_back_to_read_only_only_when_allowed(
    hasher: PasswordHasher,
) -> None:
    """2.3, §7: read-only is `PERM_ACL_GUEST`, which may receive and not post."""
    closed = PasswordPolicy(admin_hash=hash_password("admin"))
    assert await evaluate(closed, "wrong", hasher=hasher) is None

    open_to_spectators = PasswordPolicy(admin_hash=hash_password("admin"), allow_read_only=True)
    admitted = await evaluate(open_to_spectators, "wrong", hasher=hasher)
    assert admitted is Permission.GUEST
    assert admitted.may_post is False


def test_read_write_and_admin_may_post_and_the_lowest_level_may_not() -> None:
    assert Permission.ADMIN.may_post and Permission.READ_WRITE.may_post
    assert not Permission.GUEST.may_post and not Permission.READ_ONLY.may_post
    assert Permission.ADMIN.is_admin and not Permission.READ_WRITE.is_admin


# --- 2.4 Nothing renders a password -----------------------------------------


def test_a_policy_renders_neither_its_passwords_nor_its_hashes() -> None:
    """2.4: a traceback is a rendering path, and it is the one nobody audits."""
    admin, guest = hash_password(PASSWORD), hash_password("guest")
    policy = PasswordPolicy(admin_hash=admin, guest_hash=guest, guest_open=True)
    rendered = repr(policy)
    assert PASSWORD not in rendered
    assert admin not in rendered and guest not in rendered
    assert "<redacted>" in rendered
    # And the flags, which are configuration and are meant to be visible.
    assert "guest_open=True" in rendered


def test_a_hasher_renders_no_material() -> None:
    hasher = PasswordHasher()
    assert PASSWORD not in repr(hasher)
    assert "_semaphore" not in repr(hasher)
