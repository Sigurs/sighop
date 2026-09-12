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

from sighop.bots.runtime import BotHost
from sighop.db.persistence import Persistence
from sighop.net.adverts import AdvertScheduler
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.contacts import ContactStore
from sighop.net.dedup import DedupCache
from sighop.net.dm import DirectMessenger
from sighop.net.paths import PathStore
from sighop.net.room import RoomServer
from sighop.net.tx import AirtimeBudget, TxScheduler
from sighop.radio.modem import RadioParams
from sighop.radio.probe import ProbeResult
from sighop.web.state import PanelState


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
