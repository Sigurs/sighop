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
    ENABLE_TRANSMIT,
    RAISE_CEILING,
    REVEAL_KEY,
    NonceStore,
)
from tests.test_web_state import RecordingLogger
from tests.webfixtures import StubState, stub_state

HOSTS = allowed_hosts("127.0.0.1", 8080)
SECRET = base64.b64decode(generate_secret_key())
NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def _built(
    state: StubState | None = None, logger: RecordingLogger | None = None
) -> tuple[FastAPI, StubState, RecordingLogger]:
    panel_state = state or stub_state(stub_names=("panel-identity",))
    log = logger or RecordingLogger()
    return create_app(panel_state, hosts=HOSTS, logger=log), panel_state, log


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8080", follow_redirects=False)


def _live(app: FastAPI) -> httpx2.AsyncClient:
    """A client that drives the app in **this** event loop.

    `TestClient` runs the application in a loop of its own, which is fine for a
    page over a stub state and wrong for one over a database: the async engine
    is bound to the test's loop, and a request served on another one meets a
    closed loop. Every database-backed page here uses this instead.
    """
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    )


def _post(client: TestClient, app: FastAPI, path: str, **fields: str):
    return client.post(path, data={TOKEN_FIELD: app.state.token, **fields})


async def _apost(client: httpx2.AsyncClient, app: FastAPI, path: str, **fields: str):
    return await client.post(path, data={TOKEN_FIELD: app.state.token, **fields})


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
    """12.1, `web-admin`: an unencrypted seed, protected only by permissions."""
    app, _state, _log = _built()

    with _client(app) as client:
        body = client.get("/admin/identities").text

    assert "unencrypted seed" in body
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
        body = (await client.get("/admin/rooms")).text

    assert "[redacted]" in body
    assert "not by this run" in body, "an unserved room is not marked as served"


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

    assert "3</strong> stored message(s)" in body
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
        assert secret_password not in response.text
        page = (await client.get("/admin/rooms")).text

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
async def test_the_bots_page_shows_the_greeting_records(database: Database) -> None:
    """12.5: the records that decide whether a bot acts on a contact again."""
    persistence, bot = await _bot(database)
    assert isinstance(
        await persistence.bot_state.set(bot.id, "greeted:aabb", {"outcome": "acknowledged"}),
        Succeeded,
    )

    app, _state, _log = _built(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get("/admin/bots")).text

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
            configuration='{"burst": "not a number"}',
        )
    assert response.status_code == 303

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
        response = await _apost(
            client,
            app,
            f"/admin/bots/{bot.id}/config",
            configuration='{"burst": 3, "rate_per_hour": 2}',
        )
    assert response.status_code == 303

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


def test_the_radio_page_states_that_the_board_does_not_persist_a_change() -> None:
    """12.7: and that a reset reverts to the board's build defaults."""
    app, _state, _log = _built()

    with _client(app) as client:
        body = client.get("/admin/radio").text

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
        body = client.get("/admin/radio").text

    assert "absent (timeout)" in body
    assert "absent (unsupported)" in body


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
    assert stub.identity.seed.hex() not in response.text

    audited = log.named("web_guarded_action")
    assert len(audited) == 1
    assert audited[0]["action"] == REVEAL_KEY
    assert audited[0]["outcome"] == "refused"
    assert audited[0]["target"] == stub.entity_id
    assert audited[0]["actor"] == "unauthenticated"


def test_the_guarded_event_is_separate_from_the_requests_event() -> None:
    """13.1, design D10: two events, because they answer two questions."""
    app, state, log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        page = client.get(f"/admin/reveal/{stub.entity_id}").text
        _post(client, app, f"/admin/reveal/{stub.entity_id}", nonce=_nonce(page))

    assert len(log.named("web_guarded_action")) == 1
    assert len(log.named("web_request")) == 2, "requests and actions are the same event"


# --- 13.2 Revealing a private key -------------------------------------------


def test_the_confirmation_page_contains_no_key_material() -> None:
    """13.2: the material is not in any page served before the confirmation."""
    app, state, _log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        body = client.get(f"/admin/reveal/{stub.entity_id}").text

    assert stub.identity.seed.hex() not in body
    # Apostrophes are escaped in the rendered page, so the assertion is on a
    # stretch of the sentence that survives escaping and wrapping.
    assert "private seed in the next response" in body, (
        "the confirmation does not say what the action does"
    )


def test_the_reveal_is_one_response_body_and_its_own_event() -> None:
    """13.2: the material appears once, and the reveal names the identity."""
    app, state, log = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        page = client.get(f"/admin/reveal/{stub.entity_id}").text
        revealed = _post(client, app, f"/admin/reveal/{stub.entity_id}", nonce=_nonce(page))
        # The nonce is spent, so the same request again reveals nothing.
        again = _post(client, app, f"/admin/reveal/{stub.entity_id}", nonce=_nonce(page))

    assert revealed.status_code == 200
    assert stub.identity.seed.hex() in revealed.text
    assert again.status_code == 403
    assert stub.identity.seed.hex() not in again.text

    audited = [
        event for event in log.named("web_guarded_action") if event["outcome"] == "success"
    ]
    assert len(audited) == 1
    assert audited[0]["action"] == REVEAL_KEY
    assert audited[0]["entity_name"] == stub.name
    assert audited[0]["public_key"] == stub.identity.public_key.hex()
    assert stub.identity.seed.hex() not in repr(audited[0]), "the event carries the seed"


def test_no_other_route_reveals_key_material() -> None:
    """13.2: swept over every page reachable by navigation."""
    from starlette.routing import Route

    app, state, _log = _built()
    seeds = [stub.identity.seed for stub in state.adverts.stubs]

    with _client(app) as client:
        for route in app.routes:
            if not isinstance(route, Route) or "{" in route.path:
                continue
            if "GET" not in (route.methods or set()):
                continue
            body = client.get(route.path).text
            for seed in seeds:
                assert seed.hex() not in body, f"{route.path} leaked a seed"


# --- 13.3 Enabling transmission ---------------------------------------------


def test_enabling_transmission_changes_the_gate_and_the_indication_together() -> None:
    """13.3: the panel reads the scheduler's gate; there is no second copy."""
    app, state, log = _built()
    assert state.scheduler.transmit_enabled is False

    with _client(app) as client:
        page = client.get("/admin/transmit").text
        assert "opens the transmit gate" in page
        response = _post(client, app, "/admin/transmit", nonce=_nonce(page), enabled="true")
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
        response = _post(client, app, "/admin/ceiling", nonce=_nonce(page), fraction="0.5")
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
        response = _post(client, app, "/admin/ceiling", nonce=_nonce(page), fraction="2.0")

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
