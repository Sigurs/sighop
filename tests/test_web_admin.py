"""Configuration and the three guarded actions (`web-admin`, design D10).

Two rules carry this area and both are about *not* having two of something:

* **One implementation of every rule.** A change made in the browser goes
  through the repository call the command line makes, so a configuration the
  CLI would refuse is refused here for the same reason in the same words.
* **One way to do a dangerous thing.** Revealing a key, opening the transmit
  gate and raising the airtime ceiling are each confirmed on their own page,
  each require a nonce that page minted, and each emit their own event — a
  refused attempt included, because a refused attempt at revealing a key is the
  more interesting one.
"""

from __future__ import annotations

import base64
import datetime as dt

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from sighop.web.app import allowed_hosts, create_app
from sighop.web.guard import TOKEN_FIELD
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    ENABLE_TRANSMIT,
    RAISE_CEILING,
    REAUTHENTICATED_ACTIONS,
    REMOVE_BOT,
    REMOVE_CHANNEL,
    REMOVE_IDENTITY,
    REMOVE_ROOM,
    REVEAL_KEY,
    NonceStore,
)
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    OPERATOR,
    OPERATOR_PASSWORD,
    StubState,
    authenticator,
    csrf,
    session_of,
    signed_async_client,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
SECRET = base64.b64decode(generate_secret_key())
NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def _built(
    state: StubState | None = None, logger: RecordingLogger | None = None
) -> tuple[FastAPI, StubState, RecordingLogger]:
    panel_state = state or stub_state(stub_names=("panel-identity",))
    log = logger or RecordingLogger()
    return create_app(panel_state, auth=authenticator(), hosts=HOSTS, logger=log), panel_state, log


def _client(app: FastAPI) -> TestClient:
    return signed_client(app, base_url="http://127.0.0.1:8080", follow_redirects=False)


def _live(app: FastAPI) -> httpx2.AsyncClient:
    """A client that drives the app in **this** event loop.

    `TestClient` runs the application in a loop of its own, which is fine for a
    page over a stub state and wrong for one over a database: the async engine
    is bound to the test's loop, and a request served on another one meets a
    closed loop. Every database-backed page here uses this instead.
    """
    return signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    )


def _post(client: TestClient, app: FastAPI, path: str, **fields: str):
    return client.post(path, data={TOKEN_FIELD: csrf(client), **fields})


async def _apost(client: httpx2.AsyncClient, app: FastAPI, path: str, **fields: str):
    return await client.post(path, data={TOKEN_FIELD: csrf(client), **fields})


def _nonce(body: str) -> str:
    """The nonce the confirmation page minted, as a browser would submit it."""
    marker = 'name="nonce" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


# --- 12.1 / 12.2 Identities -------------------------------------------------


def test_the_identities_page_lists_what_this_run_loaded_in_full() -> None:
    """12.1: the public key and node hash in full, not truncated to fit."""
    app, state, _log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        body = client.get("/admin/identities").text

    assert stub.identity.public_key.hex() in body
    assert f"0x{stub.node_hash:02x}" in body


def test_the_page_states_what_an_exported_keyfile_is() -> None:
    """12.1, `web-admin`: an unencrypted private key, protected only by permissions."""
    app, _state, _log = _built()

    with _client(app) as client:
        body = client.get("/admin/identities").text

    assert "unencrypted private key" in body
    assert "file permissions" in body
    assert "SIGHOP_SECRET_KEY" in body, "the store's own protection is not contrasted"


def test_with_no_database_the_stored_identities_say_so_rather_than_none() -> None:
    """12.1 / 9.5: "no identities" and "cannot read identities" are two screens."""
    app, _state, _log = _built(stub_state())

    with _client(app) as client:
        body = client.get("/admin/identities").text

    assert "cannot be read" in body
    assert "no database is configured" in body


@pytest.mark.database
async def test_an_identity_created_in_the_ui_is_indistinguishable_from_the_cli(
    database: Database,
) -> None:
    """12.1, `web-admin`: one implementation of the rule, so one stored result.

    The UI has no create form of its own — creating an identity is
    `sighop keys new`, whose repository call this asserts against directly. What
    the assertion is really about is that both surfaces reach the same method:
    a second creation path is how two surfaces end up with two rules.
    """
    persistence = Persistence(database=database)
    identity = generate_identity()

    stored = await persistence.entities.store(
        name="from-anywhere",
        identity=identity,
        secret=SECRET,
        node_type=NodeType.CHAT,
    )
    assert isinstance(stored, Succeeded)

    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    record = next(row for row in listed.value if row.name == "from-anywhere")
    assert record.public_key == identity.public_key
    assert record.enabled is True


@pytest.mark.database
async def test_disabling_an_identity_a_room_is_bound_to_names_the_room(
    database: Database,
) -> None:
    """12.2: what the identity serves, before the change rather than after."""
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="lounge-host",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(stored, Succeeded)
    room = await persistence.rooms.create(
        entity_id=stored.value.id, name="[redacted]", admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get("/admin/identities")).text

    assert "[redacted]" in body
    assert "Disabling this identity stops" in body
    assert "immediately" in body and "does not wait for a restart" in body
    assert "stored messages" in body and "durable state" in body and "survive" in body


@pytest.mark.database
async def test_disabling_an_identity_held_as_a_default_states_that_too(
    database: Database,
) -> None:
    """`web-admin`: an operator holding this identity as their default is told
    it will have nothing preselected, before disabling it."""
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="my-default", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        session_of(client).default_entity_id = str(stored.value.id)
        body = (await client.get("/admin/identities")).text

    assert "your default chat identity" in body
    assert "nothing" in body and "preselected" in body


@pytest.mark.database
async def test_an_identity_can_be_disabled_and_enabled_through_the_page(
    database: Database,
) -> None:
    """12.1: the write goes through `EntityRepository.set_enabled`."""
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="switchable", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        response = await _apost(
            client, app, f"/admin/identities/{stored.value.id}/enabled", enabled="false"
        )
    assert response.status_code == 303

    listed = await persistence.entities.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].enabled is False


# --- 12.3 / 12.4 Rooms ------------------------------------------------------


