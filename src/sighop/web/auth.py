"""Who may use the panel: accounts, sessions, throttling, sign-in (milestone 9).

Milestone 8 shipped a panel that can open the transmit gate, raise a legal
duty-cycle ceiling and hand out private seeds, with nothing in front of it but a
loopback default and a loud warning. This module is what was owed.

Four pieces, each small enough to be tested with an injected clock and no
database:

* **`AccountStore`** — the slice of `WebUserRepository` the panel reads, as a
  structural `Protocol` with read-only properties (design D16). The repository
  satisfies it without knowing; a test's in-memory store does too.
* **`SessionStore`** — sessions in memory, keyed by `sha256` of the cookie. The
  raw token is handed to the caller once, to be set as a cookie, and is never
  held here: a `repr`, a traceback or a heap dump of the store yields nothing a
  browser could present (design D3).
* **`LoginThrottle`** — consecutive failures per normalised username and per
  client address, in two LRU-bounded maps, with an exponential delay after five
  (design D8).
* **`Authenticator`** — the decisions built from those: resolving a cookie to a
  session (revalidating against the database at most once a minute, design D4),
  signing in with one response and equal work for every failure, and
  re-authenticating a guarded action (design D7).

**Memory.** The panel has its own `PasswordHasher`, separate from the room
server's, so a burst of room logins from the mesh cannot queue an operator's
sign-in and the reverse. Each allows two Argon2id operations at 64 MiB, so peak
Argon2id memory for the process is 4 x 64 MiB. The throttle refuses before
verifying once a guesser has failed five times, which is what keeps that bound
from being reachable on demand.

There is no way to construct an `Authenticator` that admits everyone, and no
parameter anywhere in `web/` that turns authentication off.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from sighop.db.engine import Failed, Outcome
from sighop.db.repositories import UsernameError, normalise_username
from sighop.logging import Logger, get_logger
from sighop.passwords import PasswordError, PasswordHasher

SESSION_COOKIE = "sighop_session"

MAX_SESSIONS = 256
"""A bound, because a session store is memory a successful sign-in can ask for.
Oldest first out: the operator who signed in yesterday loses to the one signing
in now."""

IDLE_SECONDS = 12 * 3600
"""Generous on purpose: the panel is an instrument left open beside other radio
tooling (§8), and a shorter idle would sign an operator out mid-watch. An open
chat pane polls, which counts as activity — so the lifetime below is the bound
that always applies."""

LIFETIME_SECONDS = 24 * 3600

REVALIDATE_SECONDS = 60.0
"""How stale a session's view of its account may become. This is how a
`sighop web user disable` in another process reaches a running panel, with no
IPC: within a minute of the change, at the session's next request."""

THROTTLE_KEYS = 4096
FREE_FAILURES = 5
MAX_DELAY_SECONDS = 900.0

UNVERIFIED = "this account cannot currently be verified"
"""What a guarded action says when the database could not confirm the account.
Pages stay available; nothing that changes what the station may do is."""

LOGIN_FAILED = "That username and password did not sign you in."
"""The one sentence every failed sign-in gets, whatever the reason was. The
reason is in the event, because the operator's log is trusted and the browser
is not (design D8)."""

Clock = Callable[[], float]


# --- What the panel reads about an account ----------------------------------


