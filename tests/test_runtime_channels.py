"""Channels in the runtime (channel-messaging tasks 6.1 and 6.3, design D4, D10).

What is under test is the wiring: where the channel set comes from, what a run
says about it before any traffic, that a failed reload changes nothing, and that
a replay decrypts but records only when told to write.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select

from sighop.db.engine import Database, Succeeded
from sighop.db.models import ChannelMessage as ChannelMessageRow
from sighop.db.persistence import Persistence
from sighop.monitor.render import CHANNELS_OFF
from sighop.net.channels import ChannelKind, ChannelSet, LoadedChannel
from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, channel_key_from_hashtag
from sighop.radio.modem import EU868_NARROW, ModemEvent
from sighop.runtime import Runtime, RuntimeConfig
from tests.protocol.corpus import CAPTURES_DIR
from tests.test_runtime import RecordingLogger, _events, _never_ends, _startup, run_briefly, runtime
from tests.test_tx import ManualClock

PUBLIC_CAPTURE = CAPTURES_DIR / "2026-09-03.jsonl"
"""33 distinct Public frames (see `test_channel_foreign_decrypt.py`)."""

PUBLIC = LoadedChannel(1, "Public", ChannelKind.PUBLIC, PUBLIC_CHANNEL_KEY)
HASHTAG = LoadedChannel(
    2, "#dev-sighop", ChannelKind.HASHTAG, channel_key_from_hashtag("#dev-sighop")
)


def _with_loader(loader, out: io.StringIO, source=None) -> Runtime:
    return Runtime(
        source=source if source is not None else _never_ends(),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        radio=EU868_NARROW,
        clock=ManualClock(),
        out=out,
        logger=RecordingLogger(),
        channel_loader=loader,
    )


async def test_a_run_with_no_database_says_it_has_no_channels() -> None:
    out = io.StringIO()
    run = runtime(_events(PUBLIC_CAPTURE), out=out)
    await run_briefly(run)

    assert CHANNELS_OFF in out.getvalue()
    assert "requires durable storage" in CHANNELS_OFF
    assert len(run.channels.channels) == 0
    assert run.channels.decrypted == 0 and run.channels.unknown > 0
    assert "ch_rx=" not in out.getvalue()


async def test_startup_lists_the_loaded_channels_with_their_hashes() -> None:
    async def loader() -> ChannelSet:
        return ChannelSet(channels=(PUBLIC, HASHTAG))

    out = io.StringIO()
    run = _with_loader(loader, out)
    await run_briefly(run)

    text = out.getvalue()
    assert "channels: Public[11](public, guessable), #dev-sighop[" in text
    assert f"#dev-sighop[{HASHTAG.channel_hash:02x}](hashtag, guessable)" in text
    assert "ch_rx=0 ch_unknown=0 ch_undecryptable=0 ch_tx=0 ch_repeats=0" in text


async def test_a_failing_loader_keeps_the_previous_set_and_reports_it() -> None:
    calls = 0

    async def loader() -> ChannelSet:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("database unreachable")
        return ChannelSet(channels=(PUBLIC,))

    out = io.StringIO()
    run = _with_loader(loader, out)
    assert await run.reload_channels() is True
    assert await run.reload_channels() is False

    assert [c.name for c in run.channels.channels] == ["Public"]
    logger = run.logger
    assert isinstance(logger, RecordingLogger)
    assert any(name == "channel_config_read_failed" for _l, name, _f in logger.events)


async def test_a_reload_that_changes_the_set_says_so_in_the_run_output() -> None:
    """Milestone 10's live exercise: a channel added from the terminal reached
    the running station with nothing said about it in the run.
    """
    sets = iter((ChannelSet(channels=(PUBLIC,)), ChannelSet(channels=(PUBLIC, HASHTAG))))
    current = ChannelSet()

    async def loader() -> ChannelSet:
        nonlocal current
        current = next(sets, current)
        return current

    out = io.StringIO()
    run = _with_loader(loader, out)
    await run_briefly(run)  # the startup set is loaded and reported as startup

    assert "channels: Public[11]" in out.getvalue()
    assert "channels changed" not in out.getvalue()

    assert await run.reload_channels() is True
    text = out.getvalue()
    assert "channels changed: +#dev-sighop  (2 loaded" in text
    logger = run.logger
    assert isinstance(logger, RecordingLogger)
    assert any(name == "channel_set_changed" for _l, name, _f in logger.events)


async def test_a_replay_decrypts_public_frames_with_a_loader() -> None:
    async def loader() -> ChannelSet:
        return ChannelSet(channels=(PUBLIC,))

    out = io.StringIO()
    run = _with_loader(loader, out, source=_events(PUBLIC_CAPTURE))
    await asyncio.wait_for(run.run(), 10)

    assert run.channels.decrypted == 33
    assert "✗ Public from claimed" in out.getvalue()


@pytest.mark.database
async def test_a_channel_added_to_the_repository_appears_after_reload(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    run = runtime(_never_ends(), out=io.StringIO(), persistence=persistence)

    assert await run.reload_channels() is True
    assert [c.name for c in run.channels.channels] == ["Public"]
    assert isinstance(await persistence.channels.add_hashtag("#dev-sighop"), Succeeded)
    assert await run.reload_channels() is True
    assert [c.name for c in run.channels.channels] == ["Public", "#dev-sighop"]


async def _channel_rows(database: Database) -> int:
    async with database.sessions() as session:
        return int(
            (await session.execute(select(func.count()).select_from(ChannelMessageRow))).scalar_one()
        )


async def _replay_then_open() -> AsyncIterator[ModemEvent]:
    """The capture, then a source that stays open, so the test decides when the
    run stops — after the writer has drained, not while a batch is in flight."""
    async for event in _events(PUBLIC_CAPTURE):
        yield event
    await asyncio.Event().wait()


async def _replay(persistence: Persistence, **config: object) -> Runtime:
    run = runtime(
        _replay_then_open(),
        out=io.StringIO(),
        persistence=persistence,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, replay=True, **config),  # type: ignore[arg-type]
    )
    task = asyncio.create_task(run.run())
    for _ in range(2000):
        if run.channels.decrypted == 33:
            break
        await asyncio.sleep(0.01)
    await persistence.channel_writer.wait_idle()
    run.stop()
    await asyncio.wait_for(task, 30)
    return run


@pytest.mark.database
async def test_a_replay_records_channel_messages_only_when_writes_are_enabled(
    database: Database,
) -> None:
    quiet = await _replay(Persistence(database=database, writes_enabled=False))
    assert quiet.channels.decrypted == 33
    assert await _channel_rows(database) == 0, "a replay recorded without --persist-replay"

    writing = await _replay(
        Persistence(database=database, writes_enabled=True), replay_persists=True
    )
    assert writing.channels.decrypted == 33
    assert await _channel_rows(database) == 33