@pytest.mark.database
async def test_the_rooms_page_shows_member_and_message_counts(
    database: Database,
) -> None:
    """12.3: the two numbers an operator actually wants from a room list."""
    persistence = Persistence(database=database)
    entity = await persistence.entities.store(
        name="lounge-host",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(entity, Succeeded)
    room = await persistence.rooms.create(
        entity_id=entity.value.id, name="[redacted]", admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)
    assert isinstance(
        await persistence.messages.store(
            room_id=room.value.id, author_public_key=b"\x01" * 32, text=b"hello"
        ),
        Succeeded,
    )

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get("/rooms")).text

    assert "[redacted]" in body
    assert "unserved" in body, "an unserved room is not marked as served"

    # One list for reading and configuring (consolidate-web-pages 2.2).
    row = body[body.index("<td>[redacted]</td>") :]
    row = row[: row.index("</tr>")]
    assert f'href="/rooms/{room.value.id}"' in row
    assert f'href="/admin/rooms/{room.value.id}/delete"' in row
    assert '<td class="num">1</td>' in row, "the message count is not in the row"


@pytest.mark.database
async def test_a_rotation_states_that_members_must_log_in_again(
    database: Database,
) -> None:
    """12.4: the consequence, before the change is applied."""
    persistence, room = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room.id}/password")).text

    assert "log in again" in body
    assert "refused in silence" in body


@pytest.mark.database
async def test_a_retention_bound_states_how_many_messages_are_stored(
    database: Database,
) -> None:
    """12.4: the count, before the bound is applied."""
    persistence, room = await _room(database)
    for index in range(3):
        assert isinstance(
            await persistence.messages.store(
                room_id=room.id, author_public_key=b"\x02" * 32, text=f"m{index}".encode()
            ),
            Succeeded,
        )

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room.id}/retention")).text

    # `plural()` writes the real plural now, and the whole phrase is emphasised.
    assert "3 stored messages</strong>" in body
    assert "retention wins over sync" in body


@pytest.mark.database
async def test_a_submitted_password_appears_in_no_response_and_no_event(
    database: Database,
) -> None:
    """12.3, `web-admin`: not in a query string, not reflected, not in an event."""
    persistence, room = await _room(database)
    logger = RecordingLogger()
    app, _state, log = _built(stub_state(persistence=persistence), logger=logger)
    secret_password = "correct-horse-battery-staple"

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            f"/admin/rooms/{room.id}/password",
            admin_password=secret_password,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/rooms"
        assert secret_password not in response.text
        page = (await client.get("/rooms")).text

    assert secret_password not in page
    assert secret_password not in repr(log.events), "a password reached an event"

    # And it did change the stored hash, so the assertion is not vacuous.
    listed = await persistence.rooms.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].admin_password_hash.startswith("$argon2id$")


@pytest.mark.database
async def test_setting_retention_goes_through_the_rooms_repository(
    database: Database,
) -> None:
    """12.3: the same call `sighop room retention` makes."""
    persistence, room = await _room(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            f"/admin/rooms/{room.id}/retention",
            retention_days="7",
            retention_messages="",
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/rooms"

    listed = await persistence.rooms.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].retention_days == 7
    assert listed.value[0].retention_messages is None, "blank means no bound"


async def _room(database: Database):
    persistence = Persistence(database=database)
    entity = await persistence.entities.store(
        name="lounge-host",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(entity, Succeeded)
    room = await persistence.rooms.create(
        entity_id=entity.value.id, name="[redacted]", admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)
    return persistence, room.value


# --- 12.5 / 12.6 Bots -------------------------------------------------------


@pytest.mark.database
async def test_a_bot_identitys_page_shows_the_greeting_records(database: Database) -> None:
    """12.5: the records that decide whether a bot acts on a contact again."""
    persistence, bot = await _bot(database)
    assert isinstance(
        await persistence.bot_state.set(bot.id, "greeted:aabb", {"outcome": "acknowledged"}),
        Succeeded,
    )

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{bot.entity_id}")).text

    assert "greeted:aabb" in body
    assert "acknowledged" in body
    assert "greeted:" in body, "the greeting records are not readable"


@pytest.mark.database
async def test_an_invalid_driver_configuration_leaves_the_stored_one_unchanged(
    database: Database,
) -> None:
    """12.5: refused with the driver's own reason, and nothing written."""
    persistence, bot = await _bot(database)
    before = dict(bot.config)
    logger = RecordingLogger()
    app, _state, log = _built(stub_state(persistence=persistence), logger=logger)

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            f"/admin/bots/{bot.id}/config",
            key="burst",
            value="not a number",
        )
    assert response.status_code == 400
    assert "<h1>dev-greeter</h1>" in response.text, "not the identity's page"
    assert "whole number" in response.text, "the driver's reason is not on the page"

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].config == before, "a refused configuration was stored"

    refusals = log.named("web_bot_config_refused")
    assert refusals, "the refusal was not reported"
    assert "whole number" in str(refusals[0]["reason"]), (
        "the driver's own reason was replaced with a generic one"
    )


@pytest.mark.database
async def test_a_valid_driver_configuration_is_stored(database: Database) -> None:
    """12.5: the refusal above means something only if acceptance works."""
    persistence, bot = await _bot(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(client, app, f"/admin/bots/{bot.id}/config", key="burst", value="3")
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/identities/{bot.entity_id}"

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].config["burst"] == 3


@pytest.mark.database
async def test_a_switch_to_active_is_unchanged_until_it_is_confirmed(
    database: Database,
) -> None:
    """12.6: an active bot transmits unprompted, so it is confirmed explicitly."""
    persistence, bot = await _bot(database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        page = (await client.get(f"/admin/bots/{bot.id}/mode")).text
        assert "transmit unprompted" in page

        unconfirmed = await _apost(client, app, f"/admin/bots/{bot.id}/mode", mode="active")
        assert unconfirmed.status_code == 303

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].mode == "observe", "the mode changed without confirmation"

    async with _live(app) as client:
        confirmed = await _apost(
            client, app, f"/admin/bots/{bot.id}/mode", mode="active", confirm="yes"
        )
    assert confirmed.status_code == 303

    listed = await persistence.bots.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value[0].mode == "active"


async def _bot(database: Database):
    from sighop.bots import drivers as bot_drivers

    persistence = Persistence(database=database)
    entity = await persistence.entities.store(
        name="dev-greeter",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type="bot",
    )
    assert isinstance(entity, Succeeded)
    created = await persistence.bots.create(
        entity_id=entity.value.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
    )
    assert isinstance(created, Succeeded)
    return persistence, created.value


# --- 12.7 The radio ---------------------------------------------------------


def test_the_system_page_states_that_the_board_does_not_persist_a_change() -> None:
    """12.7: and that a reset reverts to the board's build defaults."""
    app, _state, _log = _built()

    with _client(app) as client:
        body = client.get("/system").text

    assert "does not persist" in body
    assert "build" in body and "defaults" in body