class Account(Protocol):
    """One account, as sign-in and revalidation need it."""

    @property
    def username(self) -> str: ...

    @property
    def password_hash(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def password_set_at(self) -> dt.datetime: ...


class AccountStore(Protocol):
    """The slice of `WebUserRepository` the panel uses (design D16).

    Methods rather than a mapping because a read can fail: a `Failed` is the
    database saying it is degraded, which is an answer the panel acts on
    differently from "no such account".
    """

    async def get(self, username: str) -> Outcome[Account | None]: ...

    async def count_enabled(self) -> Outcome[int]: ...


# --- Sessions ---------------------------------------------------------------


def token_key(token: str) -> str:
    """What the store is keyed by. The raw token never is."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Session:
    """One signed-in browser. Holds no credential a browser could present.

    `csrf_token` is the provenance token this session's pages carry (design D6).
    It is not the cookie: it is rendered into pages on purpose, and a script on
    the page can read it, which is what makes it useless without the cookie that
    scripts cannot.
    """

    key: str
    username: str
    issued_at: float
    last_seen: float
    password_set_at: dt.datetime
    csrf_token: str = field(repr=False)
    verified_at: float
    verified: bool = True
    """False while the database could not confirm the account at revalidation.
    Pages are served; every guarded action is refused (design D4)."""


@dataclass(slots=True)
class SessionStore:
    """Sessions in memory, bounded, expiring. A restart ends every one."""

    clock: Clock = time.monotonic
    logger: Logger | None = None
    max_sessions: int = MAX_SESSIONS
    idle_seconds: float = IDLE_SECONDS
    lifetime_seconds: float = LIFETIME_SECONDS
    _sessions: OrderedDict[str, Session] = field(default_factory=OrderedDict, repr=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="web")

    def __repr__(self) -> str:
        return f"SessionStore(sessions={len(self._sessions)}, max_sessions={self.max_sessions})"

    def __len__(self) -> int:
        return len(self._sessions)

    def issue(
        self,
        username: str,
        *,
        password_set_at: dt.datetime,
        replacing: str | None = None,
    ) -> tuple[str, Session]:
        """A new session and the raw token for its cookie, which only the caller holds.

        `replacing` is whatever cookie the browser presented. It is dropped
        before the new one exists, so a signed-in browser's old identifier grants
        nothing afterwards (session fixation, design D6).
        """
        if replacing:
            self._discard(token_key(replacing), reason="rotated")
        while len(self._sessions) >= self.max_sessions:
            oldest = next(iter(self._sessions))
            self._discard(oldest, reason="evicted")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(
            key=token_key(token),
            username=username,
            issued_at=now,
            last_seen=now,
            password_set_at=password_set_at,
            csrf_token=secrets.token_urlsafe(32),
            verified_at=now,
        )
        self._sessions[session.key] = session
        return token, session

    def resolve(self, token: str | None) -> Session | None:
        """The live session for a cookie, or None. Expired ones end here."""
        if not token:
            return None
        key = token_key(token)
        session = self._sessions.get(key)
        if session is None:
            return None
        now = self.clock()
        if now - session.issued_at >= self.lifetime_seconds:
            self._discard(key, reason="lifetime")
            return None
        if now - session.last_seen >= self.idle_seconds:
            self._discard(key, reason="idle")
            return None
        session.last_seen = now
        return session

    def end(self, session: Session, *, reason: str, **fields: object) -> None:
        self._discard(session.key, reason=reason, **fields)

    def _discard(self, key: str, *, reason: str, **fields: object) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        assert self.logger is not None
        self.logger.info(
            "web_session_ended",
            outcome="success",
            reason=reason,
            actor=session.username,
            session_age_s=round(self.clock() - session.issued_at, 1),
            **fields,
        )


# --- The throttle -----------------------------------------------------------


@dataclass(slots=True)
class _Failures:
    count: int
    last: float


@dataclass(slots=True)
class LoginThrottle:
    """Consecutive failures by username and by client, each LRU-bounded.

    Unknown usernames are tracked exactly like real ones, so what the throttle
    does leaks nothing about which exist. The client is the socket's own address:
    forwarding headers are not read (§8), so behind a proxy the per-address map
    degrades to a global throttle, which is the safe direction.
    """

    clock: Clock = time.monotonic
    capacity: int = THROTTLE_KEYS
    _by_username: OrderedDict[str, _Failures] = field(default_factory=OrderedDict)
    _by_client: OrderedDict[str, _Failures] = field(default_factory=OrderedDict)

    def retry_after(self, username: str, client: str) -> float:
        """Seconds until an attempt for this pair may run. Zero means now."""
        now = self.clock()
        return max(
            _wait(self._by_username.get(username), now),
            _wait(self._by_client.get(client), now),
        )

    def record_failure(self, username: str, client: str) -> None:
        now = self.clock()
        for table, key in ((self._by_username, username), (self._by_client, client)):
            entry = table.pop(key, None)
            table[key] = _Failures(count=1 if entry is None else entry.count + 1, last=now)
            while len(table) > self.capacity:
                table.popitem(last=False)

    def record_success(self, username: str, client: str) -> None:
        self._by_username.pop(username, None)
        self._by_client.pop(client, None)

    def failures(self, username: str | None = None, client: str | None = None) -> int:
        table, key = (
            (self._by_username, username) if username is not None else (self._by_client, client)
        )
        entry = table.get(key or "")
        return 0 if entry is None else entry.count

    @property
    def tracked(self) -> tuple[int, int]:
        return len(self._by_username), len(self._by_client)


def delay_for(failures: int) -> float:
    """`min(2 ** (n - 5), 900)` seconds once five failures are spent."""
    if failures < FREE_FAILURES:
        return 0.0
    return min(float(2 ** (failures - FREE_FAILURES)), MAX_DELAY_SECONDS)


def _wait(entry: _Failures | None, now: float) -> float:
    if entry is None:
        return 0.0
    return max(0.0, entry.last + delay_for(entry.count) - now)


# --- Decisions --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignIn:
    """What a sign-in attempt came to. `token` is set only on success."""

    outcome: str
    reason: str
    username: str
    token: str | None = field(default=None, repr=False)
    session: Session | None = None

    @property
    def succeeded(self) -> bool:
        return self.token is not None


@dataclass(frozen=True, slots=True)
class Reauthentication:
    """Whether a guarded action's password re-entry admits it."""

    ok: bool
    reason: str
    """`verified`, `bad_password` (wrong or missing), `throttled`, `unverified`,
    `account_changed`."""

    @property
    def message(self) -> str:
        if self.reason in ("unverified",):
            return UNVERIFIED
        return "the password did not confirm this action"


@dataclass(slots=True)
class Authenticator:
    """Every authentication decision the panel makes, in one place."""

    accounts: AccountStore
    sessions: SessionStore = field(default_factory=SessionStore)
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    hasher: PasswordHasher = field(default_factory=PasswordHasher)
    """The panel's own, never the room server's (design D8)."""

    logger: Logger | None = None
    login_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    """The per-process provenance token the sign-in form carries — all a
    request with no session yet can be bound to (design D6)."""

    revalidate_seconds: float = REVALIDATE_SECONDS
    _dummy_hash: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="web")

    async def start(self) -> None:
        """Compute `DUMMY_HASH` once, through the hasher, off the loop.

        Called before the interface serves. A sign-in for an unknown username
        verifies against this, so it costs what a wrong password costs.
        """
        if self._dummy_hash is None:
            self._dummy_hash = await self.hasher.hash(secrets.token_bytes(32))

    # --- Resolving a request's session -------------------------------------

    async def resolve(self, token: str | None) -> Session | None:
        """The session a cookie names, revalidated when it is due."""
        session = self.sessions.resolve(token)
        if session is None:
            return None
        clock = self.sessions.clock
        if clock() - session.verified_at < self.revalidate_seconds and session.verified:
            return session
        return await self._revalidate(session)

    async def _revalidate(self, session: Session) -> Session | None:
        clock = self.sessions.clock
        read = await self.accounts.get(session.username)
        if isinstance(read, Failed):
            # Kept, marked, and retried on the next request: an outage costs
            # guarded actions and new sign-ins, never the open dashboard.
            session.verified = False
            return session
        account = read.value
        detail = _changed(session, account)
        if detail is not None:
            self.sessions.end(session, reason="account_changed", detail=detail)
            return None
        session.verified = True
        session.verified_at = clock()
        return session

    # --- Signing in ---------------------------------------------------------

    async def sign_in(
        self,
        username: str,
        password: str,
        *,
        client: str,
        presented: str | None = None,
    ) -> SignIn:
        """The sign-in decision (design D8).

        Throttle first, and a throttled attempt runs no verification at all.
        Then exactly one verification — against the account's hash, or against
        `DUMMY_HASH` when there is no account — so an unknown username, a
        disabled account and a wrong password cost the same work and get the
        same response.
        """
        try:
            name = normalise_username(username)
            usable = True
        except UsernameError:
            # Still throttled and still verified: a malformed username must not
            # be a faster way to learn "no" than a well-formed unknown one.
            name = username[:64].casefold()
            usable = False

        waited = self.throttle.retry_after(name, client)
        if waited > 0:
            return self._login_event(
                SignIn(outcome="refused", reason="throttled", username=name),
                client=client,
                retry_after_s=round(waited, 1),
            )

        account: Account | None = None
        if usable:
            read = await self.accounts.get(name)
            if isinstance(read, Failed):
                await self._verify(None, password)
                return self._login_event(
                    SignIn(outcome="refused", reason="database_unavailable", username=name),
                    client=client,
                )
            account = read.value

        verified = await self._verify(account, password)
        if account is None:
            reason = "unknown_user"
        elif not account.enabled:
            reason = "disabled"
        elif not verified:
            reason = "bad_password"
        else:
            self.throttle.record_success(name, client)
            token, session = self.sessions.issue(
                account.username,
                password_set_at=account.password_set_at,
                replacing=presented,
            )
            return self._login_event(
                SignIn(
                    outcome="success",
                    reason="success",
                    username=account.username,
                    token=token,
                    session=session,
                ),
                client=client,
            )
        self.throttle.record_failure(name, client)
        return self._login_event(
            SignIn(outcome="refused", reason=reason, username=name), client=client
        )

    async def _verify(self, account: Account | None, password: str) -> bool:
        if self._dummy_hash is None:
            await self.start()
        encoded = account.password_hash if account is not None else self._dummy_hash
        assert encoded is not None
        try:
            return await self.hasher.verify(encoded, password)
        except PasswordError:
            return False

    def _login_event(self, result: SignIn, *, client: str, **fields: object) -> SignIn:
        assert self.logger is not None
        emit = self.logger.info if result.succeeded else self.logger.error
        emit(
            "web_login",
            outcome=result.outcome,
            reason=result.reason,
            username=result.username,
            client=client,
            **fields,
        )
        return result

    def sign_out(self, session: Session) -> None:
        self.sessions.end(session, reason="logout")

    # --- Re-authenticating a guarded action --------------------------------

    async def reauthenticate(
        self, session: Session, password: str | None, *, client: str
    ) -> Reauthentication:
        """A fresh read and one verification, for a guarded action (design D7).

        A wrong or missing password counts toward the sign-in throttle for that
        username and does not end the session: a typo should not cost the
        operator their open panel.
        """
        if not session.verified:
            return Reauthentication(ok=False, reason="unverified")
        name = session.username
        if self.throttle.retry_after(name, client) > 0:
            return Reauthentication(ok=False, reason="throttled")
        read = await self.accounts.get(name)
        if isinstance(read, Failed):
            session.verified = False
            return Reauthentication(ok=False, reason="unverified")
        account = read.value
        detail = _changed(session, account)
        if detail is not None:
            self.sessions.end(session, reason="account_changed", detail=detail)
            return Reauthentication(ok=False, reason="account_changed")
        assert account is not None
        if password is None:
            # Refused exactly as a wrong password is, reason included: a form
            # with the field removed is not a different kind of attempt.
            self.throttle.record_failure(name, client)
            return Reauthentication(ok=False, reason="bad_password")
        if not await self._verify(account, password):
            self.throttle.record_failure(name, client)
            return Reauthentication(ok=False, reason="bad_password")
        return Reauthentication(ok=True, reason="verified")

    async def enabled_accounts(self) -> Outcome[int]:
        return await self.accounts.count_enabled()


def _changed(session: Session, account: Account | None) -> str | None:
    """Why this session's account no longer admits it, or None."""
    if account is None:
        return "removed"
    if not account.enabled:
        return "disabled"
    if account.password_set_at != session.password_set_at:
        return "password_changed"
    return None


def safe_next(value: str | None) -> str:
    """A post-sign-in destination: a same-origin absolute path, or `/`.

    Never a URL. `//evil.example` and `/\\evil.example` are paths to a parser and
    hosts to a browser, so both are refused along with anything carrying a
    scheme or a control character.
    """
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if any(ord(character) < 0x20 or character == "\\" for character in value):
        return "/"
    if ":" in value.split("?", 1)[0].split("/", 2)[1]:
        return "/"
    return value
