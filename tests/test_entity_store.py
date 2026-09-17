"""The entity store and key sealing (section 4, design D4/D14).

Two rules are what these tests are really about, and both are §6's:

* a database dump must not be sufficient to impersonate a room server, and
* nothing — a listing, a log event, an error message — ever renders key material.

The second is easy to hold today and easy to lose in a year, so it is asserted
against the actual output rather than trusted to review.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import os
import stat
import uuid

import pytest
from sqlalchemy import text

from sighop.cli import main
from sighop.config import (
    DATABASE_SCHEMA_VARIABLE,
    SECRET_KEY_SIZE,
    SECRET_KEY_VARIABLE,
    DatabaseConfig,
    generate_secret_key,
    parse_secret_key,
)
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    EntityKeyMismatchError,
    EntityRepository,
    advert_config_for,
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
    PLAINTEXT_KEY_NOTICE,
    EntityRegistry,
    NodeHashCollisionError,
    create_keyfile,
)
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.payloads import NodeType

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


def test_keys_secret_prints_a_thirty_two_byte_secret_and_the_warning() -> None:
    out = io.StringIO()
    assert main(["keys", "secret"], out=out) == 0
    printed = out.getvalue()
    value = printed.split("SIGHOP_SECRET_KEY=", 1)[1].splitlines()[0]
    assert len(parse_secret_key(value)) == SECRET_KEY_SIZE
    assert "unrecoverable" in printed
    assert "printed once" in printed


# --- 4.4 / 4.5 The entity repository ----------------------------------------


@pytest.mark.database
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


@pytest.mark.database
async def test_the_stored_column_holds_ciphertext_and_not_the_private_key(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    await store.store(name="roomy", identity=identity, secret=SECRET)
    async with database.sessions() as session:
        sealed = (
            await session.execute(text("SELECT sealed_private_key FROM entity"))
        ).scalar_one()
    assert bytes(sealed) != identity.private_key
    assert identity.private_key not in bytes(sealed)
    assert identity.private_scalar not in bytes(sealed)


@pytest.mark.database
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


@pytest.mark.database
async def test_a_wrong_secret_names_the_entity_and_produces_no_key(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    with pytest.raises(SealAuthenticationError) as excinfo:
        await store.load_all(OTHER_SECRET)
    assert "roomy" in str(excinfo.value)


@pytest.mark.database
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
    """Rewrite one row's key material as a pre-0008 build sealed it."""
    from nacl.secret import SecretBox

    from sighop.db.sealing import SEAL_VERSION

    sealed = bytes([SEAL_VERSION]) + bytes(SecretBox(SECRET).encrypt(os.urandom(32)))
    async with database.sessions() as session:
        await session.execute(
            text("UPDATE entity SET sealed_private_key = :sealed WHERE name = :name"),
            {"sealed": sealed, "name": name},
        )
        await session.commit()


@pytest.mark.database
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


@pytest.mark.database
async def test_a_stranded_row_is_left_exactly_as_it_was(database: Database) -> None:
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)
    await _strand_a_row(database, "roomy")
    async with database.sessions() as session:
        before = bytes(
            (
                await session.execute(text("SELECT sealed_private_key FROM entity"))
            ).scalar_one()
        )

    with pytest.raises(SealRemovedFormatError):
        await store.load_all(SECRET)

    async with database.sessions() as session:
        after = bytes(
            (
                await session.execute(text("SELECT sealed_private_key FROM entity"))
            ).scalar_one()
        )
    assert after == before, "loading rewrote the row it refused"


@pytest.mark.database
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


@pytest.mark.database
async def test_the_tolerant_load_still_raises_for_a_wrong_secret(
    database: Database,
) -> None:
    """Only the removed format is set aside. A bad store is still a bad store."""
    store = EntityRepository(database=database)
    await store.store(name="roomy", identity=generate_identity(), secret=SECRET)

    with pytest.raises(SealAuthenticationError):
        await store.load_openable(OTHER_SECRET)


