"""Webhooks in the runtime (webhook-notifications task 6.1, design D9).

The dispatcher is handed in with a fake repository and transport, so what is
under test is the wiring: when it exists, what the run says about it, and that a
replay never reaches it.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

from sighop.monitor.render import WEBHOOKS_OFF_NO_DATABASE, WEBHOOKS_OFF_REPLAY
from sighop.net.contacts import ContactObservation
from sighop.net.rx import RxRecord
from sighop.radio.modem import EU868_NARROW, ModemEvent
from sighop.runtime import Runtime, RuntimeConfig
from sighop.webhooks.dispatcher import WebhookDispatcher
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR
from tests.test_runtime import _events, _startup, run_briefly, runtime
from tests.test_tx import ManualClock
from tests.test_webhooks_dispatcher import (
    NOW,
    SECRET,
    FakeRepository,
    FakeTransport,
    RecordingLogger,
    _record,
)

CAPTURE = CAPTURES_DIR / CAPTURE_FILES[0]


def _dispatcher(repository: FakeRepository, transport: FakeTransport) -> WebhookDispatcher:
    return WebhookDispatcher(
        repository=repository,
        secret=SECRET,
        logger=RecordingLogger(),
        clock=lambda: NOW,
        transport=transport,
    )


async def _capture_then_open() -> AsyncIterator[ModemEvent]:
    """The capture, then a source that stays open like a live link."""
    async for event in _events(CAPTURE):
        yield event
    await asyncio.Event().wait()


def _run(
    dispatcher: WebhookDispatcher, out: io.StringIO, *, live: bool = False, **config: object
) -> Runtime:
    return Runtime(
        source=_capture_then_open() if live else _events(CAPTURE),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, **config),  # type: ignore[arg-type]
        radio=EU868_NARROW,
        clock=ManualClock(),
        out=out,
        logger=RecordingLogger(),
        webhooks=dispatcher,
    )


async def test_a_run_with_no_database_says_webhooks_require_durable_storage() -> None:
    out = io.StringIO()
    run = runtime(_events(CAPTURE), out=out)
    await run_briefly(run)

    assert run.webhooks is None
    assert WEBHOOKS_OFF_NO_DATABASE in out.getvalue()
    assert " wh ok=" not in out.getvalue(), "no counters without a dispatcher"


async def test_a_replay_sends_nothing_even_with_a_dispatcher_handed_in() -> None:
    transport = FakeTransport()
    repository = FakeRepository(records=[_record("dev-all")])
    out = io.StringIO()
    run = _run(_dispatcher(repository, transport), out, replay=True, replay_persists=True)

    await run.run()

    assert run.webhooks is None
    assert len(run.contacts) > 0, "the capture created contacts"
    assert transport.calls == []
    assert repository.reads == 0
    assert WEBHOOKS_OFF_REPLAY in out.getvalue()


async def test_a_receive_only_run_delivers_first_sightings_and_reports_counters() -> None:
    transport = FakeTransport()
    dispatcher = _dispatcher(FakeRepository(records=[_record("dev-all")]), transport)
    out = io.StringIO()
    run = _run(dispatcher, out, live=True)
    assert not run.scheduler.transmit_enabled

    task = asyncio.create_task(run.run())
    for _ in range(2000):
        if dispatcher.delivered:
            break
        await asyncio.sleep(0.001)
    for _ in range(50):
        await dispatcher.wait_idle()
        await asyncio.sleep(0.001)
    run.stop()
    await asyncio.wait_for(task, 5)

    text = out.getvalue()
    assert "webhooks: 1 enabled (new_repeater, new_companion)" in text
    assert dispatcher.delivered > 0
    assert len(transport.calls) == dispatcher.delivered == dispatcher.events_raised
    assert f"wh ok={dispatcher.delivered} fail=0 drop=0" in text.strip().splitlines()[-1]


async def test_bots_still_observe_with_a_webhook_listener_registered() -> None:
    run = _run(_dispatcher(FakeRepository(), FakeTransport()), io.StringIO())
    listeners = run.contacts.observation_listeners
    assert run.webhooks is not None
    assert listeners == (run.bots.on_observation, run.webhooks.on_observation)

    seen: list[bool] = []
    bots = run.bots.on_observation

    def watched(observation: ContactObservation, record: RxRecord) -> None:
        seen.append(observation.created)
        bots(observation, record)

    run.contacts._listeners = (watched, listeners[1])
    await run.run()

    assert seen, "the bot host still received observations"
    assert run.webhooks.events_raised > 0