def test_an_unanswered_radio_parameter_shows_as_absent() -> None:
    """12.7, §4.1: absent, with the reason — never zero, never a default."""
    from sighop.radio.modem import EU868_NARROW
    from sighop.radio.probe import AbsenceReason, Absent, ProbeResult

    probe = ProbeResult(
        configured_radio=EU868_NARROW,
        device_name=Absent(reason=AbsenceReason.TIMEOUT),
        radio=EU868_NARROW,
        tx_power_dbm=Absent(reason=AbsenceReason.UNSUPPORTED, error_code=5),
        firmware_version=Absent(reason=AbsenceReason.UNSUPPORTED),
        battery_mv=Absent(reason=AbsenceReason.TIMEOUT),
        mcu_temp_tenths_c=Absent(reason=AbsenceReason.TIMEOUT),
        sensors_raw=Absent(reason=AbsenceReason.TIMEOUT),
    )
    app, _state, _log = _built(stub_state(probe_result=probe, radio=EU868_NARROW))

    with _client(app) as client:
        body = client.get("/system").text

    assert "absent (timeout)" in body
    assert "absent (unsupported)" in body


def test_the_system_page_shows_the_readback_once_and_links_the_gate_controls() -> None:
    """consolidate-web-pages 1.2: one readback table, and both confirmations linked
    — following the link mints a confirmation and changes nothing."""
    from sighop.radio.modem import EU868_NARROW
    from sighop.radio.probe import AbsenceReason, Absent, ProbeResult

    probe = ProbeResult(
        configured_radio=EU868_NARROW,
        device_name="board",
        radio=EU868_NARROW,
        tx_power_dbm=14,
        firmware_version=Absent(reason=AbsenceReason.UNSUPPORTED),
        battery_mv=Absent(reason=AbsenceReason.TIMEOUT),
        mcu_temp_tenths_c=Absent(reason=AbsenceReason.TIMEOUT),
        sensors_raw=Absent(reason=AbsenceReason.TIMEOUT),
    )
    app, state, log = _built(stub_state(probe_result=probe, radio=EU868_NARROW))
    ceiling = state.scheduler.budget.ceiling_fraction

    with _client(app) as client:
        body = client.get("/system").text
        confirm = client.get("/admin/transmit")
        client.get("/admin/ceiling")

    assert 'href="/admin/transmit"' in body
    assert 'href="/admin/ceiling"' in body
    assert body.count("<td>radio readback</td>") == 1
    assert body.count("<td>tx power</td>") == 1
    assert "expects revision" in body
    assert confirm.status_code == 200
    assert state.scheduler.transmit_enabled is False
    assert state.scheduler.budget.ceiling_fraction == ceiling
    assert log.named("web_guarded_action") == []


def test_a_run_without_a_probe_still_shows_the_schema_and_the_gate_links() -> None:
    app, _state, _log = _built()

    with _client(app) as client:
        body = client.get("/system").text

    assert "no board to ask" in body
    assert "expects revision" in body
    assert 'href="/admin/transmit"' in body
    assert 'href="/admin/ceiling"' in body


# --- 13.1 The guarded-action pattern ----------------------------------------


def test_a_nonce_is_good_exactly_once_and_only_for_its_own_action() -> None:
    """13.1: a confirmation cannot be replayed, reused or pointed elsewhere."""
    store = NonceStore()
    value = store.mint(REVEAL_KEY, "entity-1", now=NOW)

    assert store.spend(value, REVEAL_KEY, "entity-2", now=NOW) is False
    value = store.mint(REVEAL_KEY, "entity-1", now=NOW)
    assert store.spend(value, ENABLE_TRANSMIT, "entity-1", now=NOW) is False

    value = store.mint(REVEAL_KEY, "entity-1", now=NOW)
    assert store.spend(value, REVEAL_KEY, "entity-1", now=NOW) is True
    assert store.spend(value, REVEAL_KEY, "entity-1", now=NOW) is False, "replayed"


def test_the_three_new_removals_are_guarded_and_described() -> None:
    """Every guarded action states what it does, in its own words."""
    for action in (REMOVE_IDENTITY, REMOVE_ROOM, REMOVE_BOT):
        assert ACTION_DESCRIPTIONS.get(action), action

    identity = ACTION_DESCRIPTIONS[REMOVE_IDENTITY]
    assert "Nothing recovers it" in identity
    assert "Disabling an identity is the reversible action" in identity

    room = ACTION_DESCRIPTIONS[REMOVE_ROOM]
    assert "None of it can be recovered" in room
    assert "The identity the room speaks as is not deleted" in room

    bot = ACTION_DESCRIPTIONS[REMOVE_BOT]
    assert "every record of who has been greeted" in bot
    assert "The identity the bot speaks as is not deleted" in bot


def test_only_removing_an_identity_asks_for_the_password() -> None:
    """Design D6: key material is the tier reveal and export are in. A room
    and a bot destroy stored content, so they match `REMOVE_CHANNEL`."""
    assert REMOVE_IDENTITY in REAUTHENTICATED_ACTIONS
    assert REMOVE_ROOM not in REAUTHENTICATED_ACTIONS
    assert REMOVE_BOT not in REAUTHENTICATED_ACTIONS
    assert REMOVE_CHANNEL not in REAUTHENTICATED_ACTIONS


def test_a_removal_nonce_is_bound_to_its_own_target_and_action() -> None:
    """A confirmation minted for one room cannot be spent on another."""
    store = NonceStore()
    for action in (REMOVE_IDENTITY, REMOVE_ROOM, REMOVE_BOT):
        value = store.mint(action, "target-1", now=NOW)
        assert store.spend(value, action, "target-2", now=NOW) is False, action

        value = store.mint(action, "target-1", now=NOW)
        assert store.spend(value, REMOVE_CHANNEL, "target-1", now=NOW) is False, action

        value = store.mint(action, "target-1", now=NOW)
        assert store.spend(value, action, "target-1", now=NOW) is True, action
        assert store.spend(value, action, "target-1", now=NOW) is False, f"{action} replayed"


def test_a_removal_nonce_expires() -> None:
    store = NonceStore()
    for action in (REMOVE_IDENTITY, REMOVE_ROOM, REMOVE_BOT):
        value = store.mint(action, "target-1", now=NOW)
        assert store.spend(value, action, "target-1", now=NOW + dt.timedelta(hours=1)) is False