@pytest.mark.database
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


# --- Removing a stored identity (design D10) --------------------------------
#
# The only destructive action on `sighop keys`, added because a row stranded by
# migration 0008 blocks its own replacement: the public key it still holds makes
# `store()` refuse the same identity supplied as a private key.


@pytest.mark.database
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


@pytest.mark.database
async def test_removing_an_identity_that_is_not_there_says_so(database: Database) -> None:
    store = EntityRepository(database=database)

    removed = await store.remove(generate_identity().public_key)

    assert isinstance(removed, Succeeded)
    assert removed.value is False


@pytest.mark.database
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
    await persistence.bots.create(
        entity_id=bot_entity.value.id, driver="greeter", config={}
    )

    bound_room = await store.bound_to(stored.value.id)
    bound_bot = await store.bound_to(bot_entity.value.id)

    assert isinstance(bound_room, Succeeded)
    assert bound_room.value == ["room 'the-room'"]
    assert isinstance(bound_bot, Succeeded)
    assert bound_bot.value == ["bot 'greeter'"]


@pytest.mark.database
async def test_bound_to_is_empty_for_an_unbound_identity(database: Database) -> None:
    store = EntityRepository(database=database)
    stored = await store.store(name="free", identity=generate_identity(), secret=SECRET)

    bound = await store.bound_to(stored.value.id)

    assert isinstance(bound, Succeeded)
    assert bound.value == []


