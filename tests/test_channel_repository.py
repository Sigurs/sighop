"""`ChannelRepository` and `ChannelMessageRepository` against a real server (tasks 5.2, 5.3)."""

from __future__ import annotations

import base64
import datetime as dt

import pytest
from sqlalchemy import select

from sighop.db.engine import Database, Failed, Succeeded
from sighop.db.models import Channel as ChannelRow
from sighop.db.repositories import (
    ChannelConfigError,
    ChannelExistsError,
    ChannelMessageRepository,
    ChannelRepository,
)
from sighop.net.channels import ChannelKind, ChannelMessageRecord, ChannelOutcome
from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, ChannelKey, channel_key_from_hashtag

SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(1, 33))
KEY = bytes(range(100, 116))
KEY_B64 = base64.b64encode(KEY).decode()
NOW = dt.datetime(2026, 9, 14, 18, 0, tzinfo=dt.UTC)


def _value[T](outcome: Succeeded[T] | Failed) -> T:
    assert isinstance(outcome, Succeeded), outcome
    return outcome.value


# --- Stored rules (no server needed) -----------------------------------------


@pytest.mark.parametrize("length", [8, 24, 33])
async def test_a_psk_of_the_wrong_length_is_refused_naming_the_length(length: int) -> None:
    from sighop.db.repositories import parse_psk

    with pytest.raises(ChannelConfigError, match=f"decodes to {length} bytes"):
        parse_psk(base64.b64encode(bytes(length)).decode())


def test_a_psk_that_is_not_base64_is_refused_without_echoing_it() -> None:
    from sighop.db.repositories import parse_psk

    with pytest.raises(ChannelConfigError, match="does not decode as base64") as refused:
        parse_psk("not*base64!!")
    assert "not*base64" not in str(refused.value)


def test_a_hashtag_gains_its_leading_hash() -> None:
    from sighop.db.repositories import parse_hashtag

    assert parse_hashtag("dev-sighop") == "#dev-sighop"
    with pytest.raises(ChannelConfigError):
        parse_hashtag("#")


# --- ChannelRepository ------------------------------------------------------


async def test_a_hashtag_channel_is_stored_with_its_derived_hash(database: Database) -> None:
    channels = ChannelRepository(database=database)

    record = _value(await channels.add_hashtag("#dev-sighop"))

    assert record.kind is ChannelKind.HASHTAG and record.name == "#dev-sighop"
    assert record.channel_hash == channel_key_from_hashtag("#dev-sighop").channel_hash
    assert record.guessable


async def test_a_psk_is_sealed_and_its_column_holds_no_key_bytes(database: Database) -> None:
    channels = ChannelRepository(database=database)

    record = _value(await channels.add_psk(KEY_B64, name="private", secret=SECRET))

    assert record.kind is ChannelKind.PSK and not record.guessable
    async with database.sessions() as session:
        row = (
            await session.execute(select(ChannelRow).where(ChannelRow.name == "private"))
        ).scalar_one()
    assert row.sealed_key is not None and row.sealed_key[0] == 2
    assert KEY not in bytes(row.sealed_key) and KEY_B64.encode() not in bytes(row.sealed_key)
    assert row.hashtag is None
    assert "private" in repr(record) and KEY_B64 not in repr(record)


async def test_the_same_key_under_another_name_is_refused_naming_the_existing(
    database: Database,
) -> None:
    channels = ChannelRepository(database=database)
    await channels.add_hashtag("#dev-sighop")
    derived = base64.b64encode(channel_key_from_hashtag("#dev-sighop").key).decode()

    with pytest.raises(ChannelExistsError, match="'#dev-sighop'"):
        await channels.add_psk(derived, name="sneaky", secret=SECRET)
    with pytest.raises(ChannelExistsError, match="'Public'"):
        await channels.add_psk(
            base64.b64encode(PUBLIC_CHANNEL_KEY.key).decode(), name="p2", secret=SECRET
        )
    await channels.add_psk(KEY_B64, name="private", secret=SECRET)
    with pytest.raises(ChannelExistsError, match="'private'"):
        await channels.add_psk(KEY_B64, name="private-2", secret=SECRET)
    names = [record.name for record in _value(await channels.list_all())]
    assert names == ["Public", "#dev-sighop", "private"]


