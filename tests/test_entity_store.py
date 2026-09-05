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
from sighop.db.repositories import (
    EntityKeyMismatchError,
    EntityRepository,
    advert_config_for,
)
from sighop.db.sealing import (
    SEAL_VERSION,
    SealAuthenticationError,
    SealFormatError,
    seal_seed,
)
from sighop.db.sealing import open_seed as unseal
from sighop.keystore import (
    PLAINTEXT_SEED_NOTICE,
    EntityRegistry,
    NodeHashCollisionError,
    create_keyfile,
)
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.payloads import NodeType

SECRET = base64.b64decode(generate_secret_key())
OTHER_SECRET = base64.b64decode(generate_secret_key())


# --- 4.1 Sealing (design D4) ------------------------------------------------


def test_a_sealed_seed_round_trips_and_is_not_the_seed() -> None:
    identity = generate_identity()
    sealed = seal_seed(identity.seed, SECRET)
    assert sealed[0] == SEAL_VERSION
    assert identity.seed not in sealed, "the plaintext seed appears in the stored value"
    assert unseal(sealed, SECRET) == identity.seed


def test_the_wrong_secret_does_not_open_a_sealed_seed() -> None:
    sealed = seal_seed(generate_identity().seed, SECRET)
    with pytest.raises(SealAuthenticationError) as excinfo:
        unseal(sealed, OTHER_SECRET, entity="entity 'roomy'")
    assert "entity 'roomy'" in str(excinfo.value)
    assert "Nothing was decrypted" in str(excinfo.value)


def test_flipping_one_ciphertext_byte_is_an_authentication_error() -> None:
    """Poly1305 is what makes a tampered row fail loudly rather than yield
    some other key — the property §6 asks for by name."""
    sealed = bytearray(seal_seed(generate_identity().seed, SECRET))
    sealed[-1] ^= 0x01
    with pytest.raises(SealAuthenticationError):
        unseal(bytes(sealed), SECRET)


def test_an_unknown_seal_version_is_corruption_not_a_wrong_secret() -> None:
    sealed = bytearray(seal_seed(generate_identity().seed, SECRET))
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
    assert entity.identity.seed == identity.seed
    assert entity.record.advert_config == advert_config_for(NodeType.ROOM_SERVER)
    assert entity.record.enabled is True


@pytest.mark.database
async def test_the_stored_column_holds_ciphertext_and_not_the_seed(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    await store.store(name="roomy", identity=identity, secret=SECRET)
    async with database.sessions() as session:
        sealed = (await session.execute(text("SELECT sealed_seed FROM entity"))).scalar_one()
    assert bytes(sealed) != identity.seed
    assert identity.seed not in bytes(sealed)


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
    assert identity.seed.hex() not in message


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
    assert keyfile.identity.seed.hex() not in printed
    assert "sealed under SIGHOP_SECRET_KEY" in printed

    loaded = await EntityRepository(database=database).load_all(SECRET)
    assert isinstance(loaded, Succeeded)
    assert [entity.public_key for entity in loaded.value] == [keyfile.public_key]


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
    assert identity.seed.hex() not in printed
    assert PLAINTEXT_SEED_NOTICE in printed
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # The file is the interchange format, so it does hold the seed — in the clear,
    # which is exactly what the notice above says.
    assert identity.seed.hex() in target.read_text()


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
        sealed = bytes((await session.execute(text("SELECT sealed_seed FROM entity"))).scalar_one())

    out = io.StringIO()
    assert await _cli(["keys", "list", "--database-url", store_environment], out) == 0
    printed = out.getvalue()
    assert "roomy" in printed
    assert identity.public_key.hex() in printed
    assert f"node_hash=0x{identity.node_hash:02x}" in printed
    assert "enabled" in printed
    assert identity.seed.hex() not in printed
    assert sealed.hex() not in printed
    assert base64.b64encode(SECRET).decode() not in printed


def test_keys_new_states_that_the_file_holds_an_unencrypted_seed(tmp_path) -> None:
    out = io.StringIO()
    assert main(["keys", "new", "--name", "dev", "--out", str(tmp_path / "dev.json")], out=out) == 0
    assert PLAINTEXT_SEED_NOTICE in out.getvalue()


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
    assert identity.seed.hex() not in rendered
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
    assert identity.seed.hex() not in rendered
    assert "sealed" not in rendered
    assert os.environ.get(SECRET_KEY_VARIABLE, "\0") not in rendered