@pytest.mark.database
async def test_keys_delete_removes_an_unbound_identity(
    database: Database, store_environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = generate_identity()
    store = EntityRepository(database=database)
    await store.store(name="goodbye", identity=identity, secret=SECRET)
    monkeypatch.setattr("sys.stdin", _TypedAnswer("goodbye"))
    out = io.StringIO()

    code = await _cli(
        ["keys", "delete", "goodbye", "--database-url", store_environment], out
    )

    printed = out.getvalue()
    assert code == 0, printed
    assert "removed" in printed
    assert identity.public_key.hex() in printed
    listed = await store.list_all()
    assert listed.value == []


@pytest.mark.database
async def test_keys_delete_refuses_an_identity_a_room_is_bound_to(
    database: Database, store_environment: str, capsys
) -> None:
    """The foreign keys cascade: this is what stops a room's history going too."""
    persistence = Persistence(database=database)
    server = generate_identity()
    stored = await persistence.entities.store(
        name="roomy", identity=server, secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    await persistence.rooms.create(
        name="the-room", entity_id=stored.value.id, admin_password_hash="x" * 60
    )

    code = await _cli(
        ["keys", "delete", "roomy", "--delete-key", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "the-room" in error
    assert "Remove them first" in error
    listed = await persistence.entities.list_all()
    assert [record.name for record in listed.value] == ["roomy"]
    rooms = await persistence.rooms.list_all()
    assert [room.name for room in rooms.value] == ["the-room"], "the room was deleted"


@pytest.mark.database
async def test_keys_delete_with_no_terminal_and_no_flag_removes_nothing(
    database: Database, store_environment: str, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="goodbye", identity=generate_identity(), secret=SECRET)
    monkeypatch.setattr("sys.stdin", _TypedAnswer("goodbye", a_terminal=False))

    code = await _cli(
        ["keys", "delete", "goodbye", "--database-url", store_environment], io.StringIO()
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "Nothing was removed" in error
    assert "--delete-key to accept it" in error
    listed = await store.list_all()
    assert [record.name for record in listed.value] == ["goodbye"]


@pytest.mark.database
async def test_keys_delete_refuses_a_confirmation_that_is_not_the_name(
    database: Database, store_environment: str, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="goodbye", identity=generate_identity(), secret=SECRET)
    monkeypatch.setattr("sys.stdin", _TypedAnswer("yes"))

    code = await _cli(
        ["keys", "delete", "goodbye", "--database-url", store_environment], io.StringIO()
    )

    assert code == 2
    assert "not confirmed" in capsys.readouterr().err
    listed = await store.list_all()
    assert [record.name for record in listed.value] == ["goodbye"]


@pytest.mark.database
async def test_the_consequence_distinguishes_an_openable_row_from_a_stranded_one(
    database: Database, store_environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EntityRepository(database=database)
    await store.store(name="openable", identity=generate_identity(), secret=SECRET)
    await store.store(name="stranded", identity=generate_identity(), secret=SECRET)
    await _strand_a_row(database, "stranded")

    monkeypatch.setattr("sys.stdin", _TypedAnswer("openable"))
    openable_out = io.StringIO()
    assert (
        await _cli(
            ["keys", "delete", "openable", "--database-url", store_environment],
            openable_out,
        )
        == 0
    )
    monkeypatch.setattr("sys.stdin", _TypedAnswer("stranded"))
    stranded_out = io.StringIO()
    assert (
        await _cli(
            ["keys", "delete", "stranded", "--database-url", store_environment],
            stranded_out,
        )
        == 0
    )

    assert "Unless you hold this identity's private key elsewhere" in openable_out.getvalue()
    assert "loses nothing you can still use" in stranded_out.getvalue()
    assert "cannot be undone" not in stranded_out.getvalue()


@pytest.mark.database
async def test_keys_delete_works_with_no_sealing_secret_configured(
    database: Database, store_environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row nobody can open is exactly what an operator without one may clear."""
    store = EntityRepository(database=database)
    await store.store(name="goodbye", identity=generate_identity(), secret=SECRET)
    monkeypatch.delenv(SECRET_KEY_VARIABLE, raising=False)
    monkeypatch.setattr("sys.stdin", _TypedAnswer("goodbye"))
    out = io.StringIO()

    code = await _cli(
        ["keys", "delete", "goodbye", "--database-url", store_environment], out
    )

    assert code == 0, out.getvalue()
    assert "unknown from here" in out.getvalue()
    listed = await store.list_all()
    assert listed.value == []


@pytest.mark.database
async def test_keys_delete_refuses_a_reference_matching_several_identities(
    database: Database, store_environment: str, capsys
) -> None:
    store = EntityRepository(database=database)
    first = generate_identity()
    second = generate_identity()
    await store.store(name="one", identity=first, secret=SECRET)
    await store.store(name="two", identity=second, secret=SECRET)

    code = await _cli(
        ["keys", "delete", "", "--delete-key", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "matches 2 identities" in error
    assert "one" in error and "two" in error
    listed = await store.list_all()
    assert len(listed.value) == 2


@pytest.mark.database
async def test_a_stranded_identity_can_be_deleted_and_re_imported(
    database: Database, store_environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery migration 0008 leaves an operator needing (design D10)."""
    held = generate_identity()
    store = EntityRepository(database=database)
    await store.store(name="carried-forward", identity=held, secret=SECRET)
    await _strand_a_row(database, "carried-forward")

    # Before: the identity cannot be re-imported, because the row holds its key.
    blocked = await _cli(
        [
            "keys", "import",
            "--private-key", held.private_key.hex(),
            "--name", "carried-forward",
            "--database-url", store_environment,
        ],
        io.StringIO(),
    )
    assert blocked == 2, "the stranded row did not block re-import; D10's premise is gone"

    monkeypatch.setattr("sys.stdin", _TypedAnswer("carried-forward"))
    assert (
        await _cli(
            ["keys", "delete", "carried-forward", "--database-url", store_environment],
            io.StringIO(),
        )
        == 0
    )
    assert (
        await _cli(
            [
                "keys", "import",
                "--private-key", held.private_key.hex(),
                "--name", "carried-forward",
                "--database-url", store_environment,
            ],
            io.StringIO(),
        )
        == 0
    )

    loaded = await store.load_all(SECRET)
    assert isinstance(loaded, Succeeded)
    [entity] = loaded.value
    assert entity.public_key == held.public_key, "the identity did not survive"
    assert entity.record.node_hash == held.public_key[0]
    assert entity.identity.private_key == held.private_key


class _TypedAnswer:
    """Stands in for `sys.stdin`: a terminal, and what the operator typed.

    `input()` reads the real stdin, which pytest has replaced with something
    that is not a terminal — so both halves have to be faked, and faking only
    `isatty` would hang the test waiting for a line that never comes.
    """

    def __init__(self, answer: str, *, a_terminal: bool = True) -> None:
        self._answer = answer
        self._a_terminal = a_terminal

    def isatty(self) -> bool:
        return self._a_terminal

    def readline(self) -> str:
        return self._answer + "\n"


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
    from_file = registry.load(create_keyfile(tmp_path / "one.json", "from-file").path)
    assert stored.source == "the entity store"
    assert stored.from_store
    assert from_file.source == str(tmp_path / "one.json")
    assert not from_file.from_store


# --- 4.7 / 4.8 / 4.10 The command line --------------------------------------


async def _cli(argv: list[str], out: io.StringIO) -> int:
    """Run the command line from inside an async test.

    `main` owns its own event loop — a database command is `asyncio.run` around
    one operation — so it cannot be called from a running one. A worker thread
    gives it the loop it expects and keeps the test able to await the database
    fixture either side of the call.
    """
    return await asyncio.to_thread(main, argv, out=out)


@pytest.fixture
def store_environment(database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the command line at the throwaway schema, with a secret set."""
    monkeypatch.setenv(SECRET_KEY_VARIABLE, base64.b64encode(SECRET).decode())
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    return database_config.url


@pytest.mark.database
async def test_keys_import_stores_the_identity_and_prints_the_public_key(
    database: Database, store_environment: str, tmp_path
) -> None:
    keyfile = create_keyfile(tmp_path / "dev.json", "dev-entity")
    out = io.StringIO()
    assert (
        await _cli(
            ["keys", "import", str(keyfile.path), "--database-url", store_environment],
            out,
        )
        == 0
    )
    printed = out.getvalue()
    assert keyfile.public_key.hex() in printed
    assert keyfile.identity.private_key.hex() not in printed
    assert "sealed under SIGHOP_SECRET_KEY" in printed

    loaded = await EntityRepository(database=database).load_all(SECRET)
    assert isinstance(loaded, Succeeded)
    assert [entity.public_key for entity in loaded.value] == [keyfile.public_key]


# --- Importing a private key the operator already holds ----------------------


@pytest.mark.database
async def test_keys_import_stores_a_supplied_private_key_without_a_file(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = generate_identity()
    out = io.StringIO()

    code = await _cli(
        [
            "keys", "import",
            "--private-key", identity.private_key.hex(),
            "--name", "from-a-device",
            "--database-url", store_environment,
        ],
        out,
    )

    printed = out.getvalue()
    assert code == 0, printed
    assert identity.public_key.hex() in printed
    assert f"0x{identity.node_hash:02x}" in printed
    assert identity.private_key.hex() not in printed
    assert "the supplied private key" in printed
    assert list(tmp_path.iterdir()) == [], "a supplied key must touch no file"

    loaded = await EntityRepository(database=database).load_all(SECRET)
    assert isinstance(loaded, Succeeded)
    assert [entity.public_key for entity in loaded.value] == [identity.public_key]
    assert loaded.value[0].identity.private_key == identity.private_key
    assert loaded.value[0].name == "from-a-device"


@pytest.mark.database
async def test_a_supplied_key_and_a_keyfile_import_to_the_same_identity(
    database: Database, store_environment: str, tmp_path
) -> None:
    """The two ways in are two ways to say the same thing."""
    keyfile = create_keyfile(tmp_path / "dev.json", "dev-entity")
    out = io.StringIO()
    assert (
        await _cli(
            ["keys", "import", str(keyfile.path), "--database-url", store_environment], out
        )
        == 0
    )
    from_file = (await EntityRepository(database=database).load_all(SECRET)).value[0]

    async with database.sessions() as session:
        await session.execute(text("DELETE FROM entity"))
        await session.commit()

    assert (
        await _cli(
            [
                "keys", "import",
                "--private-key", keyfile.identity.private_key.hex(),
                "--name", keyfile.name,
                "--database-url", store_environment,
            ],
            io.StringIO(),
        )
        == 0
    )
    from_key = (await EntityRepository(database=database).load_all(SECRET)).value[0]

    assert from_key.public_key == from_file.public_key
    assert from_key.identity.private_key == from_file.identity.private_key
    assert from_key.record.node_hash == from_file.record.node_hash
    assert from_key.record.type == from_file.record.type


@pytest.mark.database
async def test_keys_import_refuses_both_a_keyfile_and_a_private_key(
    database: Database, store_environment: str, tmp_path, capsys
) -> None:
    keyfile = create_keyfile(tmp_path / "dev.json", "dev-entity")

    code = await _cli(
        [
            "keys", "import", str(keyfile.path),
            "--private-key", generate_identity().private_key.hex(),
            "--name", "x",
            "--database-url", store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    assert "alternatives" in capsys.readouterr().err
    listed = await EntityRepository(database=database).list_all()
    assert listed.value == []


@pytest.mark.database
async def test_keys_import_refuses_neither_a_keyfile_nor_a_private_key(
    database: Database, store_environment: str, capsys
) -> None:
    code = await _cli(
        ["keys", "import", "--database-url", store_environment], io.StringIO()
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "pass a keyfile, or --private-key" in error
    listed = await EntityRepository(database=database).list_all()
    assert listed.value == []


@pytest.mark.database
async def test_a_supplied_private_key_without_a_name_is_refused(
    database: Database, store_environment: str, capsys
) -> None:
    """A keyfile carries a name; a bare key does not (design D8)."""
    code = await _cli(
        [
            "keys", "import",
            "--private-key", generate_identity().private_key.hex(),
            "--database-url", store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    assert "--private-key needs --name" in capsys.readouterr().err
    listed = await EntityRepository(database=database).list_all()
    assert listed.value == []


@pytest.mark.database
@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("not-hex-at-all" * 8, "not hexadecimal"),
        ("ab" * 32, "a seed, which this system does not accept"),
        ("ab" * 65, "is 65 bytes, expected 64"),
    ],
)
async def test_keys_import_refuses_a_private_key_it_cannot_use(
    database: Database, store_environment: str, capsys, supplied: str, expected: str
) -> None:
    code = await _cli(
        [
            "keys", "import",
            "--private-key", supplied,
            "--name", "x",
            "--database-url", store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    assert expected in capsys.readouterr().err
    listed = await EntityRepository(database=database).list_all()
    assert listed.value == [], "a refused key must store nothing"


@pytest.mark.database
async def test_a_supplied_private_key_already_stored_fails_naming_the_existing_one(
    database: Database, store_environment: str, capsys
) -> None:
    identity = generate_identity()
    store = EntityRepository(database=database)
    await store.store(name="already-here", identity=identity, secret=SECRET)

    code = await _cli(
        [
            "keys", "import",
            "--private-key", identity.private_key.hex(),
            "--name", "second-go",
            "--database-url", store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "already-here" in error
    assert "the stored row is unchanged" in error
    listed = await store.list_all()
    assert [record.name for record in listed.value] == ["already-here"]


@pytest.mark.database
async def test_importing_an_identity_already_stored_fails_naming_the_existing_one(
    database: Database, store_environment: str, tmp_path, capsys
) -> None:
    keyfile = create_keyfile(tmp_path / "dev.json", "dev-entity")
    argv = ["keys", "import", str(keyfile.path), "--database-url", store_environment]
    assert await _cli(argv, io.StringIO()) == 0
    assert await _cli(argv, io.StringIO()) == 2
    assert "already stored as entity 'dev-entity'" in capsys.readouterr().err

    listed = await EntityRepository(database=database).list_all()
    assert isinstance(listed, Succeeded)
    assert len(listed.value) == 1, "the stored row was not left unchanged"


@pytest.mark.database
async def test_keys_export_writes_an_owner_only_keyfile_and_says_what_it_holds(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = generate_identity()
    await EntityRepository(database=database).store(
        name="roomy", identity=identity, secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    target = tmp_path / "exported.json"
    out = io.StringIO()
    assert (
        await _cli(
            ["keys", "export", "roomy", str(target), "--database-url", store_environment],
            out,
        )
        == 0
    )
    printed = out.getvalue()
    assert identity.public_key.hex() in printed
    assert identity.private_key.hex() not in printed
    assert PLAINTEXT_KEY_NOTICE in printed
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # The file is the interchange format, so it does hold the seed — in the clear,
    # which is exactly what the notice above says.
    assert identity.private_key.hex() in target.read_text()


@pytest.mark.database
async def test_keys_export_refuses_an_existing_path_and_leaves_it_unchanged(
    database: Database, store_environment: str, tmp_path, capsys
) -> None:
    await EntityRepository(database=database).store(
        name="roomy", identity=generate_identity(), secret=SECRET
    )
    target = tmp_path / "taken.json"
    target.write_text("do not overwrite me")
    assert (
        await _cli(
            ["keys", "export", "roomy", str(target), "--database-url", store_environment],
            io.StringIO(),
        )
        == 2
    )
    assert str(target) in capsys.readouterr().err
    assert target.read_text() == "do not overwrite me"


@pytest.mark.database
async def test_keys_list_shows_identities_and_no_key_material(
    database: Database, store_environment: str
) -> None:
    identity = generate_identity()
    await EntityRepository(database=database).store(
        name="roomy", identity=identity, secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    async with database.sessions() as session:
        sealed = bytes((await session.execute(text("SELECT sealed_private_key FROM entity"))).scalar_one())

    out = io.StringIO()
    assert await _cli(["keys", "list", "--database-url", store_environment], out) == 0
    printed = out.getvalue()
    assert "roomy" in printed
    assert identity.public_key.hex() in printed
    assert f"node_hash=0x{identity.node_hash:02x}" in printed
    assert "enabled" in printed
    assert identity.private_key.hex() not in printed
    assert sealed.hex() not in printed
    assert base64.b64encode(SECRET).decode() not in printed


def test_keys_new_states_that_the_file_holds_an_unencrypted_seed(tmp_path) -> None:
    out = io.StringIO()
    assert main(["keys", "new", "--name", "dev", "--out", str(tmp_path / "dev.json")], out=out) == 0
    assert PLAINTEXT_KEY_NOTICE in out.getvalue()


# --- 4.9 Nothing secret reaches a log event ---------------------------------


@pytest.mark.database
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


# --- 8.9 Store commands need a database; keyfile commands do not -------------


@pytest.mark.parametrize(
    "argv",
    [
        ["keys", "list"],
        ["keys", "import", "missing.json"],
        ["keys", "export", "roomy", "out.json"],
        ["db", "current"],
        ["db", "upgrade"],
    ],
)
def test_a_store_command_with_no_database_says_one_is_required(
    argv: list[str], monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(argv, out=io.StringIO()) == 2
    assert "no database is configured" in capsys.readouterr().err


def test_keyfile_commands_stay_usable_with_no_database(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = io.StringIO()
    path = tmp_path / "dev.json"
    assert main(["keys", "new", "--name", "dev", "--out", str(path)], out=out) == 0
    assert main(["keys", "show", str(path)], out=out) == 0
    assert main(["keys", "secret"], out=out) == 0


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