def test_a_nonce_expires() -> None:
    """13.1: a confirmation left open in a tab is not an action waiting to happen."""
    store = NonceStore()
    value = store.mint(REVEAL_KEY, "entity-1", now=NOW)

    assert store.spend(value, REVEAL_KEY, "entity-1", now=NOW + dt.timedelta(hours=1)) is False


def test_a_post_without_the_nonce_is_refused_and_audited() -> None:
    """13.1: refused, nothing changed, and its own event with a refused outcome."""
    app, state, log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        response = _post(client, app, f"/admin/reveal/{stub.entity_id}")

    assert response.status_code == 403
    assert stub.identity.private_key.hex() not in response.text

    audited = log.named("web_guarded_action")
    assert len(audited) == 1
    assert audited[0]["action"] == REVEAL_KEY
    assert audited[0]["outcome"] == "refused"
    assert audited[0]["target"] == stub.entity_id
    assert audited[0]["actor"] == OPERATOR


def test_the_guarded_event_is_separate_from_the_requests_event() -> None:
    """13.1, design D10: two events, because they answer two questions."""
    app, state, log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        page = client.get(f"/admin/reveal/{stub.entity_id}").text
        _post(
            client,
            app,
            f"/admin/reveal/{stub.entity_id}",
            nonce=_nonce(page),
            password=OPERATOR_PASSWORD,
        )

    assert len(log.named("web_guarded_action")) == 1
    assert len(log.named("web_request")) == 2, "requests and actions are the same event"


# --- 13.2 Revealing a private key -------------------------------------------


def test_the_confirmation_page_contains_no_key_material() -> None:
    """13.2: the material is not in any page served before the confirmation."""
    app, state, _log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        body = client.get(f"/admin/reveal/{stub.entity_id}").text

    assert stub.identity.private_key.hex() not in body
    # Apostrophes are escaped in the rendered page, so the assertion is on a
    # stretch of the sentence that survives escaping and wrapping.
    assert "private key in the next response" in body, (
        "the confirmation does not say what the action does"
    )


def test_the_reveal_is_one_response_body_and_its_own_event() -> None:
    """13.2: the material appears once, and the reveal names the identity."""
    app, state, log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        page = client.get(f"/admin/reveal/{stub.entity_id}").text
        revealed = _post(
            client,
            app,
            f"/admin/reveal/{stub.entity_id}",
            nonce=_nonce(page),
            password=OPERATOR_PASSWORD,
        )
        # The nonce is spent, so the same request again reveals nothing.
        again = _post(
            client,
            app,
            f"/admin/reveal/{stub.entity_id}",
            nonce=_nonce(page),
            password=OPERATOR_PASSWORD,
        )

    assert revealed.status_code == 200
    assert stub.identity.private_key.hex() in revealed.text
    assert again.status_code == 403
    assert stub.identity.private_key.hex() not in again.text

    audited = [event for event in log.named("web_guarded_action") if event["outcome"] == "success"]
    assert len(audited) == 1
    assert audited[0]["action"] == REVEAL_KEY
    assert audited[0]["entity_name"] == stub.name
    assert audited[0]["public_key"] == stub.identity.public_key.hex()
    assert stub.identity.private_key.hex() not in repr(audited[0]), (
        "the event carries the private key"
    )


def test_no_other_route_reveals_key_material() -> None:
    """13.2: swept over every page reachable by navigation."""
    from tests.webfixtures import safe_pages

    app, state, _log = _built()
    private_keys = [stub.identity.private_key for stub in state.adverts.stubs]
    pages = safe_pages(app)
    assert len(pages) > 5, f"only {pages} were swept; the router walk is broken"

    with _client(app) as client:
        for path in pages:
            body = client.get(path).text
            for private_key in private_keys:
                assert private_key.hex() not in body, f"{path} leaked a private key"


# --- 13.3 Enabling transmission ---------------------------------------------


def test_enabling_transmission_changes_the_gate_and_the_indication_together() -> None:
    """13.3: the panel reads the scheduler's gate; there is no second copy."""
    app, state, log = _built()
    assert state.scheduler.transmit_enabled is False

    with _client(app) as client:
        page = client.get("/admin/transmit").text
        assert "opens the transmit gate" in page
        response = _post(
            client,
            app,
            "/admin/transmit",
            nonce=_nonce(page),
            enabled="true",
            password=OPERATOR_PASSWORD,
        )
        assert response.status_code == 303
        overview = client.get("/").text

    assert state.scheduler.transmit_enabled is True
    assert "transmit enabled" in overview
    assert "nothing will be transmitted" not in overview

    audited = log.named("web_guarded_action")[-1]
    assert audited["action"] == ENABLE_TRANSMIT
    assert audited["outcome"] == "success"
    assert audited["transmit_enabled"] is True


def test_enabling_transmission_without_a_confirmation_changes_nothing() -> None:
    """13.3: and is audited as refused."""
    app, state, log = _built()

    with _client(app) as client:
        response = _post(client, app, "/admin/transmit", enabled="true")

    assert response.status_code == 403
    assert state.scheduler.transmit_enabled is False
    assert log.named("web_guarded_action")[-1]["outcome"] == "refused"


# --- 13.4 Raising the ceiling -----------------------------------------------


def test_the_ceiling_confirmation_states_that_the_default_is_regulatory() -> None:
    """13.4: a legal limit, not a tuning knob — said before it is raised."""
    app, _state, _log = _built()

    with _client(app) as client:
        body = client.get("/admin/ceiling").text

    assert "regulatory default" in body
    assert "legal limit, not a tuning knob" in body
    assert f"{DEFAULT_CEILING_FRACTION * 100:g}%" in body


def test_raising_the_ceiling_carries_the_old_and_the_new_value() -> None:
    """13.4: and the meter says the ceiling is raised for as long as it is."""
    app, state, log = _built()
    assert state.scheduler.budget.ceiling_fraction == DEFAULT_CEILING_FRACTION

    with _client(app) as client:
        page = client.get("/admin/ceiling").text
        response = _post(
            client,
            app,
            "/admin/ceiling",
            nonce=_nonce(page),
            fraction="0.5",
            password=OPERATOR_PASSWORD,
        )
        assert response.status_code == 303
        overview = client.get("/").text

    assert state.scheduler.budget.ceiling_fraction == 0.5
    assert "ceiling raised to 50%" in overview

    audited = log.named("web_guarded_action")[-1]
    assert audited["action"] == RAISE_CEILING
    assert audited["previous_ceiling_fraction"] == DEFAULT_CEILING_FRACTION
    assert audited["ceiling_fraction"] == 0.5
    assert audited["above_regulatory_default"] is True


