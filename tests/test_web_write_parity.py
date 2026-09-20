"""The writes the panel gained, and the rule that keeps them honest.

Milestone 8 built a panel that could watch everything and change almost nothing.
This change closes the distance, and every test here is ultimately about one
property: **a write in the browser is the repository call the command line
makes**. Not "behaves the same" — *is* the same call, so a rule has one
implementation and a refusal reads the same in both surfaces.

The two heavy ones are guarded on the pattern milestone 8 established, because
they are not configuration changes:

* **exporting an identity** writes an unencrypted private key out of the platform;
* **posting to a room** reaches every member of it and cannot be unsent.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import json
import uuid

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from sighop.bots import drivers as bot_drivers
from sighop.cli import main
from sighop.config import (
    DATABASE_SCHEMA_VARIABLE,
    SECRET_KEY_VARIABLE,
    DatabaseConfig,
    generate_secret_key,
)
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    BOT_ENTITY_TYPE,
    MAX_ENTITY_NAME_BYTES,
    LoadedEntity,
    advert_config_for,
)
from sighop.keystore import keyfile_document
from sighop.net.contacts import Contact
from sighop.net.room import POST_SYNC_DELAY_SECS, STORED_POST_TEXT_LEN
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, WireText
from sighop.web.app import allowed_hosts, create_app
from sighop.web.guard import TOKEN_FIELD
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    EXPORT_KEY,
    POST_TO_ROOM,
    NonceStore,
)
from sighop.web.render import Refusal, refused
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    OPERATOR,
    OPERATOR_PASSWORD,
    StubState,
    authenticator,
    csrf,
    signed_async_client,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
SECRET = base64.b64decode(generate_secret_key())


@pytest.fixture
def cli_store_environment(database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point `sighop.cli.main` at the same throwaway schema the panel is using.

    So that "the browser and the command line store the same thing" is tested
    against one database rather than two, which is the only way the claim means
    anything.
    """
    monkeypatch.setenv(SECRET_KEY_VARIABLE, base64.b64encode(SECRET).decode())
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    return database_config.url


NOW = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)


def _built(
    state: StubState | None = None,
    logger: RecordingLogger | None = None,
    *,
    sealing_secret: bytes | None = SECRET,
) -> tuple[FastAPI, StubState, RecordingLogger]:
    panel_state = state or stub_state(stub_names=("panel-identity",))
    log = logger or RecordingLogger()
    app = create_app(
        panel_state, auth=authenticator(), hosts=HOSTS, logger=log, sealing_secret=sealing_secret
    )
    return app, panel_state, log


def _client(app: FastAPI) -> TestClient:
    return signed_client(app, base_url="http://127.0.0.1:8080", follow_redirects=False)


def _live(app: FastAPI) -> httpx2.AsyncClient:
    """A client driving the app in *this* event loop — see `test_web_admin`."""
    return signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    )


async def _apost(client: httpx2.AsyncClient, app: FastAPI, path: str, **fields: str):
    return await client.post(path, data={TOKEN_FIELD: csrf(client), **fields})


def _nonce(body: str) -> str:
    marker = 'name="nonce" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


# --- 1. The shared shapes these pages need ----------------------------------


def test_a_refusal_carries_back_what_was_typed() -> None:
    """1.1: a refused form re-renders with the author's values, not an empty one."""
    refusal = refused("a room keeps 140 bytes and this is 180", field="text", text="…" * 60)

    assert isinstance(refusal, Refusal)
    assert refusal.value("text") == "…" * 60
    assert refusal.concerns("text") is True
    assert refusal.concerns("name") is False
    assert refusal.value("absent", "fallback") == "fallback"


def test_a_refusal_about_the_whole_form_concerns_no_single_field() -> None:
    """1.1: "which field" is a real answer and "none of them" is another."""
    refusal = refused("no database is configured")

    assert refusal.field == ""
    assert refusal.concerns("anything") is False


def test_the_two_new_guarded_actions_mint_and_spend_like_the_others() -> None:
    """1.2: on the existing machinery, so one nonce rule covers all five."""
    store = NonceStore()

    for action in (EXPORT_KEY, POST_TO_ROOM):
        value = store.mint(action, "target-1", now=NOW)
        assert store.spend(value, action, "target-2", now=NOW) is False, (
            f"{action} spent a nonce on another target"
        )

        value = store.mint(action, "target-1", now=NOW)
        assert store.spend(value, action, "target-1", now=NOW) is True
        assert store.spend(value, action, "target-1", now=NOW) is False, "replayed"


def test_the_two_new_actions_say_what_they_do() -> None:
    """1.2: a confirmation with no description is a confirmation of nothing."""
    assert "reaches every member" in ACTION_DESCRIPTIONS[POST_TO_ROOM]
    assert "cannot be unsent" in ACTION_DESCRIPTIONS[POST_TO_ROOM]
    assert "private key" in ACTION_DESCRIPTIONS[EXPORT_KEY]


def test_the_panel_builds_without_a_sealing_secret() -> None:
    """1.3: `None` is an ordinary answer — a run with no database has nothing sealed."""
    app, _state, _log = _built(sealing_secret=None)

    assert app.state.panel.sealing_secret is None
    with _client(app) as client:
        assert client.get("/admin/identities").status_code == 200


def test_the_secret_reaches_the_panel_and_appears_in_no_page() -> None:
    """1.3, design D1: held for opening a seed, and never rendered."""
    app, _state, _log = _built()
    assert app.state.panel.sealing_secret == SECRET

    with _client(app) as client:
        for path in ("/admin/identities", "/rooms", "/chat"):
            assert SECRET.hex() not in client.get(path).text


def test_the_cli_supplies_the_secret_only_when_a_database_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.3, design D1: `cli.py` is the one module that knows both sides.

    And a run with no database is handed nothing: it has no sealed seed to open,
    so demanding the variable would refuse a capability that run does not have.
    """
    import argparse

    from sighop.cli import _web_sealing_secret
    from sighop.config import SECRET_KEY_VARIABLE

    monkeypatch.setenv(SECRET_KEY_VARIABLE, generate_secret_key())
    args = argparse.Namespace(database_url=None)

    # The one attribute the decision reads, which is the whole of what it
    # needs from a run; typed loosely here rather than composing a `Runtime`.
    assert _web_sealing_secret(args, _FakeRuntime(persistence=None)) is None  # type: ignore[arg-type]
    supplied = _web_sealing_secret(args, _FakeRuntime(persistence=object()))  # type: ignore[arg-type]
    assert isinstance(supplied, bytes) and len(supplied) == 32


class _FakeRuntime:
    """Only the one attribute the secret decision reads."""

    def __init__(self, *, persistence: object | None) -> None:
        self.persistence = persistence


# --- 2. Identities: create, inspect, import ---------------------------------


async def _stored(
    persistence: Persistence,
    *,
    name: str = "stored-identity",
    node_type: NodeType = NodeType.CHAT,
    enabled: bool = True,
):
    """One stored identity, through the call both surfaces make."""
    identity = generate_identity()
    outcome = await persistence.entities.store(
        name=name,
        identity=identity,
        secret=SECRET,
        node_type=node_type,
        enabled=enabled,
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value, identity


def _shape(record) -> dict[str, object]:
    """A stored row without what is unique to one creation.

    The id, the creation time and the key itself differ between any two
    identities; everything else is what "created the same way" means.
    """
    return {
        "type": record.type,
        "name": record.name,
        "enabled": record.enabled,
        "advert_config": record.advert_config,
        "node_hash": record.public_key[0],
    }


@pytest.mark.database
async def test_an_identity_created_in_the_browser_is_stored_as_the_cli_stores_one(
    database: Database,
) -> None:
    """2.1, design D3: the browser reaches `EntityRepository.store`, not a copy."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="browser-made",
            node_type="ROOM_SERVER",
        )
    assert response.status_code == 303

    direct, _identity = await _stored(
        persistence, name="browser-made", node_type=NodeType.ROOM_SERVER
    )
    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    made = next(row for row in listed.value if row.id != direct.id)

    assert _shape(made) == _shape(direct) | {"node_hash": made.public_key[0]}
    assert made.type == "room_server"
    assert made.node_type is NodeType.ROOM_SERVER
    assert response.headers["location"] == f"/admin/identities/{made.id}"


@pytest.mark.database
async def test_an_identity_created_from_a_supplied_private_key_matches_the_cli(
    database: Database, cli_store_environment: str
) -> None:
    """The same key, through both surfaces, is the same stored identity."""
    held = generate_identity()
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="from-a-device",
            private_key=held.private_key.hex(),
        )

    assert response.status_code == 303
    opened = await persistence.entities.load_all(SECRET)
    assert isinstance(opened, Succeeded)
    [entity] = opened.value
    assert entity.public_key == held.public_key
    assert entity.identity.private_key == held.private_key
    assert entity.name == "from-a-device"

    # And what `sighop keys import --private-key` stores for the same key.
    async with database.sessions() as session:
        await session.execute(text("DELETE FROM entity"))
        await session.commit()
    assert (
        await asyncio.to_thread(
            main,
            [
                "keys",
                "import",
                "--private-key",
                held.private_key.hex(),
                "--name",
                "from-a-device",
                "--database-url",
                cli_store_environment,
            ],
            out=io.StringIO(),
        )
        == 0
    )
    from_cli = (await persistence.entities.load_all(SECRET)).value[0]
    assert _shape(from_cli.record) == _shape(entity.record)
    assert from_cli.identity.private_key == entity.identity.private_key


@pytest.mark.database
async def test_an_empty_private_key_field_still_generates(database: Database) -> None:
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client, app, "/admin/identities/create", name="generated", private_key="  "
        )

    assert response.status_code == 303
    listed = await persistence.entities.list_all()
    assert [row.name for row in listed.value] == ["generated"]


