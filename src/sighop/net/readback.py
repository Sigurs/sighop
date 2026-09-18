"""Waiting for the board's radio readback, for transmissions composed before it.

`airtime.require_params` refuses to price a transmission the board has not told
us how to price, and that refusal is right (§4.1). What it cannot tell apart is
a board that answered *nothing* from one that has not answered *yet* — and at
startup the second is the common case, because the RX pipeline and the probe are
started in the same breath. A frame the modem buffered while the database opened
arrives before the parameters do, and everything composed in reaction to it was
being dropped.

So the waiting lives here, with the waiters rather than in `airtime.py`, which
stays a pure synchronous module. A site that may wait awaits the signal within a
bounded budget and then calls `require_params` exactly as before.
"""

from __future__ import annotations

import asyncio

from sighop.net.airtime import NoRadioReadback

RADIO_READBACK_WAIT_SECONDS = 3.0
"""How long a transmission waits for the board before it is refused.

Long enough to cover a probe that retries — `SetRadio` is already retried
5 s x 3 after the CP2102-reset discovery — and short enough to sit inside an
acknowledgement window, so an acknowledgement that waited is still one the
sender can use. Not derived from the ack timeout: a room push has no ack
timeout, and the two would then disagree about how long the board gets.
"""


class RadioReadbackTimeout(NoRadioReadback):
    """The readback was waited for and did not arrive inside the budget.

    A subclass, so every `except NoRadioReadback` already written keeps catching
    it and every refusal counter keeps counting what it counted. What changes is
    only what the operator reads: "the board was asked and said nothing" and
    "the board was waited for and did not answer in time" call for different
    actions — a firmware problem against a board that is slow or gone mid-run.
    """


async def wait_for_readback(
    signal: asyncio.Event | None,
    *,
    budget: float | None = None,
) -> None:
    """Return once parameters are available, or raise `RadioReadbackTimeout`.

    A `None` signal is not an error and not a wait: a messenger constructed
    without one — every unit test that builds one directly, and the replay path,
    which has the radio from the capture's provenance before it starts — behaves
    exactly as it did before this existed.
    """
    if signal is None or signal.is_set():
        return
    # Read at call time rather than bound as a default, so a test can shorten
    # the budget without every caller having to take one as an argument.
    budget = RADIO_READBACK_WAIT_SECONDS if budget is None else budget
    try:
        await asyncio.wait_for(signal.wait(), budget)
    except TimeoutError:
        raise RadioReadbackTimeout(
            f"no GetRadio readback after waiting {budget:g}s for the board; "
            "refusing to compute airtime from configured values"
        ) from None
