"""Repeater collection in the runtime (repeater-metrics 5.1).

The wiring only: a replay has no collector whatever the stored settings say,
and a live run starts one, subscribes it, and hands it bundled responses.
"""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import AsyncIterator

from sighop.config import generate_secret_key
from sighop.db.engine import Succeeded
from sighop.db.persistence import Persistence
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from sighop.radio.modem import EU868_NARROW, ModemEvent
from sighop.runtime import Runtime, RuntimeConfig
from tests.protocol.corpus import AMBIENT, CORPUS_DIR
from tests.test_runtime import _events, _startup
from tests.test_tx import ManualClock, RecordingLogger

CAPTURE = CORPUS_DIR / AMBIENT
SECRET = base64.b64decode(generate_secret_key())


async def _open_forever() -> AsyncIterator[ModemEvent]:
    async for event in _events(CAPTURE):
        yield event
    await asyncio.Event().wait()


def _run(persistence: Persistence, *, live: bool, **config: object) -> Runtime:
    return Runtime(
        source=_open_forever() if live else _events(CAPTURE),
        startup=_startup,
        persistence=persistence,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, **config),  # type: ignore[arg-type]
        radio=EU868_NARROW,
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
    )


async def _enable_collection(persistence: Persistence) -> None:
    stored = await persistence.entities.store(
        name="collector", identity=generate_identity(), secret=SECRET, node_type=NodeType.CHAT
    )
    assert isinstance(stored, Succeeded)
    saved = await persistence.repeater_collection.save(
        enabled=True,
        entity_id=stored.value.id,
        interval_minutes=5,
        recent_days=365,
        retention_days=30,
    )
    assert isinstance(saved, Succeeded)


async def test_a_replay_with_collection_enabled_submits_and_records_nothing(
    fresh_persistence: Persistence,
) -> None:
    await _enable_collection(fresh_persistence)
    submitted: list[str] = []
    run = _run(fresh_persistence, live=False, replay=True, transmit_enabled=True)
    run.scheduler.on_resolved = lambda submission, outcome: submitted.append(submission.origin)

    await run.run()

    assert run.collector is None
    assert run.path_bodies.on_bundled_response is None
    assert not any(origin.startswith("repeater_") for origin in submitted)
    history = await fresh_persistence.repeater_polls.latest_for(
        [contact.public_key for contact in run.contacts]
    )
    assert isinstance(history, Succeeded) and history.value == {}


async def test_a_live_run_starts_and_wires_the_collector(fresh_persistence: Persistence) -> None:
    run = _run(fresh_persistence, live=True)
    assert run.collector is not None
    assert run.path_bodies.on_bundled_response == run.collector.on_bundled_response
    assert any(s.name == "repeater-collection" for s in run.bus.subscriber_stats)

    task = asyncio.create_task(run.run())
    names: set[str] = set()
    for _ in range(500):
        names = {t.get_name() for t in asyncio.all_tasks()}
        if "repeater-collection" in names:
            break
        await asyncio.sleep(0.001)
    assert "repeater-collection" in names
    run.stop()
    await asyncio.wait_for(task, 5)
    assert run.collector._stopped.is_set()