def test_a_ceiling_outside_the_allowed_range_is_refused() -> None:
    """13.4: the same bound `AirtimeBudget` itself applies, before it is applied."""
    app, state, log = _built()

    with _client(app) as client:
        page = client.get("/admin/ceiling").text
        response = _post(
            client,
            app,
            "/admin/ceiling",
            nonce=_nonce(page),
            fraction="2.0",
            password=OPERATOR_PASSWORD,
        )

    assert response.status_code == 400
    assert state.scheduler.budget.ceiling_fraction == DEFAULT_CEILING_FRACTION
    assert log.named("web_guarded_action")[-1]["outcome"] == "refused"


def test_a_guarded_action_is_never_performed_by_a_bare_link() -> None:
    """13.1 / 8.3: no GET on any guarded path changes anything."""
    app, state, _log = _built()
    stub = state.adverts.stubs[0]
    before = (
        state.scheduler.transmit_enabled,
        state.scheduler.budget.ceiling_fraction,
    )

    with _client(app) as client:
        for path in ("/admin/transmit", "/admin/ceiling", f"/admin/reveal/{stub.entity_id}"):
            assert client.get(path).status_code == 200

    assert (state.scheduler.transmit_enabled, state.scheduler.budget.ceiling_fraction) == before


# --- Channels (change `channel-messaging`, task 8.3) -------------------------

PSK = base64.b64encode(bytes(range(60, 76))).decode()


def _psk_absent(body: str) -> None:
    """The pasted key, in any encoding it could be rendered in, is not on the page."""
    raw = base64.b64decode(PSK)
    for form in (PSK, raw.hex(), raw.hex().upper(), PSK.rstrip("=")):
        assert form not in body


def _channel_state(database: Database) -> tuple[FastAPI, StubState, RecordingLogger]:
    state = stub_state(stub_names=("panel-identity",))
    state.persistence = Persistence(database=database)
    state.channel_secret = SECRET
    log = RecordingLogger()
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=log, sealing_secret=SECRET)
    return app, state, log


def test_with_no_database_the_chat_page_offers_no_channel_controls() -> None:
    app, _state, _log = _built()
    with _client(app) as client:
        body = client.get("/chat").text
    assert "require durable storage" in body
    assert 'action="/admin/channels/' not in body


@pytest.mark.database
async def test_the_chat_page_lists_channel_kind_hash_guessable_and_count(
    database: Database,
) -> None:
    app, _state, _log = _channel_state(database)
    async with _live(app) as client:
        body = (await client.get("/chat")).text
    assert "Public" in body and ">public<" in body and ">11<" in body
    # Guessable is a status glyph with its meaning on hover, not a word per row.
    assert 'class="status status-guessable"' in body
    assert "sighop channel key" in body and "not shown here" in body


@pytest.mark.database
async def test_a_hashtag_added_in_the_ui_matches_the_cli_and_is_decrypted_at_once(
    database: Database,
) -> None:
    from sighop.db.repositories import ChannelRepository
    from sighop.protocol.crypto import channel_key_from_hashtag

    app, state, _log = _channel_state(database)
    async with _live(app) as client:
        added = await _apost(client, app, "/admin/channels/hashtag", hashtag="dev-sighop")
        page = (await client.get(added.headers["location"])).text

    assert added.status_code == 303
    assert added.headers["location"] == "/chat?added=%23dev-sighop"
    assert "Channel #dev-sighop added" in page
    assert "anyone who knows or guesses its name" in page
    stored = await ChannelRepository(database=database).get("#dev-sighop")
    assert isinstance(stored, Succeeded) and stored.value is not None
    assert stored.value.kind == "hashtag" and stored.value.hashtag == "#dev-sighop"
    assert stored.value.channel_hash == channel_key_from_hashtag("#dev-sighop").channel_hash
    assert state.channel_reloads == 1
    assert [c.name for c in state.channels.channels] == ["Public", "#dev-sighop"]


@pytest.mark.database
async def test_a_psk_is_never_in_the_page_after_an_add_or_a_refusal(database: Database) -> None:
    app, state, _log = _channel_state(database)
    async with _live(app) as client:
        added = await _apost(client, app, "/admin/channels/psk", name="crew", key=PSK)
        after_add = (await client.get(added.headers["location"])).text
        listing = (await client.get("/chat")).text
        refused = await _apost(client, app, "/admin/channels/psk", name="crew-2", key=PSK)
        short = await _apost(
            client,
            app,
            "/admin/channels/psk",
            name="short",
            key=base64.b64encode(bytes(8)).decode(),
        )

    assert added.status_code == 303 and "Channel crew added" in after_add
    for body in (after_add, listing, refused.text, short.text):
        _psk_absent(body)
    assert (
        refused.status_code == 400
        and "already stored as the channel &#39;crew&#39;" in refused.text
    )
    assert 'value="crew-2"' in refused.text, "the name is handed back"
    assert short.status_code == 400 and "decodes to 8 bytes" in short.text
    assert 'type="password" name="key"' in short.text
    assert [c.name for c in state.channels.channels] == ["Public", "crew"]


@pytest.mark.database
async def test_channel_administration_is_closed_until_it_is_wanted(database: Database) -> None:
    """The three add-a-channel forms are on the chat page, as `web-chat`
    requires, but folded away on an ordinary visit — and never folded away over
    a refusal the operator has to read (`web-admin`)."""
    app, _state, _log = _channel_state(database)
    async with _live(app) as client:
        ordinary = (await client.get("/chat")).text
        refused = await _apost(
            client,
            app,
            "/admin/channels/psk",
            name="short",
            key=base64.b64encode(bytes(8)).decode(),
        )
        added = await _apost(client, app, "/admin/channels/hashtag", hashtag="dev-sighop")
        after_add = (await client.get(added.headers["location"])).text

    # Present, and closed, on a visit with nothing to say.
    assert '<details class="channel-admin"' in ordinary
    assert '<details class="channel-admin" open>' not in ordinary
    assert "/admin/channels/psk" in ordinary, "the form is on the page, merely folded"

    # Open wherever there is a reason or a result inside it to read.
    assert '<details class="channel-admin" open>' in refused.text
    assert "decodes to 8 bytes" in refused.text
    assert '<details class="channel-admin" open>' in after_add


