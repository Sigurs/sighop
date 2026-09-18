"""Waiting for the board's readback (`net/readback.py`, design D1-D4).

The helper is three lines of behaviour and one of them matters more than it
looks: a signal that is *already* set must not yield. Everything that transmits
goes through here, so a wait that cost a scheduler turn on every packet would be
a tax on the whole run for a case that exists only in a run's first second.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from sighop.net.airtime import NoRadioReadback
from sighop.net.readback import (
    RADIO_READBACK_WAIT_SECONDS,
    RadioReadbackTimeout,
    wait_for_readback,
)


async def test_a_signal_already_set_does_not_wait() -> None:
    signal = asyncio.Event()
    signal.set()
    turns = 0

    async def counting() -> None:
        nonlocal turns
        while True:
            turns += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(counting())
    await wait_for_readback(signal)
    ticker.cancel()

    assert turns == 0, "an available readback cost the run a scheduler turn"


async def test_no_signal_at_all_does_not_wait() -> None:
    """Every unit test that builds a messenger directly, and the replay path."""
    await wait_for_readback(None)


async def test_a_signal_set_during_the_wait_releases_it() -> None:
    signal = asyncio.Event()
    waiting = asyncio.create_task(wait_for_readback(signal))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiting.done(), "returned before the board answered"

    signal.set()
    await asyncio.wait_for(waiting, 1)


async def test_a_signal_never_set_raises_inside_the_budget() -> None:
    """D3: bounded, because a wait that cannot end is a silent hang."""
    started = time.monotonic()

    with pytest.raises(RadioReadbackTimeout) as raised:
        await wait_for_readback(asyncio.Event(), budget=0.05)

    assert time.monotonic() - started < 1.0
    assert "0.05s" in str(raised.value)


async def test_the_timeout_is_caught_as_a_missing_readback() -> None:
    """Why it is a subclass: every `except NoRadioReadback` already written."""
    with pytest.raises(NoRadioReadback):
        await wait_for_readback(asyncio.Event(), budget=0.01)


def test_the_budget_sits_inside_an_acknowledgement_window() -> None:
    """A late acknowledgement is only useful if it is still inside the window.

    The sender's is 4-5 s (`SEND_TIMEOUT_BASE_MS` plus its per-hop terms), so a
    budget at or above that would produce acknowledgements nobody is listening
    for any more — the wait would have bought nothing.
    """
    assert 0 < RADIO_READBACK_WAIT_SECONDS < 4.0
