"""A panel state with no runtime behind it (design D2).

`tests/roomfixtures.py`'s trick, applied to milestone 8's seam: the pages are
written against `web/state.py`'s `Protocol`s, so a test can drive every one of
them without a `Runtime`, without a modem and without a database. If that ever
stops being possible, the seam has stopped being a seam — which is the property
these fixtures exist to keep checkable.

The parts are real `net/` objects rather than mocks. They are constructible with
no radio and no I/O, they are what `Runtime` would be holding anyway, and a
mocked `TxScheduler` would let a page read a field the real one does not have.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
import weakref
from typing import TYPE_CHECKING

from sighop.bots.runtime import BotHost
from sighop.db.engine import DatabaseUnavailableError, Failed, Outcome, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import WebUserRecord, WebUserRepository, normalise_username
from sighop.net.adverts import AdvertScheduler
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.contacts import ContactStore
from sighop.net.dedup import DedupCache
from sighop.net.dm import DirectMessenger
from sighop.net.paths import PathStore
from sighop.net.room import RoomServer
from sighop.net.tx import AirtimeBudget, TxScheduler
from sighop.passwords import PasswordHasher
from sighop.radio.modem import RadioParams
from sighop.radio.probe import ProbeResult
from sighop.web.auth import (
    SESSION_COOKIE,
    AccountStore,
    Authenticator,
    LoginThrottle,
    Session,
    SessionStore,
)
from sighop.web.guard import TOKEN_HEADER
from sighop.web.state import PanelState

if TYPE_CHECKING:  # pragma: no cover
    import httpx2
    from fastapi.testclient import TestClient


@dataclasses.dataclass(slots=True)
class StubState:
    """Everything a page reads, assembled by hand.

    Satisfies `PanelState` structurally, which is asserted rather than assumed:
    `tests/test_web_state.py` binds one of these to a `PanelState` under mypy,
    so a page that grows a new requirement fails the type check here rather than
    at the first request.
    """

    scheduler: TxScheduler
    pipeline: IngressPipeline
    contacts: ContactStore
    adverts: AdvertScheduler
    messenger: DirectMessenger
    bots: BotHost
    rooms: list[RoomServer] = dataclasses.field(default_factory=list)
    radio: RadioParams | None = None
    probe_result: ProbeResult | None = None
    persistence: Persistence | None = None


def stub_state(
    *,
    transmit_enabled: bool = False,
    ceiling_fraction: float | None = None,
    radio: RadioParams | None = None,
    probe_result: ProbeResult | None = None,
    persistence: Persistence | None = None,
    stub_names: tuple[str, ...] = (),
) -> StubState:
    """One assembled panel state, with nothing running behind it.

    `stub_names` adds in-memory identities, each with a generated key. They are
    what makes the "no page reveals key material" assertion non-vacuous: a state
    with no identities in it has no seed for a page to have leaked.
    """
    budget = (
        AirtimeBudget()
        if ceiling_fraction is None
        else AirtimeBudget(ceiling_fraction=ceiling_fraction)
    )
    scheduler = TxScheduler(
        radio=radio, budget=budget, transmit_enabled=transmit_enabled
    )
    bus = NetworkBus(tx_sink=scheduler)
    pipeline = IngressPipeline(
        bus=bus, dedup=DedupCache(), paths=PathStore(), radio=radio
    )
    contacts = ContactStore()
    adverts = AdvertScheduler(submit=bus.submit)
    for name in stub_names:
        adverts.add_stub(name)
    messenger = DirectMessenger(
        contacts=contacts,
        paths=pipeline.paths,
        submit=bus.submit,
        entities=adverts.stubs,
        radio=radio,
    )
    return StubState(
        scheduler=scheduler,
        pipeline=pipeline,
        contacts=contacts,
        adverts=adverts,
        messenger=messenger,
        bots=BotHost(),
        radio=radio,
        probe_result=probe_result,
        persistence=persistence,
    )


def as_panel_state(state: StubState) -> PanelState:
    """The structural assertion itself, in the place mypy will check it."""
    return state


# --- Walking the route table (and why it needs walking) ---------------------


def registered_routes(app: object) -> list[object]:
    """Every route the application actually serves, routers included.

    This FastAPI version does **not** flatten `include_router` into
    `app.routes`: an included router appears as one opaque `_IncludedRouter`
    object holding its own `routes`. So the obvious `for route in app.routes`
    sees only the handful declared with `@app.get` directly — three of this
    panel's thirty-one — and every assertion "enumerated over the route table"
    silently checks a fraction of it.

    That is exactly the failure those assertions exist to prevent, so the walk
    lives here, once, and every sweep goes through it.
    """
    found: list[object] = []
    for route in app.routes:  # type: ignore[attr-defined]
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(registered_routes(inner))
        else:
            found.append(route)
    return found


def safe_pages(app: object) -> list[str]:
    """Every page a browser could reach by navigating, with no argument.

    Parameterised routes are exercised by the tests that own them; what is swept
    here is everything reachable by following a link or typing a path.
    """
    from fastapi.routing import APIRoute

    return sorted(
        route.path
        for route in registered_routes(app)
        if isinstance(route, APIRoute)
        and "{" not in route.path
        and "GET" in (route.methods or set())
    )


# --- Accounts and sessions with no database (milestone 9 design D16) --------


class ManualClock:
    """A clock a test moves by hand, as the airtime and dedup tests do."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingHasher(PasswordHasher):
    """A `PasswordHasher` that counts verifications and costs nothing.

    Every test but one signs in against this; `tests/test_web_login.py` uses
    the real Argon2id once, so the full path is exercised without every test
    paying 64 MiB and tens of milliseconds.
    """

    __slots__ = ("hashes", "verifications")

    def __init__(self) -> None:
        super().__init__()
        self.verifications = 0
        self.hashes = 0

    async def hash(self, password: str | bytes) -> str:
        self.hashes += 1
        return fake_hash(password)

    async def verify(self, encoded: str, password: str | bytes) -> bool:
        self.verifications += 1
        return encoded == fake_hash(password)


