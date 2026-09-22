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

import uuid
from typing import Protocol, runtime_checkable

from sighop.bots.runtime import BotHost
from sighop.db.persistence import Persistence
from sighop.net.adverts import AdvertScheduler
from sighop.net.bus import IngressPipeline
from sighop.net.channels import ChannelMessenger
from sighop.net.contacts import ContactStore
from sighop.net.dm import DirectMessenger
from sighop.net.room import RoomServer
from sighop.net.tx import TxScheduler
from sighop.radio.modem import RadioParams
from sighop.radio.probe import ProbeResult
from sighop.webhooks.dispatcher import WebhookDispatcher


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
    def channels(self) -> ChannelMessenger:
        """The loaded channels, the post path and the channel counters."""

    async def reload_channels(self) -> bool:
        """Re-read the stored channels after the panel changed one (channel-messaging D4).

        A method rather than a property, and the one in this seam: a change made
        here has to reach decryption at once, and the runtime owns the loader.
        False when the read failed and the current set was kept.
        """

    def rename_entity(self, public_key: bytes, name: str) -> bool:
        """Adopt a renamed identity into this run, without a restart.

        `reload_channels`'s sibling, and here for the same reason: an identity's
        name is on the air — it travels in every advert and is the sender name
        of every channel post — so a rename the store has taken but the run has
        not is a run advertising a name that no longer exists. `False` when this
        run does not hold the identity, which is the ordinary answer for a
        rename made against a store some other process is serving.
        """

    async def stop_serving_room(self, room_id: uuid.UUID) -> bool:
        """Stop serving a room that has been deleted, without a restart.

        `False` when this run was not serving it.
        """

    async def stop_bot(self, bot_id: uuid.UUID) -> bool:
        """Stop and drop a bot that has been deleted, without a restart.

        Waits for the dispatch in flight rather than cancelling it. `False`
        when this run was not running it.
        """

    async def reconcile_entities(self) -> bool:
        """Re-read the stored identities and apply the difference, without a
        restart (`entity-store`, design D1).

        The identity counterpart of `reload_channels`: called by the panel
        right after it takes an identity write — create, import, enable,
        disable, remove — and by the periodic refresh loop for a change made
        by another process. Adopts, withdraws and re-configures, and reports
        what changed; a re-read that changes nothing is silent. `False` when
        this run has no way to re-read the store at all.
        """

    async def reconcile_rooms(self) -> None:
        """Take up a room whose identity this run now holds, and stop one
        whose identity it no longer holds, without a restart (`room-server`).

        Called by the panel after a room is created, and as part of
        `reconcile_entities`'s own pass.
        """

    async def reconcile_bots(self) -> None:
        """Start a bot that now qualifies to run, and stop one that no longer
        does, without a restart (`bot-runtime`).

        Called by the panel after a bot is created or its enabled state is
        changed, and as part of `reconcile_entities`'s own pass.
        """

    @property
    def rooms(self) -> list[RoomServer]:
        """The rooms this run is serving. Empty is not the same as none stored."""

    @property
    def bots(self) -> BotHost:
        """The bot workers, their modes and their counters."""

    @property
    def webhooks(self) -> WebhookDispatcher | None:
        """The webhook dispatcher and its counters, or `None` when this run
        sends no webhooks (no database, no usable secret, or a replay)."""

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

    Separate from `LiveState` because the two fail differently. Since milestone
    9 a run serving the interface always has a database — its accounts live
    there — so `None` is what a `Runtime` without `--web` holds, and what a page
    test's stub may hold; the served answer to "durable state is unavailable" is
    a degraded database, which every section states rather than rendering empty
    (`web-server`).
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
