"""The read seam: what the panel needs, described structurally (design D2).

Three-quarters of what an instrument panel shows is not in the database.
`TxScheduler.status()` holds the queue depths and the budget,
`AirtimeBudget.remaining_pct` is computed against a sliding window in memory,
`DedupCache.stats`, `PathStore.destination_count`, `ContactStore` and
`ProbeResult` are process state, and the packet feed exists for one instant per
frame. All of it lives on `Runtime`.

So the panel needs `Runtime` and must not import it. Two `Protocol`s are the
seam: `Runtime` satisfies them without knowing they exist, `cli.py` passes one
in, and the import graph stays acyclic — `runtime.py` imports nothing from
`web/`, `web/` imports nothing from `runtime.py`, and
`tests/test_web_state.py` asserts both.

This is the structural-typing shape milestone 6's `RoomStorage` and milestone
7's bot state already use, including the detail that decides whether it works:
**every member here is a read-only property**. A `Protocol` declaring a mutable
attribute is invariant in its type, so `rooms: list[RoomServer]` on the protocol
would demand exactly that annotation on the implementation; a read-only property
is satisfied by a plain attribute of a compatible type, which is what `Runtime`
has.

The types themselves come from `net/`, `db/` and `radio/` — packages `web/` may
import, being a peer of them. Only `runtime.py` is out of bounds, and it is the
only module in the project that composes them all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sighop.bots.runtime import BotHost
from sighop.db.persistence import Persistence
from sighop.net.adverts import AdvertScheduler
from sighop.net.bus import IngressPipeline
from sighop.net.contacts import ContactStore
from sighop.net.dm import DirectMessenger
from sighop.net.room import RoomServer
from sighop.net.tx import TxScheduler
from sighop.radio.modem import RadioParams
from sighop.radio.probe import ProbeResult


@runtime_checkable
class LiveState(Protocol):
    """The running platform, as the panel reads it.

    Live by construction: every value here is read from the object the radio is
    running on, at the moment the request is served. §6's rule that memory stays
    the authority is not a policy the panel has to remember — there is nothing
    else here for it to read.
    """

    @property
    def scheduler(self) -> TxScheduler:
        """Queue depths, the transmit gate and the airtime budget."""

    @property
    def pipeline(self) -> IngressPipeline:
        """The dedup cache, the path store and the delivered count."""

    @property
    def contacts(self) -> ContactStore:
        """Who we have heard, with what the advert claimed and whether it verified."""

    @property
    def adverts(self) -> AdvertScheduler:
        """The local identities this run holds, and their advert schedules."""

    @property
    def messenger(self) -> DirectMessenger:
        """The send path, the acknowledgement expectations, and the counters."""

    @property
    def rooms(self) -> list[RoomServer]:
        """The rooms this run is serving. Empty is not the same as none stored."""

    @property
    def bots(self) -> BotHost:
        """The bot workers, their modes and their counters."""

    @property
    def radio(self) -> RadioParams | None:
        """The parameters in force, or `None` when the board has not answered.

        Absent rather than defaulted, per §4.1: a value the board did not give
        must never be displayed as though it had.
        """

    @property
    def probe_result(self) -> ProbeResult | None:
        """The startup readback, when one was taken."""


@runtime_checkable
class DurableState(Protocol):
    """The database behind the panel, or its absence.

    `None` is a first-class answer and is the reason this is separate from
    `LiveState`: a run with no database serves the whole interface, and the
    sections backed by durable state say that no database is configured rather
    than rendering empty (`web-server`).
    """

    @property
    def persistence(self) -> Persistence | None:
        """Everything durable, or `None` when this run has no database."""


@runtime_checkable
class PanelState(LiveState, DurableState, Protocol):
    """Both halves, which together are what a page is given.

    `Runtime` satisfies this structurally. Nothing else in the project does, and
    nothing else has to: a test's stub satisfies it too, which is what lets
    every page be driven with no `Runtime` present.
    """