def fake_hash(password: str | bytes) -> str:
    import hashlib

    raw = password.encode("utf-8") if isinstance(password, str) else password
    return f"$argon2id$test${hashlib.sha256(raw).hexdigest()}"


@dataclasses.dataclass(slots=True)
class MemoryAccounts:
    """An `AccountStore` in a dict. `degraded` makes every read a `Failed`."""

    accounts: dict[str, WebUserRecord] = dataclasses.field(default_factory=dict)
    degraded: bool = False
    reads: int = 0

    def add(
        self,
        username: str,
        password: str,
        *,
        enabled: bool = True,
        password_hash: str | None = None,
    ) -> WebUserRecord:
        now = dt.datetime.now(dt.UTC)
        record = WebUserRecord(
            id=uuid.uuid4(),
            username=normalise_username(username),
            password_hash=password_hash or fake_hash(password),
            enabled=enabled,
            created_at=now,
            password_set_at=now,
        )
        self.accounts[record.username] = record
        return record

    def update(self, username: str, **changes: object) -> WebUserRecord:
        record = dataclasses.replace(self.accounts[username], **changes)  # type: ignore[arg-type]
        self.accounts[username] = record
        return record

    async def get(self, username: str) -> Outcome[WebUserRecord | None]:
        self.reads += 1
        if self.degraded:
            return Failed(
                operation="get_web_user", error=DatabaseUnavailableError("database unavailable")
            )
        return Succeeded(value=self.accounts.get(normalise_username(username)))

    async def count_enabled(self) -> Outcome[int]:
        if self.degraded:
            return Failed(
                operation="count_enabled_web_users",
                error=DatabaseUnavailableError("database unavailable"),
            )
        return Succeeded(value=sum(1 for record in self.accounts.values() if record.enabled))


OPERATOR = "dev-operator"
OPERATOR_PASSWORD = "an-operator-password"


def authenticator(
    accounts: MemoryAccounts | None = None,
    *,
    clock: ManualClock | None = None,
    logger: object | None = None,
) -> Authenticator:
    """A production `Authenticator` over an in-memory store and a free hasher."""
    store = accounts if accounts is not None else MemoryAccounts()
    if not store.accounts:
        store.add(OPERATOR, OPERATOR_PASSWORD)
    ticking = clock or ManualClock()
    return Authenticator(
        accounts=store,
        sessions=SessionStore(clock=ticking, logger=logger),  # type: ignore[arg-type]
        throttle=LoginThrottle(clock=ticking),
        hasher=CountingHasher(),
        logger=logger,  # type: ignore[arg-type]
    )


def as_account_store(store: MemoryAccounts) -> AccountStore:
    """The in-memory store satisfies the protocol — checked by mypy here."""
    return store


def repository_as_account_store(repository: WebUserRepository) -> AccountStore:
    """So does the real repository — checked by mypy here (task 2.2)."""
    return repository


_SESSIONS: weakref.WeakKeyDictionary[object, Session] = weakref.WeakKeyDictionary()


def _app_of(client: object) -> object:
    app = getattr(client, "app", None)
    if app is None:  # an httpx2.AsyncClient over an ASGITransport
        app = client._transport.app  # type: ignore[attr-defined]
    return app


def signed_in(client: object, username: str = OPERATOR, *, send_token: bool = True) -> Session:
    """Sign a client in through the production `SessionStore.issue()`.

    No bypass: the application's own guard resolves the cookie this sets, with
    the same store every request uses. With `send_token`, the session's CSRF
    token is sent as the provenance header on every later request, as a served
    page's HTMX would; the guard tests turn that off to aim at the token itself.
    """
    auth: Authenticator = _app_of(client).state.auth  # type: ignore[attr-defined]
    record = auth.accounts.accounts[normalise_username(username)]  # type: ignore[attr-defined]
    token, session = auth.sessions.issue(record.username, password_set_at=record.password_set_at)
    client.cookies.set(SESSION_COOKIE, token)  # type: ignore[attr-defined]
    if send_token:
        client.headers[TOKEN_HEADER] = session.csrf_token  # type: ignore[attr-defined]
    _SESSIONS[client] = session
    return session


def session_of(client: object) -> Session:
    return _SESSIONS[client]


def csrf(client: object) -> str:
    """The provenance token of the session this client is signed in as."""
    return session_of(client).csrf_token


def signed_client(app: object, *, send_token: bool = True, **kwargs: object) -> TestClient:
    """A `TestClient` already signed in as the operator."""
    from fastapi.testclient import TestClient

    client = TestClient(app, **kwargs)  # type: ignore[arg-type]
    signed_in(client, send_token=send_token)
    return client


def signed_async_client(**kwargs: object) -> httpx2.AsyncClient:
    """An `httpx2.AsyncClient` over the app's ASGI transport, signed in."""
    import httpx2

    client = httpx2.AsyncClient(**kwargs)  # type: ignore[arg-type]
    signed_in(client)
    return client