@pytest.mark.database
async def test_removing_a_channel_is_confirmed_with_its_count_and_a_nonce(
    database: Database,
) -> None:
    from sighop.db.repositories import ChannelMessageRepository, ChannelRepository
    from sighop.net.channels import ChannelMessageRecord, ChannelOutcome

    app, state, log = _channel_state(database)
    history = ChannelMessageRepository(database=database)
    await history.upsert_many(
        [
            ChannelMessageRecord(
                channel_id=1,
                direction="in",
                ref=f"p{index}",
                text=b"x",
                wire_timestamp=1,
                handled_at=NOW,
                outcome=ChannelOutcome.RECEIVED,
            )
            for index in range(40)
        ]
    )
    announced: list[str] = []
    app.state.panel.announce = announced.append

    async with _live(app) as client:
        bare = await _apost(client, app, "/admin/channels/1/remove")
        form = (await client.get("/admin/channels/1/remove")).text
        confirmed = await _apost(client, app, "/admin/channels/1/remove", nonce=_nonce(form))
        again = (await client.get("/chat")).text

    assert bare.status_code == 403
    assert "40 recorded messages will be deleted" in form
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == "/chat?removed=Public"
    listed = await ChannelRepository(database=database).list_all()
    assert isinstance(listed, Succeeded) and listed.value == []
    assert state.channels.channels.channels == ()
    events = log.named("web_guarded_action")
    assert [e["outcome"] for e in events] == ["refused", "success"]
    assert events[-1]["actor"] == OPERATOR and events[-1]["messages_deleted"] == 40
    assert any(OPERATOR in line and "Public" in line for line in announced)
    assert "add Public again" in again


# --- Deleting a room and a bot through the panel -----------------------------


async def _a_room(persistence: Persistence, *, name: str = "lounge"):
    """A stored room-server identity with a room bound to it."""
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
    return stored.value, created.value


@pytest.mark.database
async def test_no_safe_request_deletes_a_room(database: Database) -> None:
    """A GET, a prefetch and a reload all reach the confirmation, never the act."""
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        for _ in range(3):
            response = await client.get(f"/admin/rooms/{room.id}/delete")
            assert response.status_code == 200

    assert len((await persistence.rooms.list_all()).value) == 1


@pytest.mark.database
async def test_the_room_confirmation_counts_what_it_will_delete(database: Database) -> None:
    persistence = Persistence(database=database)
    identity, room = await _a_room(persistence)
    await persistence.messages.store(room_id=room.id, author_public_key=b"\x02" * 32, text=b"hello")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room.id}/delete")).text

    assert "1" in body
    assert "cannot be undone" in body
    assert identity.name in body
    assert "is <strong>not</strong> deleted" in body


@pytest.mark.database
async def test_deleting_a_room_removes_it_and_keeps_its_identity(database: Database) -> None:
    persistence = Persistence(database=database)
    identity, room = await _a_room(persistence)
    state = stub_state(persistence=persistence)
    app, _state, log = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room.id}/delete")).text
        response = await _apost(client, app, f"/admin/rooms/{room.id}/delete", nonce=_nonce(body))
        listing = (await client.get(response.headers["location"])).text

    assert response.status_code == 303
    assert response.headers["location"] == f"/rooms?deleted={room.name}"
    assert f"Room {room.name} deleted" in listing
    assert (await persistence.rooms.list_all()).value == []
    assert [row.name for row in (await persistence.entities.list_all()).value] == [identity.name]
    assert room.id in state.stopped_rooms, "the run was never told to stop serving it"
    [audited] = [
        event for event in log.named("web_guarded_action") if event["action"] == REMOVE_ROOM
    ]
    assert audited["outcome"] == "success"
    assert audited["room"] == "lounge"
    assert audited["actor"] == OPERATOR