async def test_a_name_in_use_is_refused(database: Database) -> None:
    channels = ChannelRepository(database=database)
    with pytest.raises(ChannelExistsError, match="named 'Public' already exists"):
        await channels.add_hashtag("#other", name="Public")


async def test_a_different_key_with_the_same_hash_is_accepted(database: Database) -> None:
    channels = ChannelRepository(database=database)
    twin = next(
        ChannelKey(key=seed.to_bytes(16, "little"))
        for seed in range(100_000)
        if ChannelKey(key=seed.to_bytes(16, "little")).channel_hash == 0x11
    )

    record = _value(
        await channels.add_psk(base64.b64encode(twin.key).decode(), name="twin", secret=SECRET)
    )

    assert record.channel_hash == 0x11


async def test_load_keys_skips_an_unsealable_row_by_name(database: Database) -> None:
    channels = ChannelRepository(database=database)
    await channels.add_hashtag("#dev-sighop")
    await channels.add_psk(KEY_B64, name="private", secret=SECRET)

    good = _value(await channels.load_keys(SECRET))
    wrong = _value(await channels.load_keys(OTHER_SECRET))
    none = _value(await channels.load_keys(None))

    assert [c.name for c in good] == ["Public", "#dev-sighop", "private"]
    assert good.by_id(3) is not None and good.by_id(3).key.key == KEY
    assert [c.name for c in wrong] == ["Public", "#dev-sighop"] and wrong.skipped == ("private",)
    assert none.skipped == ("private",)


def _message(channel_id: int, ref: str, **fields: object) -> ChannelMessageRecord:
    values: dict[str, object] = {
        "channel_id": channel_id,
        "direction": "in",
        "ref": ref,
        "text": b"hi",
        "wire_timestamp": 1_757_000_000,
        "handled_at": NOW,
        "outcome": ChannelOutcome.RECEIVED,
        "unverified_sender_name": "alice",
    }
    values.update(fields)
    return ChannelMessageRecord(**values)  # type: ignore[arg-type]


async def test_removal_cascades_and_reports_the_count(database: Database) -> None:
    channels = ChannelRepository(database=database)
    history = ChannelMessageRepository(database=database)
    record = _value(await channels.add_hashtag("#dev-sighop"))
    _value(await history.upsert_many([_message(record.id, f"p{i}") for i in range(40)]))

    assert _value(await channels.message_count(record.id)) == 40
    assert _value(await channels.message_counts()) == {record.id: 40}
    assert _value(await channels.remove(record.id)) == 40
    assert _value(await channels.get("#dev-sighop")) is None
    assert _value(await history.count()) == 0
    assert _value(await channels.remove(record.id)) is None


async def test_removing_public_is_not_undone(database: Database) -> None:
    channels = ChannelRepository(database=database)
    public = _value(await channels.get("Public"))
    assert public is not None
    _value(await channels.remove(public.id))
    assert [c.name for c in _value(await channels.load_keys(None))] == []
    _value(await channels.add_public())
    assert [c.name for c in _value(await channels.list_all())] == ["Public"]


# --- ChannelMessageRepository (task 5.3) ------------------------------------


async def test_a_post_is_updated_in_place(database: Database) -> None:
    history = ChannelMessageRepository(database=database)
    post = _message(
        1,
        "post1",
        direction="out",
        unverified_sender_name=None,
        entity_public_key=b"\x01" * 32,
        outcome=ChannelOutcome.AWAITING,
    )
    _value(await history.upsert(post))
    from dataclasses import replace

    _value(
        await history.upsert_many(
            [
                replace(post, outcome=ChannelOutcome.TRANSMITTED, packet_id="pk"),
                replace(post, outcome=ChannelOutcome.TRANSMITTED, packet_id="pk", repeats_heard=2),
            ]
        )
    )

    [stored] = _value(await history.recent(1))
    assert stored.outcome is ChannelOutcome.TRANSMITTED
    assert stored.repeats_heard == 2 and stored.packet_id == "pk"
    assert stored.entity_public_key == b"\x01" * 32