@pytest.mark.database
@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("   ", "an identity name cannot be empty"),
        ("greeter: one", "read as the message"),
        ("n" * (MAX_ENTITY_NAME_BYTES + 1), "at most 23 bytes"),
        ("roo\x01my", "U+0001"),
    ],
)
async def test_a_refused_identity_name_reads_the_same_in_both_surfaces(
    database: Database, cli_store_environment: str, capsys, supplied: str, expected: str
) -> None:
    """One validator in the repository, so neither surface can drift (design D4).

    The name rules live where the public-key rules already do, which is why
    both halves are checked against the same string rather than against two
    hand-written ones.
    """
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(client, app, "/admin/identities/create", name=supplied)

    assert response.status_code == 400
    assert expected in response.text

    code = await asyncio.to_thread(
        main,
        [
            "keys",
            "import",
            "--private-key",
            generate_identity().private_key.hex(),
            "--name",
            supplied,
            "--database-url",
            cli_store_environment,
        ],
        out=io.StringIO(),
    )
    assert code == 2
    error = capsys.readouterr().err
    assert expected in error, (
        f"the command line refused {supplied!r} differently from the panel: {error!r}"
    )

    listed = await persistence.entities.list_all()
    assert listed.value == [], "a refused name stored an identity"


@pytest.mark.database
@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("nothexatall" * 12, "not hexadecimal"),
        ("ab" * 32, "a seed, which this system does not accept"),
        ("ab" * 70, "is 70 bytes, expected 64"),
    ],
)
async def test_a_supplied_private_key_is_refused_in_the_command_lines_words(
    database: Database, supplied: str, expected: str
) -> None:
    """Design D6: one validator, so the two surfaces cannot drift apart."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="refused",
            node_type="ROOM_SERVER",
            private_key=supplied,
        )

    assert response.status_code == 400
    assert expected in response.text
    assert 'value="refused"' in response.text, "the name the operator typed was not kept"
    assert 'value="ROOM_SERVER" selected' in response.text, (
        "the node type the operator chose was not kept"
    )
    listed = await persistence.entities.list_all()
    assert listed.value == [], "a refused key stored something"


@pytest.mark.database
async def test_a_refused_private_key_is_not_echoed_back_into_the_form(
    database: Database,
) -> None:
    """Every other field is preserved on a refusal. Key material is not."""
    persistence = Persistence(database=database)
    app, _state, log = _built(stub_state(persistence=persistence))
    # Valid hex of the right length, refused for its node hash, so the value
    # reaches the refusal path rather than being rejected as malformed.
    held = generate_identity()

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="refused",
            private_key=held.private_key.hex()[:-2] + "zz",
        )

    assert response.status_code == 400
    assert held.private_key.hex()[:-2] not in response.text, (
        "the submitted private key was rendered back into the page"
    )
    assert "refused" in response.text, "the other fields were not preserved"
    assert held.private_key.hex()[:-2] not in repr(log.events), (
        "a log event carried the submitted private key"
    )


@pytest.mark.database
async def test_a_supplied_key_colliding_with_a_known_node_hash_is_refused(
    database: Database,
) -> None:
    """A supplied key cannot be rolled again, so the rule becomes a refusal."""
    persistence = Persistence(database=database)
    state = stub_state(persistence=persistence, stub_names=("loaded-one",))
    app, _state, _log = _built(state)
    taken = state.adverts.stubs[0].node_hash
    colliding = generate_identity()
    while colliding.node_hash != taken:
        colliding = generate_identity()

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="collides",
            private_key=colliding.private_key.hex(),
        )

    assert response.status_code == 400
    assert f"0x{taken:02x} is already held" in response.text
    listed = await persistence.entities.list_all()
    assert listed.value == []


@pytest.mark.database
async def test_creation_avoids_every_node_hash_this_run_knows(
    database: Database,
) -> None:
    """2.1, §3 rule 3: a panel-created identity is not what stops the next start.

    Both halves of "knows": the identities this run loaded and the ones the
    database holds. A run loads stored entities, so generating against only the
    loaded set would let the panel create exactly the collision the rule exists
    to prevent.
    """
    from sighop.web.routes.keys import _taken_hashes

    persistence = Persistence(database=database)
    state = stub_state(persistence=persistence, stub_names=("loaded-one",))
    app, _state, _log = _built(state)
    record, _identity = await _stored(persistence)

    taken = await _taken_hashes(app.state.panel)
    assert record.node_hash in taken, "a stored hash was not avoided"
    assert state.adverts.stubs[0].node_hash in taken, "a loaded hash was not avoided"

    async with _live(app) as client:
        for index in range(8):
            created = await _apost(
                client, app, "/admin/identities/create", name=f"generated-{index}"
            )
            assert created.status_code == 303

    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    hashes = [row.node_hash for row in listed.value]
    assert len(hashes) == len(set(hashes)), "two stored identities share a node hash"


@pytest.mark.database
async def test_the_identity_page_shows_every_field_and_no_seed(
    database: Database,
) -> None:
    """2.2: `sighop keys show`, for a stored identity rather than for a file."""
    persistence = Persistence(database=database)
    record, identity = await _stored(persistence, name="inspect-me")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await client.get(f"/admin/identities/{record.id}")
    body = response.text

    assert response.status_code == 200
    assert "inspect-me" in body
    assert record.public_key.hex() in body
    assert f"0x{record.node_hash:02x}" in body
    assert record.created_at.isoformat() in body
    assert "CHAT" in body, "the node type it adverts as is not shown"
    assert "flood_interval_seconds" in body, "the advert configuration is not shown"
    assert ">yes<" in body, "the enabled state is not shown"
    assert identity.private_key.hex() not in body, "the page leaked a private key"


@pytest.mark.database
async def test_a_keyfile_imported_in_the_browser_matches_one_the_cli_imported(
    database: Database,
    tmp_path,
) -> None:
    """2.3: the document a keyfile holds, through the same repository call."""
    from sighop.keystore import create_keyfile, load_keyfile

    persistence = Persistence(database=database)
    written = create_keyfile(tmp_path / "peer.json", "from-a-file")
    keyfile = load_keyfile(written.path)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/import",
            document=written.path.read_text(),
        )
    assert response.status_code == 303

    # The same file, imported the way `sighop keys import` imports it.
    second = Persistence(database=database)
    listed = await second.entities.list_all()
    assert isinstance(listed, Succeeded)
    imported = next(row for row in listed.value if row.name == "from-a-file")
    assert imported.public_key == keyfile.public_key
    assert imported.node_hash == keyfile.identity.node_hash
    assert imported.node_type is NodeType.CHAT
    assert imported.enabled is True

    # And the seed that was sealed is the seed the file held.
    opened = await persistence.entities.load_all(SECRET)
    assert isinstance(opened, Succeeded)
    entity = next(e for e in opened.value if e.record.id == imported.id)
    assert entity.identity.private_key == keyfile.identity.private_key


@pytest.mark.database
async def test_a_malformed_keyfile_is_refused_with_the_keystores_own_reason(
    database: Database,
) -> None:
    """2.3: the keystore's message, and nothing stored."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    identity = generate_identity()
    wrong_key = keyfile_document(identity, "mismatched", NodeType.CHAT)
    wrong_key["public_key_hex"] = generate_identity().public_key.hex()

    async with _live(app) as client:
        not_json = await _apost(client, app, "/admin/identities/import", document="{ not json")
        mismatched = await _apost(
            client,
            app,
            "/admin/identities/import",
            document=json.dumps(wrong_key),
        )

    assert not_json.status_code == 400
    assert "keyfile is not valid JSON" in not_json.text
    assert mismatched.status_code == 400
    assert "is not the one the stored private key derives" in mismatched.text
    assert "refusing to prefer either value" in mismatched.text

    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value == [], "a refused import stored something"


@pytest.mark.database
async def test_a_refused_import_re_renders_with_the_submitted_document(
    database: Database,
) -> None:
    """1.1 / 2.3: the author is present, so they keep what they pasted."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    submitted = '{"version": 9, "name": "wrong-version"}'

    async with _live(app) as client:
        response = await _apost(client, app, "/admin/identities/import", document=submitted)

    assert response.status_code == 400
    assert "wrong-version" in response.text, "the submitted document was discarded"


@pytest.mark.database
async def test_a_public_key_already_stored_is_refused_by_the_repository(
    database: Database,
) -> None:
    """2.4: the repository's own message, not one this surface invented."""
    persistence = Persistence(database=database)
    record, identity = await _stored(persistence, name="already-here")
    app, _state, _log = _built(stub_state(persistence=persistence))
    document = keyfile_document(identity, "a-second-name", NodeType.CHAT)

    async with _live(app) as client:
        response = await _apost(
            client, app, "/admin/identities/import", document=json.dumps(document)
        )

    assert response.status_code == 400
    assert "is already stored as entity &#39;already-here&#39;" in response.text
    assert str(record.id) in response.text
    assert "the stored row is unchanged" in response.text

    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    assert len(listed.value) == 1


