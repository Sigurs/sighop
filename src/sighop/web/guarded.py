"""Guarded actions: confirm, then act, and record it (design D10).

Seven of the panel's actions are not ordinary configuration changes:

* **revealing a private key** — the material that *is* an identity;
* **exporting a private key** — the same material, written out as a file that
  leaves the platform;
* **enabling transmission** — turning a receive-only node into one that emits;
* **raising the airtime ceiling** — above a limit that on EU 868 is legal
  rather than merely configured;
* **posting to a room** — which reaches every member of it and cannot be
  unsent;
* **a zero-hop advert now** and **a flood advert now** for a loaded identity —
  a packet on the air, and for a flood one that every repeater in the mesh
  repeats on other people's airtime.

§8 requires each to be re-confirmed and logged as its own wide event with the
acting user recorded. Milestone 9 fills that field in: `audit()`'s `actor` is a
required keyword with no default, so a call site that forgets it is a type error
rather than an event that silently names nobody.

The pattern is deliberately not "a link that does it":

1. A **confirmation view** states what this specific action does, and mints a
   nonce that exists only for it.
2. A **POST** carries the session's provenance token (design D9, every
   state-changing request) *plus* that nonce — and, for the four actions that
   change what the station may do or who it can impersonate, the acting user's
   **password**, verified against a fresh read of their account (milestone 9
   design D7). A room post does not ask for one: it is content, not a change of
   permission, and a password prompt on every post would train operators to
   type their password without reading. Neither does an advert: it is an
   ordinary transmission by an identity this operator already runs.
3. The action emits its **own** structured event, separate from the request's,
   naming the action, the target and the outcome — including when it was
   refused, because a refused attempt at revealing a key is the more
   interesting one.

A nonce is spent when it is used, so a confirmation page cannot be replayed by
reloading, and expires so an abandoned one does not stay live all session. It
names its **target** as well as its action, so a confirmation minted for one
identity or one room cannot be spent on another.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field

from sighop.logging import Logger, get_logger

NONCE_TTL_SECONDS = 300.0
"""How long a confirmation stays good. Long enough to read what it says, short
enough that a page left open in a tab is not an action waiting to happen."""

MAX_OUTSTANDING = 64
"""A bound, because a nonce store is memory a browser can ask for."""

REVEAL_KEY = "reveal_private_key"
ENABLE_TRANSMIT = "enable_transmit"
RAISE_CEILING = "raise_airtime_ceiling"
EXPORT_KEY = "export_private_key"
POST_TO_ROOM = "post_to_room"
ADVERT_ZERO_HOP = "advert_zero_hop"
ADVERT_FLOOD = "advert_flood"

REAUTHENTICATED_ACTIONS = frozenset({REVEAL_KEY, EXPORT_KEY, ENABLE_TRANSMIT, RAISE_CEILING})
"""The guarded actions whose confirmation carries the acting user's password."""

ACTION_DESCRIPTIONS = {
    REVEAL_KEY: (
        "shows this identity's private seed in the next response. A seed is the "
        "identity: anyone holding it can transmit as this node and read every "
        "direct message sent to it."
    ),
    ENABLE_TRANSMIT: (
        "opens the transmit gate for this run. Until now every packet has been "
        "composed, scheduled and charged against the duty-cycle budget, and then "
        "dropped at the hand-off to the modem. After this they go on the air."
    ),
    RAISE_CEILING: (
        "raises the airtime ceiling above the regulatory default. On EU 868 the "
        "10% default is a legal limit, not a tuning knob: raising it is a "
        "decision about what this station is permitted to do, not a preference."
    ),
    EXPORT_KEY: (
        "writes this identity's private seed out of the platform as a keyfile. "
        "The file is the identity: anyone holding it can transmit as this node "
        "and read every direct message sent to it."
    ),
    POST_TO_ROOM: (
        "posts to this room as the room's own identity. A post reaches every "
        "member of the room and cannot be unsent — it is not a configuration "
        "change, and there is no version of it that only some members see."
    ),
    ADVERT_ZERO_HOP: (
        "sends one zero-hop advert for this identity now. It reaches direct "
        "neighbours only — no repeater passes it on — and it leaves the flood "
        "schedule unchanged."
    ),
    ADVERT_FLOOD: (
        "sends one flood advert for this identity now. It is repeated by every "
        "repeater in the mesh, on everyone's airtime, and it takes the place of "
        "the next scheduled flood, which moves a full interval out."
    ),
}


@dataclass(frozen=True, slots=True)
class Nonce:
    value: str
    action: str
    target: str
    minted_at: dt.datetime

    def live(self, now: dt.datetime) -> bool:
        return (now - self.minted_at).total_seconds() <= NONCE_TTL_SECONDS


@dataclass(slots=True)
class NonceStore:
    """One-shot confirmations, bounded and expiring."""

    _nonces: dict[str, Nonce] = field(default_factory=dict)

    def mint(self, action: str, target: str, *, now: dt.datetime | None = None) -> str:
        at = now or dt.datetime.now(dt.UTC)
        self._expire(at)
        while len(self._nonces) >= MAX_OUTSTANDING:
            self._nonces.pop(next(iter(self._nonces)))
        value = secrets.token_urlsafe(24)
        self._nonces[value] = Nonce(
            value=value, action=action, target=target, minted_at=at
        )
        return value

    def spend(
        self, value: str, action: str, target: str, *, now: dt.datetime | None = None
    ) -> bool:
        """Consume a nonce, or refuse. A nonce is good exactly once."""
        at = now or dt.datetime.now(dt.UTC)
        self._expire(at)
        nonce = self._nonces.pop(value, None)
        if nonce is None:
            return False
        return nonce.action == action and nonce.target == target and nonce.live(at)

    def _expire(self, now: dt.datetime) -> None:
        for value, nonce in list(self._nonces.items()):
            if not nonce.live(now):
                del self._nonces[value]

    @property
    def outstanding(self) -> int:
        return len(self._nonces)


def audit(
    logger: Logger | None,
    *,
    action: str,
    target: str,
    outcome: str,
    actor: str,
    **fields: object,
) -> None:
    """One guarded action, as its own wide event (§8, design D10).

    Separate from the request's event on purpose: a reader looking for "who
    revealed a key" is not looking for "a POST returned 200", and the two have
    different retention value. `actor` is the signed-in username and has no
    default (milestone 9 design D7).
    """
    log = logger or get_logger(component="web")
    emit = log.info if outcome == "success" else log.error
    emit(
        "web_guarded_action",
        outcome=outcome,
        action=action,
        target=target,
        actor=actor,
        **fields,
    )
