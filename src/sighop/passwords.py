"""Room passwords: Argon2id from libsodium, off the loop, bounded (design D1).

PyNaCl is already a dependency for §5's sealing and identity code, and exposes
libsodium's Argon2id as `nacl.pwhash.argon2id.str()` / `nacl.pwhash.verify()`.
What comes back is the standard encoded string —
`$argon2id$v=19$m=65536,t=2,p=1$<salt>$<tag>` — so the parameters and the salt
travel *with* the hash and changing them later needs no schema change.
`argon2-cffi` would be a new dependency for what libsodium already gives us, and
`cryptography`'s `Argon2id` needs OpenSSL 3.2+ against our floor of
`cryptography>=43`. Neither buys anything.

The consequence that matters here is operational rather than cryptographic. The
interactive parameters allocate **64 MiB** and run for tens of milliseconds per
verification. Inline on the asyncio loop that stalls the RX pipeline for the
duration; several at once allocate 64 MiB each. So every hash and every
verification runs in a worker thread, and a semaphore bounds how many may run at
one time. `RoomThrottle` in `net/room.py` fronts this, which makes the login
throttle also the memory-exhaustion guard — the honest way to describe it.

Nothing in this module ever renders a password. The only values it returns are
encoded hashes and permission levels, and `PasswordPolicy` hides even the hashes
from `repr`, because the one place a secret escapes is the debug output nobody
audited.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import nacl.exceptions
import nacl.pwhash

from sighop.protocol.payloads import Permission

T = TypeVar("T")

DEFAULT_VERIFY_CONCURRENCY = 2
"""How many Argon2id operations may run at once (design D1). Two, because each
allocates 64 MiB and the number that matters is the one a login storm can reach,
not the one a benchmark likes."""

ENCODED_PREFIX = "$argon2id$"


class PasswordError(ValueError):
    """A stored hash that is not one, or a password libsodium will not take."""


def hash_password(password: str | bytes) -> str:
    """Hash a password. Blocking — call it through `PasswordHasher` from a loop.

    An empty password is legal and is hashed like any other: §7's "an empty
    guest password is legal… but must be an explicit choice" is a decision about
    *configuration*, and expressing it by refusing to hash an empty string would
    put it in the wrong layer.
    """
    encoded: bytes = nacl.pwhash.argon2id.str(_encode(password))
    return encoded.decode("ascii")


def verify_password(encoded: str, password: str | bytes) -> bool:
    """Whether `password` is the one `encoded` was made from.

    A wrong password is a `False`, not an exception: it is the ordinary case on
    this path, and a caller that has to catch to learn "no" is a caller that
    eventually catches something else by accident.
    """
    if not encoded.startswith(ENCODED_PREFIX):
        raise PasswordError(
            f"stored hash does not start with {ENCODED_PREFIX!r}; it was not produced "
            "by this code and no password can be checked against it"
        )
    try:
        return bool(nacl.pwhash.verify(encoded.encode("ascii"), _encode(password)))
    except nacl.exceptions.InvalidkeyError:
        return False


def _encode(password: str | bytes) -> bytes:
    return password.encode("utf-8") if isinstance(password, str) else password


@dataclass(slots=True)
class PasswordHasher:
    """Runs hashing and verification in a thread, bounded by a semaphore.

    The counters exist so the bound is observable. A throttle that cannot be
    seen from the status line is indistinguishable from a mesh that went quiet
    (design D8), and the same is true one layer down: `peak_running` is what
    proves the semaphore is doing anything at all.
    """

    concurrency: int = DEFAULT_VERIFY_CONCURRENCY
    running: int = field(default=0, init=False)
    peak_running: int = field(default=0, init=False)
    completed: int = field(default=0, init=False)
    waited: int = field(default=0, init=False)
    """How many calls found the bound already full and had to queue."""

    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def _gate(self) -> asyncio.Semaphore:
        # Created lazily: a Semaphore built outside a running loop binds to the
        # wrong one, and this object is constructed by configuration code.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    async def hash(self, password: str | bytes) -> str:
        return await self._run(lambda: hash_password(password))

    async def verify(self, encoded: str, password: str | bytes) -> bool:
        return await self._run(lambda: verify_password(encoded, password))

    async def _run(self, work: Callable[[], T]) -> T:
        gate = self._gate()
        if gate.locked():
            self.waited += 1
        async with gate:
            self.running += 1
            self.peak_running = max(self.peak_running, self.running)
            try:
                return await asyncio.to_thread(work)
            finally:
                self.running -= 1
                self.completed += 1

    def counters(self) -> dict[str, int]:
        return {
            "password_concurrency": self.concurrency,
            "password_peak_running": self.peak_running,
            "password_completed": self.completed,
            "password_queued": self.waited,
        }


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """What a room will accept, as stored — hashes and two flags, no passwords.

    The three states the two guest columns encode between them are the whole
    point of the shape (design D2), and each is a different answer to a login:

    * a guest hash set — that password admits a guest at read-write,
    * NULL with `guest_open` false — guest logins are refused; this is what a
      newly created room is, and it is why an empty guest password is never the
      state a room starts in,
    * NULL with `guest_open` true — an operator opened the room on purpose, and
      an empty password is admitted at read-write.
    """

    admin_hash: str
    guest_hash: str | None = None
    guest_open: bool = False
    allow_read_only: bool = False

    def __repr__(self) -> str:
        # Not the plaintext — there is none here — but a hash is still material
        # an offline attack starts from, and a traceback is a rendering path.
        return (
            f"PasswordPolicy(admin_hash=<redacted>, "
            f"guest_hash={'<redacted>' if self.guest_hash else None}, "
            f"guest_open={self.guest_open}, allow_read_only={self.allow_read_only})"
        )


async def evaluate(
    policy: PasswordPolicy, password: str | bytes, *, hasher: PasswordHasher
) -> Permission | None:
    """The permission a password earns, or None for "answer with silence".

    The order is the firmware's (`MyMesh.cpp:343-356`) and the order matters: the
    admin password is checked first, so a room whose two passwords are the same
    admits an administrator rather than a guest.

    `None` is not an error. It is §7's load-bearing behaviour — the firmware
    returns without replying when nothing matches — and it is what makes "an
    unauthenticated stranger cannot make sighop transmit" true.
    """
    if await hasher.verify(policy.admin_hash, password):
        return Permission.ADMIN
    if _guest_admits(policy) and await _guest_matches(policy, password, hasher=hasher):
        return Permission.READ_WRITE
    if policy.allow_read_only:
        # PERM_ACL_GUEST, which is what the firmware grants here and what the
        # rest of sighop calls read-only: receives history, may not post.
        return Permission.GUEST
    return None


def _guest_admits(policy: PasswordPolicy) -> bool:
    """Whether guest logins are possible at all — the NULL/`guest_open` split."""
    return policy.guest_hash is not None or policy.guest_open


async def _guest_matches(
    policy: PasswordPolicy, password: str | bytes, *, hasher: PasswordHasher
) -> bool:
    if policy.guest_hash is not None:
        return await hasher.verify(policy.guest_hash, password)
    # Open room: any password is the guest password, empty included. There is no
    # hash to verify against, so nothing is verified — and no Argon2id run is
    # spent proving that anything matches everything.
    return True