@pytest.mark.database
async def test_a_bot_identity_that_adverts_wrongly_is_refused_by_the_repository(
    database: Database,
) -> None:
    """2.4: the same rule `sighop keys import --bot` applies, in the same words."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="wrong-kind-of-bot",
            node_type="ROOM_SERVER",
            role="bot",
        )

    assert response.status_code == 400
    assert "indistinguishable from a companion" in response.text

    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value == []


def test_creating_without_a_database_says_so_rather_than_failing() -> None:
    """2.1: a run with no database is a run that stores nothing, stated."""
    app, _state, _log = _built(stub_state(), sealing_secret=None)

    with _client(app) as client:
        response = client.post(
            "/admin/identities/create",
            data={TOKEN_FIELD: csrf(client), "name": "nowhere"},
        )

    assert response.status_code == 400
    assert "no database is configured" in response.text


# --- 3. Exporting an identity (guarded) -------------------------------------


@pytest.mark.database
async def test_the_export_confirmation_names_the_identity_and_holds_no_key(
    database: Database,
) -> None:
    """3.1: minted for that identity alone, and containing no key material."""
    persistence = Persistence(database=database)
    record, identity = await _stored(persistence, name="exportable")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{record.id}/export")).text

    assert "exportable" in body
    assert "private key" in body
    assert identity.private_key.hex() not in body


@pytest.mark.database
async def test_the_exported_document_is_the_command_lines_file(
    database: Database,
    tmp_path,
) -> None:
    """3.2: the same document, so the two files are interchangeable."""
    from sighop.keystore import create_keyfile, keyfile_bytes

    persistence = Persistence(database=database)
    record, identity = await _stored(persistence, name="round-trip")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        page = (await client.get(f"/admin/identities/{record.id}/export")).text
        response = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/export",
            nonce=_nonce(page),
            password=OPERATOR_PASSWORD,
        )

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="round-trip.json"'

    written = create_keyfile(tmp_path / "cli.json", "round-trip", identity=identity)
    assert _without_timestamp(written.path.read_bytes()) == _without_timestamp(response.content)
    # And the whole of the difference is the timestamp: the panel dates the
    # document from the row rather than from the moment it was asked for.
    assert response.content == keyfile_bytes(
        keyfile_document(
            identity,
            "round-trip",
            NodeType.CHAT,
            created_at=record.created_at.isoformat(),
        )
    )


def _without_timestamp(document: bytes) -> object:
    """One keyfile, minus the field that says when it was written."""
    parsed = json.loads(document)
    parsed.pop("created_at")
    return parsed


@pytest.mark.database
async def test_an_exported_keyfile_re_imports_as_the_identity_it_came_from(
    database: Database,
) -> None:
    """3.2, `web-admin`: usable interchangeably is the point of byte-identity."""
    from sighop.keystore import keyfile_from_text

    persistence = Persistence(database=database)
    record, identity = await _stored(persistence, name="there-and-back")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        page = (await client.get(f"/admin/identities/{record.id}/export")).text
        downloaded = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/export",
            nonce=_nonce(page),
            password=OPERATOR_PASSWORD,
        )

    parsed = keyfile_from_text(downloaded.text, "downloaded")
    assert parsed.identity.private_key == identity.private_key
    assert parsed.public_key == record.public_key
    assert parsed.name == "there-and-back"


@pytest.mark.database
async def test_the_difference_in_protection_is_stated_in_both_places(
    database: Database,
) -> None:
    """3.3, design D2: at the point of export and on the identities page."""
    persistence = Persistence(database=database)
    record, _identity = await _stored(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        confirmation = (await client.get(f"/admin/identities/{record.id}/export")).text
        listing = (await client.get("/admin/identities")).text

    for body in (confirmation, listing):
        assert "owner-only (mode 0600) in the same call" in body
        assert "whatever protection the download directory gives it" in body


@pytest.mark.database
async def test_an_export_without_a_confirmation_produces_nothing(
    database: Database,
) -> None:
    """3.4: refused, and refused as its own event."""
    persistence = Persistence(database=database)
    record, identity = await _stored(persistence)
    logger = RecordingLogger()
    app, _state, log = _built(stub_state(persistence=persistence), logger=logger)

    async with _live(app) as client:
        absent = await _apost(client, app, f"/admin/identities/{record.id}/export")
        page = (await client.get(f"/admin/identities/{record.id}/export")).text
        nonce = _nonce(page)
        assert (
            await _apost(
                client,
                app,
                f"/admin/identities/{record.id}/export",
                nonce=nonce,
                password=OPERATOR_PASSWORD,
            )
        ).status_code == 200
        spent = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/export",
            nonce=nonce,
            password=OPERATOR_PASSWORD,
        )

    assert absent.status_code == 403
    assert spent.status_code == 403
    assert identity.private_key.hex() not in absent.text
    assert identity.private_key.hex() not in spent.text

    audited = log.named("web_guarded_action")
    assert [event["outcome"] for event in audited] == ["refused", "success", "refused"]
    assert all(event["action"] == EXPORT_KEY for event in audited)
    assert all(event["target"] == str(record.id) for event in audited)
    assert identity.private_key.hex() not in repr(audited), "an event carried the private key"


@pytest.mark.database
async def test_a_disabled_stored_identity_can_still_be_exported(
    database: Database,
) -> None:
    """3.5, design D1: the case that made the secret be passed in.

    A run loads only *enabled* entities, so an export built on what the run has
    open would quietly mean something narrower in the browser than on the
    command line.
    """
    persistence = Persistence(database=database)
    record, identity = await _stored(persistence, name="switched-off", enabled=False)
    app, _state, _log = _built(stub_state(persistence=persistence))
    assert not app.state.panel.state.adverts.stubs, "the run holds this identity open"

    async with _live(app) as client:
        page = (await client.get(f"/admin/identities/{record.id}/export")).text
        response = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/export",
            nonce=_nonce(page),
            password=OPERATOR_PASSWORD,
        )

    assert response.status_code == 200
    assert json.loads(response.text)["private_key_hex"] == identity.private_key.hex()


# --- 4. Rooms: create, and the read-only fallback ---------------------------


@pytest.mark.database
async def test_a_room_created_in_the_browser_matches_one_the_cli_created(
    database: Database,
) -> None:
    """4.1: through `RoomRepository.create`, with the hashing off the loop."""
    persistence = Persistence(database=database)
    host, _identity = await _stored(
        persistence, name="browser-host", node_type=NodeType.ROOM_SERVER
    )
    other, _second = await _stored(persistence, name="cli-host", node_type=NodeType.ROOM_SERVER)
    direct = await persistence.rooms.create(
        entity_id=other.id, name="cli-lounge", admin_password_hash="x"
    )
    assert isinstance(direct, Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/rooms/create",
            name="browser-lounge",
            entity_id=str(host.id),
            admin_password="correct-horse-battery-staple",
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/rooms"

    listed = await persistence.rooms.list_all()
    assert isinstance(listed, Succeeded)
    made = next(room for room in listed.value if room.name == "browser-lounge")
    assert made.entity_id == host.id
    assert made.guest_password_hash is None
    assert made.guest_open is False
    assert made.allow_read_only is False
    assert made.retention_days is None and made.retention_messages is None
    assert made.admin_password_hash.startswith("$argon2id$"), (
        "the password was not hashed the way the command line hashes it"
    )
    assert "correct-horse-battery-staple" not in response.text


@pytest.mark.database
async def test_a_second_room_on_one_identity_is_refused(database: Database) -> None:
    """4.1: one identity is one node to the mesh, and a node is one room."""
    persistence = Persistence(database=database)
    host, _identity = await _stored(persistence, name="single-host", node_type=NodeType.ROOM_SERVER)
    first = await persistence.rooms.create(
        entity_id=host.id, name="first-lounge", admin_password_hash="x"
    )
    assert isinstance(first, Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/rooms/create",
            name="second-lounge",
            entity_id=str(host.id),
            admin_password="another-one",
        )

    assert response.status_code == 400
    assert "already carries the room &#39;first-lounge&#39;" in response.text

    listed = await persistence.rooms.list_all()
    assert isinstance(listed, Succeeded)
    assert len(listed.value) == 1


@pytest.mark.database
async def test_a_room_cannot_be_created_without_an_admin_password(
    database: Database,
) -> None:
    """4.1: a room with no admin password has no administrator and no way to
    gain one — the command line's refusal, in the same words."""
    persistence = Persistence(database=database)
    host, _identity = await _stored(
        persistence, name="passwordless-host", node_type=NodeType.ROOM_SERVER
    )
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/rooms/create",
            name="no-admin",
            entity_id=str(host.id),
        )

    assert response.status_code == 400
    assert "only credential that admits an administrator" in response.text
    assert "no-admin" in response.text, "the submitted name was discarded"

    listed = await persistence.rooms.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value == []


@pytest.mark.database
async def test_only_unbound_room_server_identities_are_offered(
    database: Database,
) -> None:
    """4.1: the form offers the choices the repository would accept, and no more."""
    persistence = Persistence(database=database)
    companion, _a = await _stored(persistence, name="just-a-companion")
    free, _b = await _stored(persistence, name="free-host", node_type=NodeType.ROOM_SERVER)
    taken, _c = await _stored(persistence, name="taken-host", node_type=NodeType.ROOM_SERVER)
    assert isinstance(
        await persistence.rooms.create(
            entity_id=taken.id, name="existing", admin_password_hash="x"
        ),
        Succeeded,
    )

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get("/rooms")).text

    assert f'value="{free.id}"' in body
    assert f'value="{taken.id}"' not in body, "an identity that already has a room"
    assert f'value="{companion.id}"' not in body, "an identity that is not a room server"


@pytest.mark.database
async def test_the_read_only_fallback_can_be_set_and_cleared(
    database: Database,
) -> None:
    """4.2: the clause milestone 8's task 12.3 marked done without building."""
    persistence = Persistence(database=database)
    host, _identity = await _stored(
        persistence, name="fallback-host", node_type=NodeType.ROOM_SERVER
    )
    created = await persistence.rooms.create(
        entity_id=host.id, name="fallback-lounge", admin_password_hash="x"
    )
    assert isinstance(created, Succeeded)
    room = created.value
    assert room.allow_read_only is False

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        form = (await client.get(f"/admin/rooms/{room.id}/password")).text
        assert "falls back to read-only" in form

        assert (
            await _apost(
                client,
                app,
                f"/admin/rooms/{room.id}/password",
                allow_read_only="true",
            )
        ).status_code == 303
        listed = await persistence.rooms.list_all()
        assert isinstance(listed, Succeeded)
        assert listed.value[0].allow_read_only is True

        assert (await _apost(client, app, f"/admin/rooms/{room.id}/password")).status_code == 303

    listed = await persistence.rooms.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].allow_read_only is False, "the flag could not be cleared"