async def test_a_received_message_gains_paths_in_place(database: Database) -> None:
    from dataclasses import replace

    history = ChannelMessageRepository(database=database)
    first = _message(
        1, "rx1", hop_count=3, paths=(bytes.fromhex("a3f128c09e4b"),), path_hash_size=2
    )
    _value(await history.upsert(first))
    both = replace(first, paths=(bytes.fromhex("a3f128c09e4b"), b""))
    _value(await history.upsert(both))
    _value(await history.upsert(first))  # a stale offer takes nothing back

    [stored] = _value(await history.recent(1))
    assert stored.ref == "rx1" and stored.hop_count == 3
    assert stored.paths == (bytes.fromhex("a3f128c09e4b"), b"")
    assert stored.path_hash_size == 2


async def test_a_message_without_paths_reads_back_as_none(database: Database) -> None:
    history = ChannelMessageRepository(database=database)
    _value(await history.upsert(_message(1, "old", hop_count=2)))

    [stored] = _value(await history.recent(1))
    assert stored.paths is None and stored.path_hash_size is None and stored.hop_count == 2


async def test_history_is_ordered_by_handled_time_not_wire_time(database: Database) -> None:
    history = ChannelMessageRepository(database=database)
    _value(await history.upsert(_message(1, "first", wire_timestamp=1_757_000_000)))
    _value(
        await history.upsert(
            _message(
                1,
                "second",
                wire_timestamp=1_757_000_000 - 86_400,
                handled_at=NOW + dt.timedelta(seconds=5),
            )
        )
    )

    assert [r.ref for r in _value(await history.recent(1))] == ["second", "first"]
    assert [r.ref for r in _value(await history.recent(1, limit=1))] == ["second"]


async def test_startup_rewrites_awaiting_posts_as_unknown(database: Database) -> None:
    history = ChannelMessageRepository(database=database)
    _value(
        await history.upsert(
            _message(
                1,
                "post",
                direction="out",
                unverified_sender_name=None,
                entity_public_key=b"\x02" * 32,
                outcome=ChannelOutcome.AWAITING,
            )
        )
    )
    _value(await history.upsert(_message(1, "rx")))

    assert _value(await history.mark_awaiting_unknown()) == 1
    outcomes = {r.ref: r.outcome for r in _value(await history.recent(1))}
    assert outcomes == {"post": ChannelOutcome.UNKNOWN, "rx": ChannelOutcome.RECEIVED}


# --- The writer lane (task 5.3) ---------------------------------------------


def test_the_channel_lane_refuses_when_full_and_counts_it() -> None:
    from sighop.db.persistence import CHANNEL_MESSAGE_QUEUE_CAPACITY, Persistence
    from tests.test_dm_repository import _unopened

    persistence = Persistence(database=_unopened())
    writer = persistence.channel_writer
    assert writer.drop_oldest is False

    assert all(writer.offer(_message(1, f"p{i}")) for i in range(writer.capacity))
    assert writer.offer(_message(1, "one-too-many")) is False
    assert writer.refused == 1 and writer.overflowed == 0
    assert writer.pending == CHANNEL_MESSAGE_QUEUE_CAPACITY
    status = persistence.as_json()
    assert status["channel_messages_refused"] == 1
    assert "channel_messages_written" in status and "channel_messages_discarded" in status


def test_a_replay_run_has_no_channel_sink() -> None:
    from sighop.db.persistence import Persistence
    from tests.test_dm_repository import _unopened

    assert Persistence(database=_unopened(), writes_enabled=False).channel_sink() is None
    writing = Persistence(database=_unopened())
    assert writing.channel_sink() is writing.channel_writer