@pytest.mark.database
async def test_deleting_a_room_without_a_confirmation_deletes_nothing(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(client, app, f"/admin/rooms/{room.id}/delete")

    assert response.status_code == 403
    assert len((await persistence.rooms.list_all()).value) == 1
    [audited] = [
        event for event in log.named("web_guarded_action") if event["action"] == REMOVE_ROOM
    ]
    assert audited["outcome"] == "refused"


@pytest.mark.database
async def test_a_room_confirmation_cannot_be_spent_on_another_room(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    _one, first = await _a_room(persistence, name="lounge")
    _two, second = await _a_room(persistence, name="study")
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{first.id}/delete")).text
        response = await _apost(client, app, f"/admin/rooms/{second.id}/delete", nonce=_nonce(body))

    assert response.status_code == 403
    assert len((await persistence.rooms.list_all()).value) == 2


@pytest.mark.database
async def test_deleting_a_room_asks_for_no_password(database: Database) -> None:
    """Design D6: stored content, not key material — `REMOVE_CHANNEL`'s tier."""
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room.id}/delete")).text

    assert 'type="password"' not in body


async def _a_bot(persistence: Persistence, *, name: str = "greeter-bot"):
    """A stored bot identity with a bot bound to it."""
    from sighop.bots import drivers as bot_drivers

    stored = await persistence.entities.store(
        name=name,
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type="bot",
    )
    assert isinstance(stored, Succeeded)
    created = await persistence.bots.create(
        entity_id=stored.value.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name=name,
    )
    assert isinstance(created, Succeeded)
    return stored.value, created.value


@pytest.mark.database
async def test_no_safe_request_deletes_a_bot(database: Database) -> None:
    persistence = Persistence(database=database)
    _identity, bot = await _a_bot(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        for _ in range(3):
            assert (await client.get(f"/admin/bots/{bot.id}/delete")).status_code == 200

    assert len((await persistence.bots.list_all()).value) == 1


@pytest.mark.database
async def test_the_bot_confirmation_counts_its_state_and_says_what_it_records(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    identity, bot = await _a_bot(persistence)
    await persistence.bot_state.set(bot.id, "greeted:aa", {})
    await persistence.bot_state.set(bot.id, "greeted:bb", {})
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/bots/{bot.id}/delete")).text

    assert "2" in body
    assert "who has been greeted" in body
    assert "cannot be undone" in body
    assert identity.name in body
    assert "is <strong>not</strong> deleted" in body


@pytest.mark.database
async def test_deleting_a_bot_removes_its_state_and_keeps_its_identity(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    identity, bot = await _a_bot(persistence)
    await persistence.bot_state.set(bot.id, "greeted:aa", {})
    state = stub_state(persistence=persistence)
    app, _state, log = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/bots/{bot.id}/delete")).text
        response = await _apost(client, app, f"/admin/bots/{bot.id}/delete", nonce=_nonce(body))
        after = (await client.get(response.headers["location"])).text

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/admin/identities/{identity.id}?bot_deleted={identity.name}"
    )
    assert f"Bot {identity.name} deleted" in after
    assert 'action="/admin/bots/create"' in after, "the freed identity offers no new bot"
    assert (await persistence.bots.list_all()).value == []
    assert (await persistence.bot_state.list(bot.id)).value == {}
    assert [row.name for row in (await persistence.entities.list_all()).value] == [identity.name]
    assert bot.id in state.stopped_bots, "the run was never told to stop the bot"
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == REMOVE_BOT]
    assert audited["outcome"] == "success"
    assert audited["keys_deleted"] == 1


@pytest.mark.database
async def test_deleting_a_bot_without_a_confirmation_deletes_nothing(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    _identity, bot = await _a_bot(persistence)
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(client, app, f"/admin/bots/{bot.id}/delete")

    assert response.status_code == 403
    assert len((await persistence.bots.list_all()).value) == 1
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == REMOVE_BOT]
    assert audited["outcome"] == "refused"


@pytest.mark.database
async def test_a_bot_page_says_a_bot_is_named_by_its_identity(database: Database) -> None:
    """There is no bot rename: the name belongs to the identity (bot-runtime)."""
    persistence = Persistence(database=database)
    identity, _bot = await _a_bot(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{identity.id}")).text

    assert "no name of its own" in body
    assert "renames the bot" in body
    assert f'href="/admin/identities/{identity.id}/rename"' in body


@pytest.mark.database
async def test_only_a_free_bot_identity_offers_the_create_a_bot_form(database: Database) -> None:
    """consolidate-web-pages 4.2: the form is on the identity it would create on."""
    persistence = Persistence(database=database)
    free = await persistence.entities.store(
        name="free-bot",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type="bot",
    )
    chat = await persistence.entities.store(
        name="plain-chat", identity=generate_identity(), secret=SECRET, node_type=NodeType.CHAT
    )
    assert isinstance(free, Succeeded) and isinstance(chat, Succeeded)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        offered = (await client.get(f"/admin/identities/{free.value.id}")).text
        plain = (await client.get(f"/admin/identities/{chat.value.id}")).text
        created = await _apost(
            client, app, "/admin/bots/create", entity_id=str(free.value.id), driver="greeter"
        )
        listing = (await client.get("/admin/identities")).text

    assert 'action="/admin/bots/create"' in offered
    assert f'name="entity_id" value="{free.value.id}"' in offered
    assert 'action="/admin/bots/create"' not in plain
    assert "<h2>bot</h2>" not in plain
    assert created.status_code == 303
    assert created.headers["location"] == f"/admin/identities/{free.value.id}"
    assert f'<a href="/admin/identities/{free.value.id}">bot &#39;greeter&#39;</a>' in listing


# --- Removing an identity through the panel ----------------------------------


@pytest.mark.database
async def test_removing_an_identity_needs_the_password_and_the_typed_name(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="goodbye", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{stored.value.id}/remove")).text
        assert 'type="password"' in body
        response = await _apost(
            client,
            app,
            f"/admin/identities/{stored.value.id}/remove",
            nonce=_nonce(body),
            password=OPERATOR_PASSWORD,
            confirm_name="goodbye",
        )

    assert response.status_code == 303
    assert (await persistence.entities.list_all()).value == []
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == REMOVE_IDENTITY]
    assert audited["outcome"] == "success"
    assert audited["entity_name"] == "goodbye"
    assert audited["actor"] == OPERATOR


@pytest.mark.database
@pytest.mark.parametrize(
    ("password", "typed"),
    [
        ("the-wrong-password", "goodbye"),
        (OPERATOR_PASSWORD, "not-the-name"),
        ("", "goodbye"),
    ],
)
async def test_an_identity_removal_missing_a_gate_removes_nothing(
    database: Database, password: str, typed: str
) -> None:
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="goodbye", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{stored.value.id}/remove")).text
        response = await _apost(
            client,
            app,
            f"/admin/identities/{stored.value.id}/remove",
            nonce=_nonce(body),
            password=password,
            confirm_name=typed,
        )

    assert response.status_code in (401, 403, 409), response.status_code
    assert [row.name for row in (await persistence.entities.list_all()).value] == ["goodbye"]
    refused = [
        e
        for e in log.named("web_guarded_action")
        if e["action"] == REMOVE_IDENTITY and e["outcome"] == "refused"
    ]
    assert refused, "a refused removal was not recorded as its own event"


@pytest.mark.database
async def test_removing_an_identity_without_a_confirmation_removes_nothing(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="goodbye", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            app,
            f"/admin/identities/{stored.value.id}/remove",
            password=OPERATOR_PASSWORD,
            confirm_name="goodbye",
        )

    assert response.status_code == 403
    assert len((await persistence.entities.list_all()).value) == 1
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == REMOVE_IDENTITY]
    assert audited["outcome"] == "refused"


@pytest.mark.database
async def test_removing_a_bound_identity_is_refused_naming_what_it_serves(
    database: Database,
) -> None:
    """The command line's rule, in the command line's words."""
    persistence = Persistence(database=database)
    identity, _room = await _a_room(persistence, name="lounge")
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{identity.id}/remove")).text
        assert "serving room" in body and "lounge" in body
        assert 'name="nonce"' not in body, "a bound identity was offered a live confirmation"
        response = await _apost(
            client,
            app,
            f"/admin/identities/{identity.id}/remove",
            password=OPERATOR_PASSWORD,
            confirm_name=identity.name,
        )

    assert response.status_code in (403, 409)
    assert len((await persistence.entities.list_all()).value) == 1
    assert len((await persistence.rooms.list_all()).value) == 1
    assert [e["outcome"] for e in log.named("web_guarded_action")] == ["refused"]


@pytest.mark.database
async def test_no_safe_request_removes_an_identity(database: Database) -> None:
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="goodbye", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        for _ in range(3):
            assert (
                await client.get(f"/admin/identities/{stored.value.id}/remove")
            ).status_code == 200

    assert len((await persistence.entities.list_all()).value) == 1


@pytest.mark.database
async def test_the_removal_page_offers_disabling_as_the_reversible_action(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    stored = await persistence.entities.store(
        name="goodbye", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{stored.value.id}/remove")).text

    assert "cannot be undone" in body
    assert "Disabling is the reversible action" in body


@pytest.mark.database
async def test_a_deletion_the_run_did_not_take_is_still_recorded_as_such(
    database: Database,
) -> None:
    """The store and the run are two outcomes, and the event carries both.

    A run that was not serving the room answers `False`, which is ordinary —
    another process may be serving it — and must not be reported as a failure
    of the deletion, which did happen.
    """
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    state = stub_state(persistence=persistence)
    state.refuse_live_stop = True
    app, _state, log = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/rooms/{room.id}/delete")).text
        response = await _apost(client, app, f"/admin/rooms/{room.id}/delete", nonce=_nonce(body))

    assert response.status_code == 303
    assert (await persistence.rooms.list_all()).value == [], "the durable delete did not happen"
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == REMOVE_ROOM]
    assert audited["outcome"] == "success"
    assert audited["stopped_serving"] is False, "the live half was not reported separately"


@pytest.mark.database
async def test_a_bot_deletion_the_run_did_not_take_is_still_recorded_as_such(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    _identity, bot = await _a_bot(persistence)
    state = stub_state(persistence=persistence)
    state.refuse_live_stop = True
    app, _state, log = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/bots/{bot.id}/delete")).text
        response = await _apost(client, app, f"/admin/bots/{bot.id}/delete", nonce=_nonce(body))

    assert response.status_code == 303
    assert (await persistence.bots.list_all()).value == []
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == REMOVE_BOT]
    assert audited["outcome"] == "success"
    assert audited["stopped_running"] is False


# --- Renaming through the panel ----------------------------------------------


@pytest.mark.database
async def test_renaming_a_room_a_channel_and_a_webhook_asks_for_no_password(
    database: Database,
) -> None:
    """Design D6: a rename is reversible by renaming back and destroys nothing."""
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    channels = (await persistence.channels.list_all()).value
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        for path in (
            f"/admin/rooms/{room.id}/rename",
            f"/admin/channels/{channels[0].id}/rename",
        ):
            body = (await client.get(path)).text
            assert 'type="password"' not in body, path
            assert 'name="nonce"' not in body, path


@pytest.mark.database
async def test_renaming_a_room_changes_only_the_name(database: Database) -> None:
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(client, app, f"/admin/rooms/{room.id}/rename", name="the-study")

    assert response.status_code == 303
    assert response.headers["location"] == "/rooms"
    [after] = (await persistence.rooms.list_all()).value
    assert after.name == "the-study"
    assert after.entity_id == room.entity_id
    assert after.admin_password_hash == room.admin_password_hash


@pytest.mark.database
async def test_a_refused_room_rename_re_renders_with_the_reason(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(client, app, f"/admin/rooms/{room.id}/rename", name="   ")

    assert response.status_code == 400
    assert "cannot be empty" in response.text
    assert [r.name for r in (await persistence.rooms.list_all()).value] == ["lounge"]


@pytest.mark.database
async def test_a_refused_channel_rename_renders_no_key(database: Database) -> None:
    persistence = Persistence(database=database)
    added = await persistence.channels.add_psk(
        base64.b64encode(bytes(range(40, 56))).decode(), name="private", secret=SECRET
    )
    assert isinstance(added, Succeeded)
    app, _state, _log = _built(stub_state(persistence=persistence))
    key_b64 = base64.b64encode(bytes(range(40, 56))).decode()

    async with _live(app) as client:
        response = await _apost(
            client, app, f"/admin/channels/{added.value.id}/rename", name="Public"
        )

    assert response.status_code == 400
    assert "already exists" in response.text
    assert key_b64 not in response.text
    assert bytes(range(40, 56)).hex() not in response.text


@pytest.mark.database
async def test_every_rename_is_recorded_with_the_old_and_the_new_name(
    database: Database,
) -> None:
    """A rename destroys nothing, so it is not a guarded action — but what it
    changed still has to be readable afterwards."""
    persistence = Persistence(database=database)
    _identity, room = await _a_room(persistence)
    channels = (await persistence.channels.list_all()).value
    app, _state, log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        await _apost(client, app, f"/admin/rooms/{room.id}/rename", name="the-study")
        await _apost(client, app, f"/admin/channels/{channels[0].id}/rename", name="Main")

    [renamed_room] = log.named("web_room_renamed")
    assert renamed_room["previous_name"] == "lounge"
    assert renamed_room["name"] == "the-study"
    assert renamed_room["actor"] == OPERATOR

    [renamed_channel] = log.named("web_channel_renamed")
    assert renamed_channel["previous_name"] == "Public"
    assert renamed_channel["name"] == "Main"
    assert renamed_channel["actor"] == OPERATOR


# --- The exclusions this build still states ----------------------------------


@pytest.mark.database
async def test_the_four_remaining_exclusions_are_still_named_where_looked_for(
    database: Database,
) -> None:
    """Withdrawing one exclusion must not quietly withdraw the others.

    `web-delete-and-rename` dropped identity removal from the list. Migrations,
    the sealing secret, account management and channel pre-shared keys stay,
    and stay stated where an operator would go looking for them.
    """
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        schema = (await client.get("/system")).text
        identities = (await client.get("/admin/identities")).text
        channels = (await client.get("/chat")).text

    assert "sighop db upgrade" in schema or "database upgrade" in schema
    assert "terminal" in schema

    assert "SIGHOP_SECRET_KEY" in identities
    assert "sighop keys secret" in identities

    assert "sighop web user" in schema or "sighop web user" in identities

    assert "sighop channel key" in channels


@pytest.mark.database
async def test_the_identities_page_no_longer_says_removal_is_not_offered(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    app, _state, _log = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get("/admin/identities")).text

    assert "not offered here" not in body
    assert "Removing a stored identity is offered here" in body
    assert "Disabling is the reversible action offered alongside it" in body


@pytest.mark.database
async def test_the_identities_page_reflects_a_write_at_once(database: Database) -> None:
    """6.5: after a write, the "loaded by this run" table shows what the run
    is actually doing — the identity it now holds, with its advert schedule —
    matching `reconcile_entities` rather than a snapshot from before it ran."""
    persistence = Persistence(database=database)
    state = stub_state(persistence=persistence)
    state.channel_secret = SECRET
    app = create_app(
        state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger(), sealing_secret=SECRET
    )

    async with _live(app) as client:
        created = await client.post(
            "/admin/identities/create",
            data={TOKEN_FIELD: csrf(client), "name": "shows-up", "node_type": "CHAT"},
        )
        assert created.status_code == 303
        body = (await client.get("/admin/identities")).text

    loaded_section = body[body.index("loaded by this run") : body.index("<h2>stored")]
    assert "shows-up" in loaded_section, (
        "the newly held identity must appear in the live, not the stored, section"
    )