@pytest.mark.database
async def test_a_room_created_here_is_served_by_a_run_that_loads_its_identity(
    database: Database,
) -> None:
    """4.3: and unserved, with the reason stated, by a run that does not."""

    persistence = Persistence(database=database)
    host, _identity = await _stored(
        persistence, name="runnable-host", node_type=NodeType.ROOM_SERVER
    )
    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        assert (
            await _apost(
                client,
                app,
                "/admin/rooms/create",
                name="will-be-served",
                entity_id=str(host.id),
                admin_password="admin-of-the-lounge",
            )
        ).status_code == 303

    opened = await persistence.entities.load_all(SECRET)
    assert isinstance(opened, Succeeded)
    loaded = next(entity for entity in opened.value if entity.record.id == host.id)

    serving = _runtime(persistence, stored=(loaded,))
    await serving._load_rooms()
    assert [server.room.name for server in serving.rooms] == ["will-be-served"]

    blind = _runtime(persistence, stored=())
    await blind._load_rooms()
    assert blind.rooms == []
    assert any("will-be-served" in line for line in blind._unserved_rooms)
    assert any("identity was not loaded" in line for line in blind._unserved_rooms)


def _runtime(persistence: Persistence, *, stored: tuple[LoadedEntity, ...]):
    """A run that is composed but never started — only its room loading is asked."""
    import io

    from sighop.runtime import Runtime, RuntimeConfig

    return Runtime(
        source=_no_events(),
        startup=_no_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=stored),
        out=io.StringIO(),
        persistence=persistence,
    )


async def _no_startup() -> str:
    return ""


# --- 5. Posting to a room (guarded) -----------------------------------------


async def _room(database: Database, *, name: str = "post-lounge"):
    """A room on a room-server identity, as both surfaces make one."""
    persistence = Persistence(database=database)
    entity = await persistence.entities.store(
        name=f"{name}-host",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(entity, Succeeded)
    room = await persistence.rooms.create(
        entity_id=entity.value.id, name=name, admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)
    return persistence, room.value, entity.value


@pytest.mark.database
async def test_the_composer_names_the_room_and_bounds_the_text(
    database: Database,
) -> None:
    """5.1: the room named, and the limit stated before anything is typed."""
    persistence, room, _entity = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}/post")).text

    assert "post-lounge" in body
    assert str(STORED_POST_TEXT_LEN) in body
    assert "room's <strong>own identity</strong>" in body


@pytest.mark.database
async def test_an_over_length_post_is_refused_with_the_text_preserved(
    database: Database,
) -> None:
    """5.1 / 1.1, design D4: the limit, the overage, and what they typed."""
    persistence, room, _entity = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    written = "x" * (STORED_POST_TEXT_LEN + 12)

    async with _live(app) as client:
        response = await _apost(client, app, f"/rooms/{room.id}/post", text=written)

    assert response.status_code == 400
    assert f"is {STORED_POST_TEXT_LEN + 12} bytes" in response.text
    assert f"a room keeps {STORED_POST_TEXT_LEN}" in response.text
    assert "12 over" in response.text
    assert written in response.text, "the author's text was discarded"

    counted = await persistence.messages.count(room.id)
    assert isinstance(counted, Succeeded)
    assert counted.value == 0, "a refused post stored something"


@pytest.mark.database
async def test_the_confirmation_states_what_the_post_does_when_served(
    database: Database,
) -> None:
    """5.2: reaches every member, stored now, deliverable after the hold."""
    persistence, room, _entity = await _room(database)
    state = stub_state(persistence=persistence, transmit_enabled=True)
    state.rooms.append(_serving(room))
    app, _state, _log = _built(state)

    async with _live(app) as client:
        body = (await _apost(client, app, f"/rooms/{room.id}/post", text="hello")).text

    assert "reaches every member" in body
    assert "stored now" in body
    assert f"{POST_SYNC_DELAY_SECS}s hold" in body
    assert "not serving" not in body
    assert "transmit gate is closed" not in body


@pytest.mark.database
async def test_the_confirmation_says_when_no_run_will_deliver_it(
    database: Database,
) -> None:
    """5.2, `web-rooms`: stored like any other post, and nothing delivered."""
    persistence, room, _entity = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence, transmit_enabled=True))

    async with _live(app) as client:
        body = (await _apost(client, app, f"/rooms/{room.id}/post", text="hello")).text

    assert "This run is not serving post-lounge" in body
    assert "nothing will be delivered" in body
    # Collapsed, because the sentence is wrapped in the template.
    assert "run that loads this room" in " ".join(body.split())


@pytest.mark.database
async def test_the_confirmation_says_when_the_gate_is_closed(
    database: Database,
) -> None:
    """5.2: stored now, on the air when the gate opens — and not refused."""
    persistence, room, _entity = await _room(database)
    state = stub_state(persistence=persistence, transmit_enabled=False)
    state.rooms.append(_serving(room))
    app, _state, _log = _built(state)

    async with _live(app) as client:
        body = (await _apost(client, app, f"/rooms/{room.id}/post", text="hello")).text

    assert "transmit gate is closed" in body
    assert "stored now" in body
    assert "put on the air only when the gate is opened" in body


@pytest.mark.database
async def test_a_confirmed_post_is_the_row_the_command_line_stores(
    database: Database,
) -> None:
    """5.3: `MessageRepository.store`, as the room's own identity, in order."""
    persistence, room, entity = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        page = (await _apost(client, app, f"/rooms/{room.id}/post", text="first")).text
        response = await _apost(
            client,
            app,
            f"/rooms/{room.id}/post/confirm",
            text="first",
            nonce=_nonce(page),
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/rooms/{room.id}"

    # The same call, from the command line's side, into the same room.
    direct = await persistence.messages.store(
        room_id=room.id, author_public_key=entity.public_key, text=b"second"
    )
    assert isinstance(direct, Succeeded)

    history = await persistence.messages.history(room.id, limit=10)
    assert isinstance(history, Succeeded)
    posts = sorted(history.value, key=lambda post: post.post_timestamp)
    assert [post.text for post in posts] == [b"first", b"second"]
    assert all(post.author_public_key == entity.public_key for post in posts)
    assert posts[0].post_timestamp < posts[1].post_timestamp, (
        "the post is not in the room's own total order"
    )
    assert posts[0].sender_timestamp is None


@pytest.mark.database
async def test_a_post_without_a_confirmation_stores_nothing(
    database: Database,
) -> None:
    """5.4: refused, nothing stored, and its own event with a refused outcome."""
    persistence, room, _entity = await _room(database)
    logger = RecordingLogger()
    app, _state, log = _built(stub_state(persistence=persistence), logger=logger)

    async with _live(app) as client:
        absent = await _apost(client, app, f"/rooms/{room.id}/post/confirm", text="unconfirmed")

    assert absent.status_code == 403
    counted = await persistence.messages.count(room.id)
    assert isinstance(counted, Succeeded)
    assert counted.value == 0

    audited = log.named("web_guarded_action")
    assert [event["outcome"] for event in audited] == ["refused"]
    assert audited[0]["action"] == POST_TO_ROOM
    assert audited[0]["target"] == str(room.id)
    assert audited[0]["room_name"] == "post-lounge"


@pytest.mark.database
async def test_a_confirmed_post_is_its_own_event_naming_the_room(
    database: Database,
) -> None:
    """5.4: separate from the request's, because they answer two questions."""
    persistence, room, _entity = await _room(database)
    logger = RecordingLogger()
    app, _state, log = _built(stub_state(persistence=persistence), logger=logger)

    async with _live(app) as client:
        page = (await _apost(client, app, f"/rooms/{room.id}/post", text="heard")).text
        await _apost(
            client,
            app,
            f"/rooms/{room.id}/post/confirm",
            text="heard",
            nonce=_nonce(page),
        )

    audited = log.named("web_guarded_action")
    assert len(audited) == 1
    assert audited[0]["outcome"] == "success"
    # Milestone 9, 8.5: confirm-and-nonce with no password field, and the post
    # names the account that made it.
    assert audited[0]["actor"] == OPERATOR
    assert 'name="password"' not in page
    assert audited[0]["room_name"] == "post-lounge"
    assert audited[0]["served"] is False
    assert audited[0]["transmit_enabled"] is False
    assert audited[0]["bytes"] == 5
    assert len(log.named("web_request")) == 2


@pytest.mark.database
async def test_opening_the_composer_and_the_confirmation_store_nothing(
    database: Database,
) -> None:
    """5.5: only the confirmed POST writes a row or charges any airtime."""
    persistence, room, _entity = await _room(database)
    state = stub_state(persistence=persistence)
    app, _state, _log = _built(state)
    before = state.scheduler.status().as_json()

    async with _live(app) as client:
        assert (await client.get(f"/rooms/{room.id}/post")).status_code == 200
        assert (
            await _apost(client, app, f"/rooms/{room.id}/post", text="not yet")
        ).status_code == 200
        # And again, because a confirmation page is re-rendered freely.
        await _apost(client, app, f"/rooms/{room.id}/post", text="still not")

    counted = await persistence.messages.count(room.id)
    assert isinstance(counted, Succeeded)
    assert counted.value == 0
    assert state.scheduler.status().as_json() == before, "composing transmitted"


@pytest.mark.database
async def test_a_confirmation_cannot_be_replayed(database: Database) -> None:
    """5.4 / 13.1: one nonce, one post — a reload posts nothing twice."""
    persistence, room, _entity = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        page = (await _apost(client, app, f"/rooms/{room.id}/post", text="once")).text
        nonce = _nonce(page)
        first = await _apost(
            client, app, f"/rooms/{room.id}/post/confirm", text="once", nonce=nonce
        )
        again = await _apost(
            client, app, f"/rooms/{room.id}/post/confirm", text="once", nonce=nonce
        )

    assert first.status_code == 303
    assert again.status_code == 403
    counted = await persistence.messages.count(room.id)
    assert isinstance(counted, Succeeded)
    assert counted.value == 1


@pytest.mark.database
async def test_an_over_length_post_is_refused_at_the_confirmation_too(
    database: Database,
) -> None:
    """5.1: the limit is not a property of the form it was typed into."""
    persistence, room, _entity = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    store = app.state.panel.nonces

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            f"/rooms/{room.id}/post/confirm",
            text="y" * (STORED_POST_TEXT_LEN + 1),
            nonce=store.mint(POST_TO_ROOM, str(room.id)),
        )

    assert response.status_code == 403
    counted = await persistence.messages.count(room.id)
    assert isinstance(counted, Succeeded)
    assert counted.value == 0