async def test_restore_rewrites_awaiting_posts_and_counts_history(database: Database) -> None:
    from sighop.db.persistence import Persistence
    from sighop.net.contacts import ContactStore
    from sighop.net.paths import PathStore

    history = ChannelMessageRepository(database=database)
    _value(
        await history.upsert(
            _message(
                1,
                "post",
                direction="out",
                unverified_sender_name=None,
                entity_public_key=b"\x02" * 32,
                outcome=ChannelOutcome.AWAITING,
            )
        )
    )
    persistence = Persistence(database=database)

    restored = await persistence.restore(ContactStore(), PathStore())

    assert restored.channel_messages == 1 and restored.channel_posts_unknown == 1


# --- Renaming a channel ------------------------------------------------------


async def test_renaming_a_psk_channel_changes_no_key_material(database: Database) -> None:
    """The name is a label; the key is in its own sealed column."""
    channels = ChannelRepository(database=database)
    created = _value(await channels.add_psk(KEY_B64, name="private", secret=SECRET))
    async with database.sessions() as session:
        sealed = (
            (await session.execute(select(ChannelRow).where(ChannelRow.id == created.id)))
            .scalar_one()
            .sealed_key
        )
    assert sealed is not None
    before = bytes(sealed)

    renamed = await channels.rename(created.id, "operations")

    assert isinstance(renamed, Succeeded)
    assert renamed.value == "private", "the previous name was not reported"
    async with database.sessions() as session:
        row = (
            await session.execute(select(ChannelRow).where(ChannelRow.id == created.id))
        ).scalar_one()
    assert row.name == "operations"
    assert row.sealed_key is not None
    assert bytes(row.sealed_key) == before, "the sealed key changed across a rename"
    assert row.channel_hash == created.channel_hash
    assert row.kind == str(ChannelKind.PSK)


async def test_renaming_a_hashtag_channel_leaves_its_hashtag_and_hash(
    database: Database,
) -> None:
    """A hashtag channel derives its key from the hashtag, never from the name."""
    channels = ChannelRepository(database=database)
    created = _value(await channels.add_hashtag("#dev-sighop"))

    renamed = await channels.rename(created.id, "the dev channel")

    assert isinstance(renamed, Succeeded)
    async with database.sessions() as session:
        row = (
            await session.execute(select(ChannelRow).where(ChannelRow.id == created.id))
        ).scalar_one()
    assert row.name == "the dev channel"
    assert row.hashtag == "#dev-sighop"
    assert row.channel_hash == channel_key_from_hashtag("#dev-sighop").channel_hash


async def test_a_renamed_channel_still_opens_under_the_same_key(database: Database) -> None:
    channels = ChannelRepository(database=database)
    created = _value(await channels.add_psk(KEY_B64, name="private", secret=SECRET))
    before = _value(await channels.load_keys(SECRET)).by_hash(created.channel_hash)

    await channels.rename(created.id, "operations")

    after = _value(await channels.load_keys(SECRET)).by_hash(created.channel_hash)
    assert [loaded.key.key for loaded in after] == [loaded.key.key for loaded in before]


async def test_renaming_a_channel_onto_a_name_in_use_is_refused(database: Database) -> None:
    channels = ChannelRepository(database=database)
    created = _value(await channels.add_psk(KEY_B64, name="private", secret=SECRET))

    with pytest.raises(ChannelExistsError, match="named 'Public' already exists"):
        await channels.rename(created.id, "Public")

    names = [record.name for record in _value(await channels.list_all())]
    assert names == ["Public", "private"]


async def test_renaming_a_channel_applies_the_name_rules(database: Database) -> None:
    channels = ChannelRepository(database=database)
    created = _value(await channels.add_psk(KEY_B64, name="private", secret=SECRET))

    for refused in ("  ", "a\x01b"):
        with pytest.raises(ChannelConfigError):
            await channels.rename(created.id, refused)

    names = [record.name for record in _value(await channels.list_all())]
    assert names == ["Public", "private"]


async def test_renaming_a_channel_that_does_not_exist_reports_so(database: Database) -> None:
    renamed = await ChannelRepository(database=database).rename(9999, "nobody")
    assert isinstance(renamed, Succeeded)
    assert renamed.value is None
