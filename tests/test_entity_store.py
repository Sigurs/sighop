"""The entity store and key sealing (section 4, design D4/D14).

Two rules are what these tests are really about, and both are §6's:

* a database dump must not be sufficient to impersonate a room server, and
* nothing — a listing, a log event, an error message — ever renders key material.

The second is easy to hold today and easy to lose in a year, so it is asserted
against the actual output rather than trusted to review.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import text

from sighop.config import (
    SECRET_KEY_VARIABLE,
    generate_secret_key,
)
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    MAX_ENTITY_NAME_BYTES,
    EntityKeyMismatchError,
    EntityNameError,
    EntityNameTakenError,
    EntityRepository,
    advert_config_for,
    parse_entity_name,
)
from sighop.db.sealing import (
    SEAL_VERSION,
    SealAuthenticationError,
    SealFormatError,
    SealRemovedFormatError,
    seal_private_key,
)
from sighop.db.sealing import open_private_key as unseal
from sighop.keystore import (
    EntityRegistry,
    NodeHashCollisionError,
    create_keyfile,
)
from sighop.net.adverts import FLOOD_INTERVAL_FLOOR_SECONDS, HOUR
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.payloads import (
    GROUP_NAME_SEPARATOR,
    MAX_ADVERT_DATA_SIZE,
    NodeType,
    build_appdata,
)
from sighop.runtime import RuntimeConfig
from tests.dbfixtures import strand_a_row
from tests.test_runtime import _events, runtime

SECRET = base64.b64decode(generate_secret_key())
OTHER_SECRET = base64.b64decode(generate_secret_key())


# --- 4.1 Sealing (design D4) ------------------------------------------------


def test_a_sealed_private_key_round_trips_and_is_not_the_key() -> None:
    identity = generate_identity()
    sealed = seal_private_key(identity.private_key, SECRET)
    assert sealed[0] == SEAL_VERSION
    assert identity.private_key not in sealed, "the plaintext key is in the stored value"
    assert unseal(sealed, SECRET) == identity.private_key


def test_the_wrong_secret_does_not_open_a_sealed_private_key() -> None:
    sealed = seal_private_key(generate_identity().private_key, SECRET)
    with pytest.raises(SealAuthenticationError) as excinfo:
        unseal(sealed, OTHER_SECRET, entity="entity 'roomy'")
    assert "entity 'roomy'" in str(excinfo.value)
    assert "Nothing was decrypted" in str(excinfo.value)


def test_flipping_one_ciphertext_byte_is_an_authentication_error() -> None:
    """Poly1305 is what makes a tampered row fail loudly rather than yield
    some other key — the property §6 asks for by name."""
    sealed = bytearray(seal_private_key(generate_identity().private_key, SECRET))
    sealed[-1] ^= 0x01
    with pytest.raises(SealAuthenticationError):
        unseal(bytes(sealed), SECRET)


def test_an_unknown_seal_version_is_corruption_not_a_wrong_secret() -> None:
    sealed = bytearray(seal_private_key(generate_identity().private_key, SECRET))
    sealed[0] = 0x7F
    with pytest.raises(SealFormatError) as excinfo:
        unseal(bytes(sealed), SECRET)
    assert "corrupt" in str(excinfo.value)


def test_a_truncated_sealed_value_is_corruption() -> None:
    with pytest.raises(SealFormatError):
        unseal(bytes([SEAL_VERSION]) + b"\x00" * 4, SECRET)


# --- 4.3 Generating the secret ----------------------------------------------


# --- An identity's name is validated wherever it is set ----------------------
#
# Two of these rules are not tidiness. A name holding `GROUP_NAME_SEPARATOR`
# could never post on a channel, and a name over `MAX_ENTITY_NAME_BYTES` could
# never advert. Both used to be reachable by creating an identity and only
# failed later, against the radio.


def test_an_empty_identity_name_is_refused() -> None:
    for empty in ("", "   ", "\t\n"):
        with pytest.raises(EntityNameError) as excinfo:
            parse_entity_name(empty)
        assert "cannot be empty" in str(excinfo.value)


def test_an_identity_name_is_stripped_of_surrounding_whitespace() -> None:
    assert parse_entity_name("  roomy  ") == "roomy"


def test_an_identity_name_that_could_never_advert_is_refused() -> None:
    """`build_appdata` packs the name last into MAX_ADVERT_DATA_SIZE bytes."""
    assert parse_entity_name("a" * MAX_ENTITY_NAME_BYTES)
    with pytest.raises(EntityNameError) as excinfo:
        parse_entity_name("a" * (MAX_ENTITY_NAME_BYTES + 1))
    assert str(MAX_ENTITY_NAME_BYTES) in str(excinfo.value)


def test_the_identity_name_bound_is_counted_in_bytes_and_not_characters() -> None:
    """A name of legal length in characters can still overflow the appdata."""
    name = "é" * MAX_ENTITY_NAME_BYTES
    assert len(name) == MAX_ENTITY_NAME_BYTES
    with pytest.raises(EntityNameError):
        parse_entity_name(name)


def test_a_name_the_bound_accepts_always_builds_appdata_with_a_location() -> None:
    """The point of the bound: the worst case still encodes (design D4)."""
    appdata = build_appdata(
        NodeType.CHAT,
        latitude=1,
        longitude=2,
        name="a" * MAX_ENTITY_NAME_BYTES,
    )
    assert len(appdata) <= MAX_ADVERT_DATA_SIZE


def test_an_identity_name_carrying_the_channel_separator_is_refused() -> None:
    """`check_sender_name` refuses this at post time; refuse it at the source."""
    with pytest.raises(EntityNameError) as excinfo:
        parse_entity_name(f"bot{GROUP_NAME_SEPARATOR}one")
    assert repr(GROUP_NAME_SEPARATOR) in str(excinfo.value)
    assert "read as the message" in str(excinfo.value)


def test_an_identity_name_with_a_control_character_is_refused() -> None:
    with pytest.raises(EntityNameError) as excinfo:
        parse_entity_name("roo\x01my")
    assert "U+0001" in str(excinfo.value)


async def test_creating_an_identity_applies_the_name_rules(database: Database) -> None:
    store = EntityRepository(database=database)
    with pytest.raises(EntityNameError):
        await store.store(name="   ", identity=generate_identity(), secret=SECRET)
    with pytest.raises(EntityNameError):
        await store.store(name="a: b", identity=generate_identity(), secret=SECRET)
    listed = await store.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value == [], "a refused name stored a row"


async def test_a_created_identity_is_stored_under_the_stripped_name(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    stored = await store.store(name="  roomy  ", identity=generate_identity(), secret=SECRET)
    assert isinstance(stored, Succeeded)
    assert stored.value.name == "roomy"


# --- 4.4 / 4.5 The entity repository ----------------------------------------


async def test_an_identity_round_trips_with_the_same_public_key_and_node_hash(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    stored = await store.store(
        name="roomy", identity=identity, secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    assert isinstance(stored, Succeeded)
    assert stored.value.type == "room_server"

    loaded = await store.load_all(SECRET)
    assert isinstance(loaded, Succeeded)
    (entity,) = loaded.value
    assert entity.public_key == identity.public_key
    assert entity.node_hash == identity.node_hash
    assert entity.identity.private_key == identity.private_key
    assert entity.record.advert_config == advert_config_for(NodeType.ROOM_SERVER)
    assert entity.record.enabled is True


@pytest.mark.usefixtures("default_persistence")
async def test_a_stored_identity_with_no_interval_adverts_at_the_floor(
    database: Database,
) -> None:
    """The migration-free claim (design.md — Risks): no surface has ever
    written a non-default interval, so a row with none set must advert
    exactly as it did before `_adopt_entity` read `advert_config` at all."""
    store = EntityRepository(database=database)
    stored = await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    assert isinstance(stored, Succeeded)
    assert stored.value.advert_config["flood_interval_seconds"] is None

    loaded = await store.load_all(SECRET)
    assert isinstance(loaded, Succeeded)

    run = runtime(
        _events(),
        config=RuntimeConfig(
            stored_entities=tuple(loaded.value), status_interval=3600, advert_tick=3600
        ),
    )

    (stub,) = run.adverts.stubs
    assert stub.flood_interval_seconds == FLOOD_INTERVAL_FLOOR_SECONDS


@pytest.mark.usefixtures("default_persistence")
async def test_a_stored_identitys_configured_interval_is_honoured(database: Database) -> None:
    store = EntityRepository(database=database)
    stored = await store.store(
        name="roomy",
        identity=generate_identity(),
        secret=SECRET,
        advert_config=advert_config_for(
            NodeType.CHAT, flood_interval_seconds=48 * HOUR, zero_hop_interval_seconds=600.0
        ),
    )
    assert isinstance(stored, Succeeded)

    loaded = await store.load_all(SECRET)
    assert isinstance(loaded, Succeeded)

    run = runtime(
        _events(),
        config=RuntimeConfig(
            stored_entities=tuple(loaded.value), status_interval=3600, advert_tick=3600
        ),
    )

    (stub,) = run.adverts.stubs
    assert stub.flood_interval_seconds == 48 * HOUR
    assert stub.zero_hop_interval_seconds == 600.0


async def test_the_stored_column_holds_ciphertext_and_not_the_private_key(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    await store.store(name="roomy", identity=identity, secret=SECRET)
    async with database.sessions() as session:
        sealed = (await session.execute(text("SELECT sealed_private_key FROM entity"))).scalar_one()
    assert bytes(sealed) != identity.private_key
    assert identity.private_key not in bytes(sealed)
    assert identity.private_scalar not in bytes(sealed)


async def test_a_corrupted_public_key_column_fails_naming_the_entity(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    await store.store(name="roomy", identity=identity, secret=SECRET)
    async with database.sessions() as session:
        await session.execute(text("UPDATE entity SET public_key = :key"), {"key": b"\x07" * 32})
        await session.commit()

    with pytest.raises(EntityKeyMismatchError) as excinfo:
        await store.load_all(SECRET)
    message = str(excinfo.value)
    assert "roomy" in message
    assert "refusing to prefer either value" in message
    assert identity.private_key.hex() not in message


async def test_a_wrong_secret_names_the_entity_and_produces_no_key(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    with pytest.raises(SealAuthenticationError) as excinfo:
        await store.load_all(OTHER_SECRET)
    assert "roomy" in str(excinfo.value)


async def test_a_disabled_entity_is_still_listed_but_not_loaded_as_originating(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    await store.store(name="retired", identity=identity, secret=SECRET, enabled=False)

    listed = await store.list_all()
    assert isinstance(listed, Succeeded)
    assert [record.name for record in listed.value] == ["retired"]
    assert listed.value[0].enabled is False

    enabled = await store.load_all(SECRET, enabled_only=True)
    assert isinstance(enabled, Succeeded)
    assert enabled.value == []


# --- Rows left behind by migration 0008 -------------------------------------
#
# A row sealing a 32-byte seed. It decrypts under the right secret; what it
# holds is a representation this build dropped, and saying "corrupt" would send
# an operator after a database fault that is not there.


async def _strand_a_row(database: Database, name: str) -> None:
    await strand_a_row(database, name, SECRET)


async def test_a_row_under_the_removed_seed_format_is_refused_by_name(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    await _strand_a_row(database, "roomy")

    with pytest.raises(SealRemovedFormatError) as excinfo:
        await store.load_all(SECRET)

    message = str(excinfo.value)
    assert "roomy" in message
    assert "32-byte seed" in message
    assert "intact" in message
    assert "corrupt" not in message, "the row is not corrupt and must not be called that"


async def test_a_stranded_row_is_left_exactly_as_it_was(database: Database) -> None:
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    await _strand_a_row(database, "roomy")
    async with database.sessions() as session:
        before = bytes(
            (await session.execute(text("SELECT sealed_private_key FROM entity"))).scalar_one()
        )

    with pytest.raises(SealRemovedFormatError):
        await store.load_all(SECRET)

    async with database.sessions() as session:
        after = bytes(
            (await session.execute(text("SELECT sealed_private_key FROM entity"))).scalar_one()
        )
    assert after == before, "loading rewrote the row it refused"


async def test_a_stranded_row_does_not_stop_the_other_identities_loading(
    database: Database,
) -> None:
    """The recovery order must not matter (`load_openable`)."""
    store = EntityRepository(database=database)
    good = generate_identity()
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    await store.store(name="still-here", identity=good, secret=SECRET)
    await _strand_a_row(database, "roomy")

    loaded = await store.load_openable(SECRET)

    assert isinstance(loaded, Succeeded)
    assert [entity.name for entity in loaded.value.opened] == ["still-here"]
    assert loaded.value.opened[0].identity.private_key == good.private_key
    assert len(loaded.value.stranded) == 1
    assert "roomy" in loaded.value.stranded[0]
    assert "no command converts one" in loaded.value.stranded[0]


async def test_the_tolerant_load_still_raises_for_a_wrong_secret(
    database: Database,
) -> None:
    """Only the removed format is set aside. A bad store is still a bad store."""
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)

    with pytest.raises(SealAuthenticationError):
        await store.load_openable(OTHER_SECRET)


async def test_the_tolerant_load_still_raises_for_a_mismatched_public_key(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    async with database.sessions() as session:
        await session.execute(text("UPDATE entity SET public_key = :key"), {"key": b"\x07" * 32})
        await session.commit()

    with pytest.raises(EntityKeyMismatchError):
        await store.load_openable(SECRET)


# --- Renaming a stored identity ----------------------------------------------


async def _one_row(database: Database) -> dict[str, object]:
    """Every column of the single stored entity, straight from the table."""
    async with database.sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT name, public_key, node_hash, sealed_private_key, type, "
                    "advert_config, enabled, created_at FROM entity"
                )
            )
        ).mappings()
        return dict(row.one())


async def test_a_rename_changes_the_name_and_nothing_else(database: Database) -> None:
    """What makes this identity *that* identity is untouched by a rename."""
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    before = await _one_row(database)

    key = before["public_key"]
    assert isinstance(key, bytes)
    renamed = await store.rename(key, "the-lobby")

    assert isinstance(renamed, Succeeded)
    assert renamed.value == "roomy", "the previous name was not reported"
    after = await _one_row(database)
    assert after["name"] == "the-lobby"
    for column in ("public_key", "node_hash", "sealed_private_key", "type", "created_at"):
        assert after[column] == before[column], f"{column} changed across a rename"


async def test_a_rename_needs_no_secret_because_it_opens_nothing(
    database: Database,
) -> None:
    """entity-store: "A rename reads no key material"."""
    store = EntityRepository(database=database)
    stored = await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    assert isinstance(stored, Succeeded)

    # No secret is passed, and none is reachable: the call takes none.
    renamed = await store.rename(stored.value.public_key, "still-here")

    assert isinstance(renamed, Succeeded)
    opened = await store.load_all(SECRET)
    assert isinstance(opened, Succeeded)
    assert opened.value[0].name == "still-here"
    assert opened.value[0].public_key == stored.value.public_key


async def test_a_rename_applies_the_name_rules(database: Database) -> None:
    store = EntityRepository(database=database)
    stored = await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    assert isinstance(stored, Succeeded)

    for refused in ("  ", "a: b", "n" * (MAX_ENTITY_NAME_BYTES + 1), "ro\x01my"):
        with pytest.raises(EntityNameError):
            await store.rename(stored.value.public_key, refused)

    listed = await store.list_all()
    assert [record.name for record in listed.value] == ["roomy"]


async def test_a_rename_onto_another_identitys_name_is_refused(database: Database) -> None:
    store = EntityRepository(database=database)
    first = await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    await store.store(name="greeter", identity=generate_identity(), secret=SECRET)
    assert isinstance(first, Succeeded)

    with pytest.raises(EntityNameTakenError) as excinfo:
        await store.rename(first.value.public_key, "greeter")

    assert "greeter" in str(excinfo.value)
    assert "nothing was renamed" in str(excinfo.value)
    listed = await store.list_all()
    assert sorted(record.name for record in listed.value) == ["greeter", "roomy"]


async def test_renaming_to_the_name_already_held_is_accepted(database: Database) -> None:
    """What a resubmitted form does. Refusing it would be a false negative."""
    store = EntityRepository(database=database)
    stored = await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    assert isinstance(stored, Succeeded)

    renamed = await store.rename(stored.value.public_key, "  roomy  ")

    assert isinstance(renamed, Succeeded)
    listed = await store.list_all()
    assert [record.name for record in listed.value] == ["roomy"]


async def test_renaming_an_identity_that_is_not_stored_reports_so(
    database: Database,
) -> None:
    renamed = await EntityRepository(database=database).rename(
        generate_identity().public_key, "nobody"
    )
    assert isinstance(renamed, Succeeded)
    assert renamed.value is None


async def test_a_rename_leaves_a_bound_room_bound(database: Database) -> None:
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="roomy", identity=generate_identity(), secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    assert isinstance(stored, Succeeded)
    created = await persistence.rooms.create(
        entity_id=stored.value.id, name="the-room", admin_password_hash="x" * 60
    )
    assert isinstance(created, Succeeded)

    await persistence.entities.rename(stored.value.public_key, "renamed")

    bound = await persistence.rooms.get_for_entity(stored.value.id)
    assert isinstance(bound, Succeeded)
    assert bound.value is not None
    assert bound.value.name == "the-room", "the room followed its identity's rename"


# --- Removing a stored identity (design D10) --------------------------------
#
# The only destructive action on `sighop keys`, added because a row stranded by
# migration 0008 blocks its own replacement: the public key it still holds makes
# `store()` refuse the same identity supplied as a private key.


async def test_remove_deletes_one_row_and_leaves_the_others(database: Database) -> None:
    store = EntityRepository(database=database)
    doomed = generate_identity()
    kept = generate_identity()
    await store.store(name="doomed", identity=doomed, secret=SECRET)
    await store.store(name="kept", identity=kept, secret=SECRET)

    removed = await store.remove(doomed.public_key)

    assert isinstance(removed, Succeeded)
    assert removed.value is True
    listed = await store.list_all()
    assert [record.name for record in listed.value] == ["kept"]


async def test_removing_an_identity_that_is_not_there_says_so(database: Database) -> None:
    store = EntityRepository(database=database)

    removed = await store.remove(generate_identity().public_key)

    assert isinstance(removed, Succeeded)
    assert removed.value is False


async def test_bound_to_reports_a_room_and_a_bot(database: Database) -> None:
    """What would be deleted with the identity, because the keys cascade."""
    persistence = Persistence(database=database)
    store = persistence.entities
    server = generate_identity()
    stored = await store.store(
        name="roomy", identity=server, secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    await persistence.rooms.create(
        name="the-room", entity_id=stored.value.id, admin_password_hash="x" * 60
    )

    bot_identity = generate_identity()
    bot_entity = await store.store(
        name="greeter-identity",
        identity=bot_identity,
        secret=SECRET,
        entity_type="bot",
        advert_config=advert_config_for(NodeType.CHAT),
    )
    await persistence.bots.create(entity_id=bot_entity.value.id, driver="greeter", config={})

    bound_room = await store.bound_to(stored.value.id)
    bound_bot = await store.bound_to(bot_entity.value.id)

    assert isinstance(bound_room, Succeeded)
    assert bound_room.value == ["room 'the-room'"]
    assert isinstance(bound_bot, Succeeded)
    assert bound_bot.value == ["bot 'greeter'"]


async def test_bound_to_is_empty_for_an_unbound_identity(database: Database) -> None:
    store = EntityRepository(database=database)
    stored = await store.store(name="free", identity=generate_identity(), secret=SECRET)

    bound = await store.bound_to(stored.value.id)

    assert isinstance(bound, Succeeded)
    assert bound.value == []


# --- 4.6 Collisions across sources (design D14) -----------------------------


def _colliding_identities() -> tuple[LocalIdentity, LocalIdentity]:
    first = generate_identity()
    while True:
        second = generate_identity()
        if second.node_hash == first.node_hash:
            return first, second


def test_two_stored_entities_that_collide_fail_naming_both_and_the_hash() -> None:
    first, second = _colliding_identities()
    registry = EntityRegistry()
    registry.add_stored("one", first)
    with pytest.raises(NodeHashCollisionError) as excinfo:
        registry.add_stored("two", second)
    message = str(excinfo.value)
    assert "'one'" in message and "'two'" in message
    assert "the entity store" in message
    assert f"0x{first.node_hash:02x}" in message


def test_a_keyfile_colliding_with_a_stored_entity_fails_naming_both(tmp_path) -> None:
    stored_identity, keyfile_identity = _colliding_identities()
    path = tmp_path / "collides.json"
    create_keyfile(path, "from-file", identity=keyfile_identity)

    registry = EntityRegistry()
    registry.add_stored("from-store", stored_identity)
    with pytest.raises(NodeHashCollisionError) as excinfo:
        registry.load(path)
    message = str(excinfo.value)
    assert "from-store" in message
    assert str(path) in message
    assert f"0x{stored_identity.node_hash:02x}" in message


def test_generation_avoids_a_stored_entity_node_hash(tmp_path) -> None:
    registry = EntityRegistry()
    stored = registry.add_stored("from-store", generate_identity())
    for index in range(12):
        created = registry.create(tmp_path / f"gen-{index}.json", f"gen-{index}")
        assert created.node_hash != stored.node_hash


def test_a_registry_entity_reports_the_source_it_came_from(tmp_path) -> None:
    registry = EntityRegistry()
    stored = registry.add_stored("from-store", generate_identity())
    # Two random identities share a node hash 1 time in 256, and loading the
    # second would then (correctly) fail the collision rule — so the keyfile's
    # identity is drawn until its first byte differs, on purpose.
    other = generate_identity()
    while other.node_hash == stored.node_hash:
        other = generate_identity()
    keyfile = create_keyfile(tmp_path / "one.json", "from-file", identity=other)
    from_file = registry.load(keyfile.path)
    assert stored.source == "the entity store"
    assert stored.from_store
    assert from_file.source == str(tmp_path / "one.json")
    assert not from_file.from_store


# --- 4.9 Nothing secret reaches a log event ---------------------------------


async def test_an_entity_load_failure_logs_neither_the_seed_nor_the_secret(
    database: Database,
) -> None:
    events: list[dict[str, object]] = []

    class Recorder:
        def error(self, event: str, **fields: object) -> None:
            events.append({"event": event, **fields})

        def info(self, event: str, **fields: object) -> None:
            events.append({"event": event, **fields})

    database.logger = Recorder()
    store = EntityRepository(database=database)
    identity = generate_identity()
    await store.store(name="roomy", identity=identity, secret=SECRET)

    with pytest.raises(SealAuthenticationError) as excinfo:
        await store.load_all(OTHER_SECRET)

    rendered = repr(events) + str(excinfo.value)
    assert identity.private_key.hex() not in rendered
    assert base64.b64encode(SECRET).decode() not in rendered
    assert base64.b64encode(OTHER_SECRET).decode() not in rendered


# --- 8.9 Nothing secret in a record, and one way to read the secret ---------


def test_the_stored_entity_record_carries_nothing_secret() -> None:
    from sighop.db.repositories import EntityRecord

    identity = generate_identity()
    record = EntityRecord(
        id=uuid.uuid4(),
        type="companion",
        name="roomy",
        public_key=identity.public_key,
        node_hash=identity.node_hash,
        advert_config=advert_config_for(NodeType.CHAT),
        enabled=True,
        created_at=dt.datetime.now(dt.UTC),
    )
    rendered = repr(record) + repr(record.as_json())
    assert identity.private_key.hex() not in rendered
    assert "sealed" not in rendered
    assert os.environ.get(SECRET_KEY_VARIABLE, "\0") not in rendered


def test_no_command_reads_the_secret_key_around_config() -> None:
    """Statically: every read of the secret goes through `Config`."""
    import ast
    from pathlib import Path

    import sighop

    offenders = []
    for path in sorted(Path(sighop.__file__).parent.rglob("*.py")):
        if path.name == "config.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "environ":
                source = ast.get_source_segment(path.read_text(), node) or ""
                offenders.append(f"{path.name}:{node.lineno}: {source}")
    allowed = {"migrations.py", "logging.py"}
    assert [entry for entry in offenders if entry.split(":")[0] not in allowed] == []