def _serving(room):
    """A stand-in for a room this run serves — only its id is ever read."""

    class _Server:
        def __init__(self, record) -> None:
            self.room = record

    return _Server(room)


# --- 6. Bots: create, configure per key, and their records ------------------


async def _bot_identity(persistence: Persistence, *, name: str):
    """An identity stored as a bot, which is what a bot may be bound to."""
    identity = generate_identity()
    outcome = await persistence.entities.store(
        name=name,
        identity=identity,
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type=BOT_ENTITY_TYPE,
        advert_config=advert_config_for(NodeType.CHAT),
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value


@pytest.mark.database
async def test_a_bot_created_in_the_browser_matches_one_the_cli_created(
    database: Database,
) -> None:
    """6.1: `BotRepository.create`, with the driver's own defaults."""
    persistence = Persistence(database=database)
    browser_host = await _bot_identity(persistence, name="browser-bot")
    cli_host = await _bot_identity(persistence, name="cli-bot")
    direct = await persistence.bots.create(
        entity_id=cli_host.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name=cli_host.name,
    )
    assert isinstance(direct, Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/bots/create",
            entity_id=str(browser_host.id),
            driver="greeter",
        )
    assert response.status_code == 303

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    made = next(bot for bot in listed.value if bot.entity_id == browser_host.id)

    assert made.driver == direct.value.driver
    assert made.config == direct.value.config
    assert made.enabled is True, "a new bot is not enabled"
    assert made.mode == "observe", "a new bot is not observing"
    assert made.entity_name == "browser-bot"


@pytest.mark.database
async def test_an_unknown_driver_is_refused_by_listing_what_exists(
    database: Database,
) -> None:
    """6.1: the registry's own refusal — no entry-point discovery, so it can."""
    persistence = Persistence(database=database)
    host = await _bot_identity(persistence, name="unknown-driver-host")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            "/admin/bots/create",
            entity_id=str(host.id),
            driver="parrot",
        )

    assert response.status_code == 400
    assert "no driver named &#39;parrot&#39;" in response.text
    assert "this build has greeter" in response.text

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value == []


@pytest.mark.database
async def test_a_new_bot_is_seeded_with_the_contacts_the_node_knows(
    database: Database,
) -> None:
    """6.1, design D7: a greeter does not start out owing every peer a message."""
    from sighop.bots.greeter import SEEDED, greeted_key

    persistence = Persistence(database=database)
    host = await _bot_identity(persistence, name="seeded-bot")
    known = Contact(public_key=b"\x41" * 32, name=WireText.from_bytes(b"old-friend"))
    assert isinstance(await persistence.contacts.upsert(known), Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        assert (
            await _apost(
                client,
                app,
                "/admin/bots/create",
                entity_id=str(host.id),
                driver="greeter",
            )
        ).status_code == 303

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    stored = await persistence.bot_state.list(listed.value[0].id)
    assert isinstance(stored, Succeeded)
    record = stored.value[greeted_key(known.public_key)]
    assert record["outcome"] == SEEDED


@pytest.mark.database
async def test_a_refused_configuration_value_names_its_own_key(
    database: Database,
) -> None:
    """6.2, design D7: and leaves every other key exactly as it was."""
    persistence, bot = await _greeter(database)
    before = dict(bot.config)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            f"/admin/bots/{bot.id}/config",
            key="burst",
            value="not a number",
        )

    assert response.status_code == 400
    assert "burst: burst is a whole number" in response.text
    assert "stored configuration is unchanged" in response.text

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].config == before


@pytest.mark.database
async def test_one_key_changes_and_the_others_do_not(database: Database) -> None:
    """6.2: the whole reason the form is per key rather than one object."""
    persistence, bot = await _greeter(database)
    before = dict(bot.config)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        assert (
            await _apost(client, app, f"/admin/bots/{bot.id}/config", key="burst", value="5")
        ).status_code == 303

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    after = listed.value[0].config
    assert after["burst"] == 5
    assert {k: v for k, v in after.items() if k != "burst"} == {
        k: v for k, v in before.items() if k != "burst"
    }


@pytest.mark.database
async def test_the_configuration_textarea_is_gone(database: Database) -> None:
    """6.2: two ways to edit one thing is how the two disagree."""
    persistence, _bot = await _greeter(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{_bot.entity_id}")).text

    assert "<textarea" not in body
    assert 'name="configuration"' not in body
    assert 'name="key" value="burst"' in body, "the per-key form is not there either"


@pytest.mark.database
async def test_a_greeting_record_can_be_listed_cleared_set_and_seeded(
    database: Database,
) -> None:
    """6.3: each operation against the keys `bots/greeter.py` owns (design D5)."""
    from sighop.bots.greeter import OPERATOR, SEEDED, greeted_key

    persistence, bot = await _greeter(database)
    peer = Contact(public_key=b"\x51" * 32, name=WireText.from_bytes(b"a-peer"))
    assert isinstance(await persistence.contacts.upsert(peer), Succeeded)
    state = stub_state(persistence=persistence)
    state.contacts.restore([peer])
    app, _state, _log = _built(state)
    key = greeted_key(peer.public_key)

    async with _live(app) as client:
        # Seed: the contact gains a record saying nothing is owed to it.
        seeded = await _apost(client, app, f"/admin/bots/{bot.id}/greeted/seed")
        assert seeded.status_code == 303
        assert seeded.headers["location"] == f"/admin/identities/{bot.entity_id}"
        stored = await persistence.bot_state.get(bot.id, key)
        assert isinstance(stored, Succeeded) and stored.value["outcome"] == SEEDED

        # List: the record is shown, by contact and by state.
        listing = (await client.get(f"/admin/bots/{bot.id}/greeted")).text
        assert peer.public_key.hex()[:16] in listing
        assert "a-peer" in listing
        assert SEEDED in listing

        # Clear: the record goes, and the contact is eligible again.
        assert (
            await _apost(
                client,
                app,
                f"/admin/bots/{bot.id}/greeted/clear",
                public_key=peer.public_key.hex(),
            )
        ).status_code == 303
        gone = await persistence.bot_state.get(bot.id, key)
        assert isinstance(gone, Succeeded) and gone.value is None

        # Set: recorded by an operator, which is its own third state.
        assert (
            await _apost(
                client,
                app,
                f"/admin/bots/{bot.id}/greeted/set",
                public_key=peer.public_key.hex(),
            )
        ).status_code == 303

    written = await persistence.bot_state.get(bot.id, key)
    assert isinstance(written, Succeeded)
    assert written.value["outcome"] == OPERATOR
    assert written.value["name"] == "a-peer"


@pytest.mark.database
async def test_seeding_leaves_an_existing_record_alone(database: Database) -> None:
    """6.3 / 6.4, `web-admin`: contacts that had a record keep the one they had."""
    from sighop.bots.greeter import OPERATOR, greeted_key

    persistence, bot = await _greeter(database)
    peer = Contact(public_key=b"\x52" * 32, name=WireText.from_bytes(b"already-known"))
    assert isinstance(await persistence.contacts.upsert(peer), Succeeded)
    key = greeted_key(peer.public_key)
    assert isinstance(
        await persistence.bot_state.set(
            bot.id, key, {"outcome": OPERATOR, "at": "earlier", "name": "already-known"}
        ),
        Succeeded,
    )

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        assert (await _apost(client, app, f"/admin/bots/{bot.id}/greeted/seed")).status_code == 303

    kept = await persistence.bot_state.get(bot.id, key)
    assert isinstance(kept, Succeeded)
    assert kept.value["outcome"] == OPERATOR, "seeding overwrote an existing record"
    assert kept.value["at"] == "earlier"


@pytest.mark.database
async def test_both_statements_precede_the_actions_they_are_about(
    database: Database,
) -> None:
    """6.4: said in front of the control, not after the change."""
    from sighop.bots.greeter import seeded_entries

    persistence, bot = await _greeter(database)
    peer = Contact(public_key=b"\x54" * 32, name=WireText.from_bytes(b"listed"))
    assert isinstance(
        await persistence.bot_state.set_many(bot.id, seeded_entries([peer], at="earlier")),
        Succeeded,
    )
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/bots/{bot.id}/greeted")).text

    collapsed = " ".join(body.split())
    assert "eligible to be acted on again" in collapsed
    assert "that means transmitting at it" in collapsed
    assert "keep the one they have" in collapsed

    # Each statement is above the form it is about, which is what "before" means.
    assert collapsed.index("eligible to be acted on again") < collapsed.index("clear this record")
    assert collapsed.index("keep the one they have") < collapsed.index(
        "seed from the stored contacts"
    )


@pytest.mark.database
async def test_clearing_a_bots_whole_state_states_and_counts(
    database: Database,
) -> None:
    """6.5: what it forgets before the change, and how many keys went after."""
    from sighop.bots.greeter import SEEDED, greeted_key

    persistence, bot = await _greeter(database)
    entries: dict[str, object] = {
        greeted_key(bytes([index]) * 32): {"outcome": SEEDED} for index in range(3)
    }
    assert isinstance(await persistence.bot_state.set_many(bot.id, entries), Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        confirmation = (await client.get(f"/admin/bots/{bot.id}/state/clear")).text
        assert "forget everything it recorded, the seed included" in " ".join(confirmation.split())
        assert "<strong>3 keys</strong>" in confirmation

        # Still there: opening the confirmation changes nothing.
        still = await persistence.bot_state.list(bot.id)
        assert isinstance(still, Succeeded) and len(still.value) == 3

        response = await _apost(client, app, f"/admin/bots/{bot.id}/state/clear", confirm="yes")

    assert response.status_code == 200
    assert "Cleared 3 keys" in response.text
    emptied = await persistence.bot_state.list(bot.id)
    assert isinstance(emptied, Succeeded)
    assert emptied.value == {}


@pytest.mark.database
async def test_clearing_a_record_makes_the_greeter_eligible_again(
    database: Database,
) -> None:
    """6.6: asserted through the greeter's own gate, not through a row count.

    A row that is gone is not the claim; the claim is that the bot will act on
    that contact again, and only the driver can answer that.
    """
    from sighop.bots.base import SuppressionReason
    from sighop.bots.greeter import GreeterBot, greeted_key, seeded_entries

    persistence, bot = await _greeter(database)
    peer = Contact(
        public_key=b"\x53" * 32,
        name=WireText.from_bytes(b"released"),
        node_type=NodeType.CHAT,
        advert_verified=True,
    )
    assert isinstance(await persistence.contacts.upsert(peer), Succeeded)
    assert isinstance(
        await persistence.bot_state.set_many(bot.id, seeded_entries([peer], at="earlier")),
        Succeeded,
    )
    driver = GreeterBot(config=dict(bot.config))
    key = greeted_key(peer.public_key)
    now = dt.datetime.now(dt.UTC)

    stored = await persistence.bot_state.list(bot.id)
    assert isinstance(stored, Succeeded)
    assert driver._record_gate(stored.value.get(key), 0, now) is SuppressionReason.ALREADY_GREETED

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        assert (
            await _apost(
                client,
                app,
                f"/admin/bots/{bot.id}/greeted/clear",
                public_key=peer.public_key.hex(),
            )
        ).status_code == 303

    after = await persistence.bot_state.list(bot.id)
    assert isinstance(after, Succeeded)
    assert driver._record_gate(after.value.get(key), 0, now) is None, (
        "the greeter still considers this contact settled"
    )


async def _greeter(database: Database):
    """A greeter bot on its own identity, as `sighop bot create` makes one."""
    persistence = Persistence(database=database)
    host = await _bot_identity(persistence, name="greeter-host")
    created = await persistence.bots.create(
        entity_id=host.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name=host.name,
    )
    assert isinstance(created, Succeeded)
    return persistence, created.value


# --- 7. The schema page, and what is deliberately absent --------------------


@pytest.mark.database
async def test_the_schema_page_shows_both_revisions_and_that_they_agree(
    database: Database,
) -> None:
    """7.1: the question a degraded panel raises first, answered."""
    from sighop.db import migrations

    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get("/system")).text

    expected = migrations.expected_revision()
    assert expected in body
    assert "agree" in body
    assert ">yes<" in body
    assert "revision this code expects" in body


@pytest.mark.database
async def test_a_disagreement_names_both_revisions_and_the_command(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7.1: and the disagreement is stated rather than left to be worked out."""
    from sighop.db import migrations

    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    real = await database.read_applied_revision()
    monkeypatch.setattr(migrations, "expected_revision", lambda: "0099_imaginary")

    async with _live(app) as client:
        body = (await client.get("/system")).text

    assert "0099_imaginary" in body
    assert (real or "(none)") in body
    assert "revisions disagree" in body
    assert migrations.UPGRADE_COMMAND in body


@pytest.mark.database
async def test_the_schema_page_says_migrations_are_not_applied_here(
    database: Database,
) -> None:
    """7.2, design D6: which absence this is, and why."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get("/system")).text

    collapsed = " ".join(body.split())
    assert "not applied from here, and that is deliberate" in collapsed
    assert "stolen session" in collapsed, "the reason is roles, not a missing login"
    assert "port with no authentication" not in collapsed, "milestone 8's reason is gone"
    assert "act an operator takes on purpose" in collapsed


def test_no_route_on_the_interface_applies_a_migration() -> None:
    """7.2: swept over the route table, so a new route joins the rule."""
    import inspect

    from fastapi.routing import APIRoute

    from tests.webfixtures import registered_routes

    app, _state, _log = _built(stub_state())
    routes = [r for r in registered_routes(app) if isinstance(r, APIRoute)]
    assert routes, "no routes to sweep; the assertion would be vacuous"

    for route in routes:
        source = inspect.getsource(route.endpoint)
        assert "migrations.upgrade" not in source, f"{route.path} applies a migration"
        assert "upgrade_async" not in source, f"{route.path} applies a migration"


def test_the_identities_page_says_the_secret_is_a_terminal_command() -> None:
    """7.3: named where an operator would look for it."""
    app, _state, _log = _built(stub_state())

    with _client(app) as client:
        body = client.get("/admin/identities").text

    collapsed = " ".join(body.split())
    assert "not generated here" in collapsed
    assert "must never be regenerated" in collapsed
    assert "unrecoverable" in collapsed
    assert "sighop keys secret" in collapsed


def test_the_identities_page_says_removal_is_offered_and_what_it_costs() -> None:
    """The change `web-delete-and-rename` withdrew design D10's exclusion.

    This used to assert that the page named `sighop keys delete` and said
    removal was not offered in the browser. It is offered now, so what the page
    owes an operator is what it costs and what the reversible alternative is.
    """
    app, _state, _log = _built(stub_state())

    with _client(app) as client:
        body = client.get("/admin/identities").text

    collapsed = " ".join(body.split())
    assert "not offered here" not in collapsed
    assert "Removing a stored identity is offered here" in collapsed
    assert "irreversible" in collapsed
    assert "Disabling is the reversible action offered alongside it" in collapsed, (
        "an operator must be pointed at what this page also offers"
    )
    assert "room or a bot is bound to" in collapsed


def test_exactly_one_route_removes_a_stored_identity_and_it_is_guarded() -> None:
    """The inverse of what this asserted before, and no longer vacuous.

    The old version walked `app.routes`, which in this FastAPI version holds
    one opaque `_IncludedRouter` per included router — so it never reached the
    identity routes at all and would have passed whatever they did. That is the
    trap `registered_routes` exists for, and this goes through it.
    """
    import inspect

    from fastapi.routing import APIRoute

    from tests.webfixtures import registered_routes

    app, _state, _log = _built(stub_state())

    removing = [
        route
        for route in registered_routes(app)
        if isinstance(route, APIRoute)
        and inspect.isfunction(route.endpoint)
        and "entities.remove" in inspect.getsource(route.endpoint)
    ]

    assert len(removing) == 1, [getattr(r, "path", r) for r in removing]
    [route] = removing
    assert route.path == "/admin/identities/{entity_id}/remove"
    assert route.methods == {"POST"}, "removal must not be reachable by a GET"
    source = inspect.getsource(route.endpoint)
    assert "nonces.spend" in source, "the removal is not behind a confirmation"
    assert "reauthenticate" in source, "the removal does not ask for the password"
    assert "confirm_name" in source, "the removal does not ask for the typed name"


def test_the_schema_page_with_no_database_says_so_rather_than_nothing() -> None:
    """7.4: and it is not the same wording a degraded database gets."""
    app, _state, _log = _built(stub_state())

    with _client(app) as client:
        body = client.get("/system").text

    assert "no database is configured" in body
    assert "unreachable" not in body
    assert "cannot be read" in body, "the page rendered empty"


@pytest.mark.database
async def test_a_degraded_database_is_its_own_wording_on_the_schema_page(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7.4: three states, as every other durable view has (`web-server`)."""
    from sighop.db.engine import DatabaseError

    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async def _refuses(self: Database) -> str | None:
        raise DatabaseError("the database is not answering")

    # On the class: `Database` uses slots, and what is being simulated is the
    # engine refusing rather than one instance being special.
    monkeypatch.setattr(Database, "read_applied_revision", _refuses)

    async with _live(app) as client:
        body = (await client.get("/system")).text

    assert "unreachable" in body
    assert "no database is configured" not in body
    assert "not answering" in body


async def _no_events():
    """A source that yields nothing; this run is never run."""
    return
    yield  # pragma: no cover - what makes this a generator


# --- 8. Non-regression and the seam ------------------------------------------
#
# Every sweep here is enumerated over the route table through
# `registered_routes`, and every one of them fills in the parameters the
# unparameterised sweeps in `test_web_panel` and `test_web_guard` skip. That is
# the whole point: half this change's new pages are `/{something}/…`, and a
# sweep that quietly skipped them would be a sweep of the pages that did not
# need sweeping.


async def _populated(database: Database):
    """A panel state with one of everything this change can write to."""
    persistence = Persistence(database=database)
    identity_record, _identity = await _stored(persistence, name="swept-identity")
    host, _host_identity = await _stored(
        persistence, name="swept-host", node_type=NodeType.ROOM_SERVER
    )
    room = await persistence.rooms.create(
        entity_id=host.id, name="swept-lounge", admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)
    bot_host = await _bot_identity(persistence, name="swept-bot")
    bot = await persistence.bots.create(
        entity_id=bot_host.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name=bot_host.name,
    )
    assert isinstance(bot, Succeeded)
    webhook = await persistence.webhooks.create(
        name="swept-hook",
        url="https://hooks.example.org/swept/token",
        format="json",
        triggers=["new_repeater"],
        secret=SECRET,
    )
    assert isinstance(webhook, Succeeded)
    state = stub_state(persistence=persistence, stub_names=("swept-run-identity",))
    # Change `channel-messaging`: the seeded Public channel, loaded as a run would.
    assert await state.reload_channels()
    return (
        persistence,
        state,
        {
            "channel_id": str(state.channels.channels.channels[0].id),
            "webhook_id": str(webhook.value.id),
            "entity_id": str(identity_record.id),
            "room_id": str(room.value.id),
            "bot_id": str(bot.value.id),
            "public_key": identity_record.public_key.hex(),
            # Milestone 8's chat pages, so the sweep covers the whole interface
            # rather than only what this change added.
            "entity_key": state.adverts.stubs[0].identity.public_key.hex(),
            "peer_key": identity_record.public_key.hex(),
            # web-advert-now's confirmation views: the flood one, the costlier.
            "kind": "flood",
        },
    )


def _every_page(app: FastAPI, ids: dict[str, str]) -> list[tuple[str, str]]:
    """Every (method, path) a browser could reach by navigating, filled in.

    Through `registered_routes`, which walks into included routers — this
    FastAPI version does not flatten them into `app.routes`, so the obvious loop
    sees three routes out of forty and every assertion below would be a fraction
    of what it claims.
    """
    from fastapi.routing import APIRoute

    from tests.webfixtures import registered_routes

    found: list[tuple[str, str]] = []
    for route in registered_routes(app):
        if not isinstance(route, APIRoute) or route.path in ("/login", "/setup"):
            # The sign-in and setup forms are the public pages and render no
            # platform state at all (milestone 9 design D5).
            continue
        path = route.path
        for name, value in ids.items():
            path = path.replace("{" + name + "}", value)
        if "{" in path:  # pragma: no cover - a parameter this fixture lacks
            raise AssertionError(f"{route.path} has a parameter nothing fills")
        for method in sorted((route.methods or set()) & {"GET", "HEAD"}):
            found.append((method, path))
    return found


@pytest.mark.database
async def test_every_page_this_change_added_renders_the_meter(
    database: Database,
) -> None:
    """8.1, design D12: including the parameterised ones, which is the half a
    sweep over link-reachable paths alone would miss."""
    _persistence, state, ids = await _populated(database)
    app, _state, _log = _built(state)
    pages = _every_page(app, ids)
    assert len(pages) > 20, f"only {len(pages)} pages were swept; the walk is broken"

    # The pages this change added, named so the sweep below cannot pass by
    # 404ing everywhere: a "no such thing" page renders the meter too, and
    # would satisfy the sweep while proving nothing.
    added = [
        "/system",
        f"/admin/identities/{ids['entity_id']}",
        f"/admin/identities/{ids['entity_id']}/export",
        f"/admin/bots/{ids['bot_id']}/greeted",
        f"/admin/bots/{ids['bot_id']}/state/clear",
        f"/rooms/{ids['room_id']}/post",
    ]

    async with _live(app) as client:
        for path in added:
            response = await client.get(path)
            assert response.status_code == 200, f"{path} → {response.status_code}"
            assert 'aria-label="duty cycle"' in response.text, f"{path} has no meter"

        for method, path in pages:
            response = await client.request(method, path)
            assert response.status_code < 500, f"{method} {path} → {response.status_code}"
            if method == "HEAD":
                continue
            if path.endswith("/messages") and response.status_code == 200:
                # An HTMX fragment, swapped into a page that already carries the
                # meter; a fragment that carried its own would draw it twice.
                assert "<html" not in response.text, f"{path} is a whole page"
                continue
            assert 'aria-label="duty cycle"' in response.text, f"{path} has no meter"
            assert "persistence" in response.text, f"{path} does not state durability"


@pytest.mark.database
async def test_no_safe_request_on_the_enlarged_interface_changes_anything(
    database: Database,
) -> None:
    """8.2: the sweep milestone 8's task 8.3 built, over every new route.

    State, transmission, stored posts and key material, in one pass: those are
    the four things a GET must never do, and a route added later joins the rule
    by being in the route table rather than by being remembered.
    """
    persistence, state, ids = await _populated(database)
    app, _state, _log = _built(state)
    private_keys = [stub.identity.private_key for stub in state.adverts.stubs]
    assert private_keys, "the state holds no identity; the leak assertion would be vacuous"

    before = state.scheduler.status().as_json()
    contacts = len(state.contacts)
    paths = state.pipeline.paths.destination_count
    posts = await persistence.messages.count(uuid.UUID(ids["room_id"]))
    assert isinstance(posts, Succeeded)
    entities = await persistence.entities.list_all()
    assert isinstance(entities, Succeeded)
    bot_state = await persistence.bot_state.list(uuid.UUID(ids["bot_id"]))
    assert isinstance(bot_state, Succeeded)

    async with _live(app) as client:
        for method, path in _every_page(app, ids):
            response = await client.request(method, path)
            for private_key in private_keys:
                assert private_key.hex() not in response.text, f"{path} leaked a private key"
            assert "private_key_hex" not in response.text, f"{path} served key material"
            assert "seed_hex" not in response.text, f"{path} served key material"

    assert state.scheduler.status().as_json() == before, "a safe request transmitted"
    assert len(state.contacts) == contacts
    assert state.pipeline.paths.destination_count == paths

    after_posts = await persistence.messages.count(uuid.UUID(ids["room_id"]))
    assert isinstance(after_posts, Succeeded)
    assert after_posts.value == posts.value, "a safe request stored a post"
    after_entities = await persistence.entities.list_all()
    assert isinstance(after_entities, Succeeded)
    assert len(after_entities.value) == len(entities.value), "a safe request stored an identity"
    after_state = await persistence.bot_state.list(uuid.UUID(ids["bot_id"]))
    assert isinstance(after_state, Succeeded)
    assert after_state.value == bot_state.value, "a safe request changed durable state"


@pytest.mark.database
async def test_every_new_write_emits_exactly_one_request_event(
    database: Database,
) -> None:
    """8.3: one wide event per unit of work (§9), and no write emits two."""
    _persistence, state, ids = await _populated(database)
    logger = RecordingLogger()
    app, _state, log = _built(state, logger=logger)

    writes = [
        ("/admin/identities/create", {"name": "one-more"}),
        (f"/admin/identities/{ids['entity_id']}/enabled", {"enabled": "false"}),
        (f"/admin/rooms/{ids['room_id']}/password", {"allow_read_only": "true"}),
        (f"/admin/bots/{ids['bot_id']}/config", {"key": "burst", "value": "4"}),
        (f"/admin/bots/{ids['bot_id']}/greeted/seed", {}),
    ]

    async with _live(app) as client:
        for path, fields in writes:
            before = len(log.named("web_request"))
            await _apost(client, app, path, **fields)
            assert len(log.named("web_request")) == before + 1, (
                f"{path} emitted more than one request event"
            )

    assert log.named("web_guarded_action") == [], (
        "an ordinary configuration change emitted a guarded-action event"
    )


@pytest.mark.database
async def test_every_new_guarded_action_emits_one_further_event(
    database: Database,
) -> None:
    """8.3: two events, because they answer two questions (design D10)."""
    _persistence, state, ids = await _populated(database)
    logger = RecordingLogger()
    app, _state, log = _built(state, logger=logger)

    async with _live(app) as client:
        export_page = (await client.get(f"/admin/identities/{ids['entity_id']}/export")).text
        requests_before = len(log.named("web_request"))
        await _apost(
            client,
            app,
            f"/admin/identities/{ids['entity_id']}/export",
            nonce=_nonce(export_page),
            password=OPERATOR_PASSWORD,
        )
        assert len(log.named("web_request")) == requests_before + 1
        assert len(log.named("web_guarded_action")) == 1

        post_page = (await _apost(client, app, f"/rooms/{ids['room_id']}/post", text="swept")).text
        requests_before = len(log.named("web_request"))
        await _apost(
            client,
            app,
            f"/rooms/{ids['room_id']}/post/confirm",
            text="swept",
            nonce=_nonce(post_page),
        )
        assert len(log.named("web_request")) == requests_before + 1
        assert len(log.named("web_guarded_action")) == 2

    assert [event["action"] for event in log.named("web_guarded_action")] == [
        EXPORT_KEY,
        POST_TO_ROOM,
    ]


async def test_the_enlarged_interface_changes_no_replay_count() -> None:
    """8.4: the assertion milestones 6, 7 and 8 all used, for this change.

    The whole application served over the platform that is replaying, with every
    page requested as it goes: if a page were reading the mesh rather than
    reporting on it, this is where it would show.
    """
    from sighop.radio.modem import EU868_NARROW
    from tests.test_web_exercise import _counts, _replayed

    without = await _counts(watched=False)

    state = stub_state(radio=EU868_NARROW, stub_names=("replay-identity",))
    state.contacts.subscribe(state.pipeline.bus)
    app, _state, _log = _built(state)
    for record in _replayed():
        state.pipeline.ingest(record)
    await state.pipeline.bus.aclose()

    with _client(app) as client:
        for path in (
            "/",
            "/contacts",
            "/admin/identities",
            "/rooms",
            "/chat",
            "/system",
        ):
            assert client.get(path).status_code == 200

    assert {
        "delivered": state.pipeline.delivered,
        "duplicates": state.pipeline.duplicates,
        "contacts": len(state.contacts),
        "paths": state.pipeline.paths.destination_count,
    } == {key: without[key] for key in ("delivered", "duplicates", "contacts", "paths")}
    assert without["delivered"] > 0, "the comparison would be vacuous"


# --- webhook-notifications 8.1: a webhook added in either surface -----------


@pytest.mark.database
async def test_a_webhook_added_in_the_browser_matches_one_the_cli_added(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through `WebhookRepository.create` in both surfaces, so one stored shape."""
    import asyncio
    import io
    import sys

    from sighop.cli import main
    from sighop.config import DATABASE_SCHEMA_VARIABLE, SECRET_KEY_VARIABLE
    from sighop.db.sealing import open_value

    persistence = Persistence(database=database)
    config = database.config
    assert config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, config.schema)
    monkeypatch.setenv(SECRET_KEY_VARIABLE, base64.b64encode(SECRET).decode())
    url = "https://discord.com/api/webhooks/1/token"

    def cli() -> int:
        original_in, original_err = sys.stdin, sys.stderr
        sys.stdin, sys.stderr = io.StringIO(f"{url}\n"), io.StringIO()
        try:
            return main(
                [
                    "webhook",
                    "add",
                    "cli-hook",
                    "--format",
                    "discord",
                    "--trigger",
                    "new_repeater",
                    "--trigger",
                    "new_companion",
                    "--max-hops",
                    "3",
                    "--database-url",
                    config.url,
                ],
                out=io.StringIO(),
            )
        finally:
            sys.stdin, sys.stderr = original_in, original_err

    assert await asyncio.to_thread(cli) == 0

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        response = await client.post(
            "/admin/webhooks/create",
            data={
                TOKEN_FIELD: csrf(client),
                "name": "browser-hook",
                "url": url,
                "format": "discord",
                "triggers": ["new_repeater", "new_companion"],
                "max_hops": "3",
            },
        )
    assert response.status_code == 303

    from sqlalchemy import select

    from sighop.db.models import Webhook as WebhookRow

    async with database.sessions() as session:
        rows = {row.name: row for row in (await session.execute(select(WebhookRow))).scalars()}

    def shape(row: WebhookRow) -> dict[str, object]:
        return {
            "url": open_value(bytes(row.sealed_url), SECRET),
            "url_host": row.url_host,
            "format": row.format,
            "triggers": list(row.triggers),
            "max_hops": row.max_hops,
            "enabled": row.enabled,
            "last": (row.last_delivered_at, row.last_failed_at, row.last_failure),
        }

    assert shape(rows["cli-hook"]) == shape(rows["browser-hook"])


# --- The deletes and renames this change added -------------------------------
#
# Same property as everything above: the browser's write *is* the repository
# call the command line makes, so a rule has one implementation.


@pytest.mark.database
async def test_a_room_deleted_in_either_surface_leaves_the_same_store(
    database: Database, cli_store_environment: str
) -> None:
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async def _room(name: str) -> tuple[uuid.UUID, uuid.UUID]:
        stored = await persistence.entities.store(
            name=f"rs-{name}",
            identity=generate_identity(),
            secret=SECRET,
            node_type=NodeType.ROOM_SERVER,
        )
        assert isinstance(stored, Succeeded)
        created = await persistence.rooms.create(
            entity_id=stored.value.id, name=name, admin_password_hash="x" * 60
        )
        assert isinstance(created, Succeeded)
        return stored.value.id, created.value.id

    entity_id, room_id = await _room("from-the-panel")
    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room_id}/delete")).text
        response = await _apost(client, app, f"/admin/rooms/{room_id}/delete", nonce=_nonce(body))
    assert response.status_code == 303
    from_panel = (await persistence.entities.get_by_id(entity_id)).value
    assert from_panel is not None, "the panel deleted the identity too"
    assert (await persistence.rooms.list_all()).value == []

    cli_entity_id, _cli_room_id = await _room("from-the-terminal")
    assert (
        await asyncio.to_thread(
            main,
            [
                "room",
                "delete",
                "from-the-terminal",
                "--delete-history",
                "--database-url",
                cli_store_environment,
            ],
            out=io.StringIO(),
        )
        == 0
    )
    from_cli = (await persistence.entities.get_by_id(cli_entity_id)).value
    assert from_cli is not None, "the command line deleted the identity too"
    assert (await persistence.rooms.list_all()).value == []
    # The two surfaces left the same shape behind: an identity, unbound.
    assert (from_panel.type, from_panel.enabled) == (from_cli.type, from_cli.enabled)


@pytest.mark.database
async def test_a_rename_in_either_surface_leaves_the_same_row(
    database: Database, cli_store_environment: str
) -> None:
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    held = generate_identity()
    stored = await persistence.entities.store(name="before", identity=held, secret=SECRET)
    assert isinstance(stored, Succeeded)

    async with _live(app) as client:
        await _apost(
            client, app, f"/admin/identities/{stored.value.id}/rename", name="from-the-panel"
        )
    from_panel = _shape((await persistence.entities.get_by_id(stored.value.id)).value)

    assert (
        await asyncio.to_thread(
            main,
            [
                "keys",
                "rename",
                "from-the-panel",
                "from-the-terminal",
                "--database-url",
                cli_store_environment,
            ],
            out=io.StringIO(),
        )
        == 0
    )
    from_cli = _shape((await persistence.entities.get_by_id(stored.value.id)).value)

    # Everything but the name is untouched by either.
    assert {k: v for k, v in from_panel.items() if k != "name"} == {
        k: v for k, v in from_cli.items() if k != "name"
    }
    assert from_panel["name"] == "from-the-panel"
    assert from_cli["name"] == "from-the-terminal"
    opened = await persistence.entities.load_all(SECRET)
    assert opened.value[0].identity.private_key == held.private_key


@pytest.mark.database
async def test_a_refused_rename_reads_the_same_in_both_surfaces(
    database: Database, cli_store_environment: str, capsys
) -> None:
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    stored = await persistence.entities.store(
        name="before", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    await persistence.entities.store(name="taken", identity=generate_identity(), secret=SECRET)

    async with _live(app) as client:
        response = await _apost(
            client, app, f"/admin/identities/{stored.value.id}/rename", name="taken"
        )
    assert response.status_code == 400
    assert "already named" in response.text

    code = await asyncio.to_thread(
        main,
        ["keys", "rename", "before", "taken", "--database-url", cli_store_environment],
        out=io.StringIO(),
    )
    assert code == 2
    assert "already named" in capsys.readouterr().err
    assert sorted(r.name for r in (await persistence.entities.list_all()).value) == [
        "before",
        "taken",
    ]


@pytest.mark.database
async def test_the_whole_lifecycle_through_the_panel(database: Database) -> None:
    """Create an identity, bind a room, delete the room, rename, then remove.

    The round trip the change exists for, and the one that proves the two
    halves fit: removal is refused while the room is bound, and the identity
    becomes reusable the moment it is not.
    """
    persistence = Persistence(database=database)
    state = stub_state(persistence=persistence)
    app, _state, _log = _built(state)

    async with _live(app) as client:
        # 1. Create an identity that can carry a room.
        created = await _apost(
            client,
            app,
            "/admin/identities/create",
            name="lifecycle",
            node_type="ROOM_SERVER",
        )
        assert created.status_code == 303
        [identity] = (await persistence.entities.list_all()).value

        # 2. Bind a room to it.
        bound = await _apost(
            client,
            app,
            "/admin/rooms/create",
            name="the-lounge",
            entity_id=str(identity.id),
            admin_password="an-admin-password",
        )
        assert bound.status_code in (303, 200), bound.text
        [room] = (await persistence.rooms.list_all()).value

        # 3. Removal is refused while the room is bound, and names it.
        blocked = (await client.get(f"/admin/identities/{identity.id}/remove")).text
        assert "the-lounge" in blocked
        assert 'name="nonce"' not in blocked

        # 4. Delete the room. The identity survives, unbound.
        body = (await client.get(f"/admin/rooms/{room.id}/delete")).text
        gone = await _apost(client, app, f"/admin/rooms/{room.id}/delete", nonce=_nonce(body))
        assert gone.status_code == 303
        assert (await persistence.rooms.list_all()).value == []
        assert (await persistence.entities.bound_to(identity.id)).value == []

        # 5. Rename it, now that nothing is bound.
        renamed = await _apost(
            client, app, f"/admin/identities/{identity.id}/rename", name="lifecycle-2"
        )
        assert renamed.status_code == 200
        assert (await persistence.entities.get_by_id(identity.id)).value.name == "lifecycle-2"

        # 6. And remove it, which is now offered.
        confirm = (await client.get(f"/admin/identities/{identity.id}/remove")).text
        assert 'name="nonce"' in confirm, "removal is still refused with nothing bound"
        removed = await _apost(
            client,
            app,
            f"/admin/identities/{identity.id}/remove",
            nonce=_nonce(confirm),
            password=OPERATOR_PASSWORD,
            confirm_name="lifecycle-2",
        )
        assert removed.status_code == 303

    assert (await persistence.entities.list_all()).value == []


@pytest.mark.database
async def test_an_identity_carries_a_new_room_after_the_old_one_is_deleted(
    database: Database,
) -> None:
    """What deleting a room is *for*: the identity is reusable, not stranded."""
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))
    stored = await persistence.entities.store(
        name="reusable",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(stored, Succeeded)
    first = await persistence.rooms.create(
        entity_id=stored.value.id, name="first", admin_password_hash="x" * 60
    )
    assert isinstance(first, Succeeded)

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{first.value.id}/delete")).text
        await _apost(client, app, f"/admin/rooms/{first.value.id}/delete", nonce=_nonce(body))
        again = await _apost(
            client,
            app,
            "/admin/rooms/create",
            name="second",
            entity_id=str(stored.value.id),
            admin_password="an-admin-password",
        )

    assert again.status_code in (303, 200), again.text
    assert [room.name for room in (await persistence.rooms.list_all()).value] == ["second"]
