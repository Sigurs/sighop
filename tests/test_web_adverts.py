"""Adverting now from the panel (`web-admin`, change web-advert-now).

Two confirmation-only guarded actions per loaded identity — a zero-hop advert
and a flood advert — each confirmed on its own page, each spending a nonce
minted for that action and that identity, and each refused with nothing
submitted when the gate is closed, the board has not answered, the identity is
not held, or (for a flood) another flood from this run is inside the gap.
"""

from __future__ import annotations

import base64
import datetime as dt

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sighop.bots import drivers as bot_drivers
from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import BOT_ENTITY_TYPE
from sighop.net.bus import Submission, TxHandle
from sighop.protocol.identity import generate_identity
from sighop.protocol.packet import RouteType
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.payloads import NodeType
from sighop.radio.modem import EU868_NARROW
from sighop.web.app import allowed_hosts, create_app
from sighop.web.guard import TOKEN_FIELD
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    ADVERT_FLOOD,
    ADVERT_ZERO_HOP,
    REAUTHENTICATED_ACTIONS,
)
from sighop.web.render import render_advert_request
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    OPERATOR,
    StubState,
    authenticator,
    csrf,
    signed_async_client,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)
SECRET = base64.b64decode(generate_secret_key())


class Recorder:
    """The advert scheduler's `submit`, recorded and passed on to the real bus."""

    def __init__(self, state: StubState) -> None:
        self.submissions: list[Submission] = []
        self._onward = state.adverts.submit
        state.adverts.submit = self

    def __call__(self, submission: Submission) -> TxHandle:
        self.submissions.append(submission)
        return self._onward(submission)  # type: ignore[operator]


def _state(**kwargs: object) -> StubState:
    options: dict[str, object] = {
        "transmit_enabled": True,
        "radio": EU868_NARROW,
        "stub_names": ("dev-room", "dev-bot"),
    }
    options.update(kwargs)
    return stub_state(**options)  # type: ignore[arg-type]


def _built(
    state: StubState | None = None,
) -> tuple[FastAPI, StubState, RecordingLogger, list[str], Recorder]:
    panel_state = state or _state()
    log = RecordingLogger()
    said: list[str] = []
    app = create_app(
        panel_state, auth=authenticator(), hosts=HOSTS, logger=log, announce=said.append
    )
    return app, panel_state, log, said, Recorder(panel_state)


def _client(app: FastAPI, *, send_token: bool = True) -> TestClient:
    return signed_client(
        app, send_token=send_token, base_url="http://127.0.0.1:8080", follow_redirects=False
    )


def _live(app: FastAPI) -> httpx2.AsyncClient:
    return signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    )


def _nonce(body: str) -> str:
    marker = 'name="nonce" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


def _confirm(client: TestClient, entity_id: str, kind: str):
    """Open the confirmation and submit it, as a browser would."""
    path = f"/admin/advert/{entity_id}/{kind}"
    page = client.get(path).text
    return client.post(path, data={TOKEN_FIELD: csrf(client), "nonce": _nonce(page)})


def _audited(log: RecordingLogger) -> list[dict[str, object]]:
    return log.named("web_guarded_action")


# --- 2.1 / 2.2 The actions and the terminal line ----------------------------


def test_both_adverts_say_what_they_cost_and_ask_no_password() -> None:
    zero_hop = ACTION_DESCRIPTIONS[ADVERT_ZERO_HOP]
    flood = ACTION_DESCRIPTIONS[ADVERT_FLOOD]

    assert "direct neighbours only" in zero_hop
    assert "flood schedule unchanged" in zero_hop
    assert "every repeater in the mesh" in flood
    assert "everyone's airtime" in flood
    assert "next scheduled flood" in flood and "full interval out" in flood
    assert ADVERT_ZERO_HOP not in REAUTHENTICATED_ACTIONS
    assert ADVERT_FLOOD not in REAUTHENTICATED_ACTIONS


def test_the_terminal_line_names_the_advert_the_identity_and_the_operator() -> None:
    flood = render_advert_request("flood", "dev-room", actor="op", next_flood_at=NOW)
    zero_hop = render_advert_request("zero-hop", "dev-room", actor="op", next_flood_at=None)

    assert flood == (
        "web: flood advert requested for 'dev-room' (by account 'op' from the web "
        f"interface); next scheduled flood {NOW.isoformat()}"
    )
    assert zero_hop.startswith("web: zero-hop advert requested for 'dev-room'")
    assert "(by account 'op' from the web interface)" in zero_hop


# --- 3.1 The confirmation views ---------------------------------------------


@pytest.mark.parametrize(
    ("kind", "statements"),
    [
        ("zero-hop", ("direct neighbours only", "flood schedule unchanged")),
        ("flood", ("every repeater in the mesh", "next scheduled flood")),
    ],
)
def test_each_confirmation_states_its_cost_and_carries_no_password(
    kind: str, statements: tuple[str, ...]
) -> None:
    app, state, _log, _said, sink = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        response = client.get(f"/admin/advert/{stub.entity_id}/{kind}")

    body = " ".join(response.text.split())
    assert response.status_code == 200
    for statement in statements:
        assert statement in body
    assert stub.name in body
    assert f"0x{stub.node_hash:02x}" in body
    assert stub.identity.public_key.hex() in body
    assert stub.next_flood_at is not None
    assert stub.next_flood_at.isoformat() in body, "the schedule as it stands is not shown"
    assert 'type="password"' not in body
    assert 'name="nonce"' in body and f'name="{TOKEN_FIELD}"' in body
    assert sink.submissions == [], "opening a confirmation submitted an advert"


def test_an_unknown_advert_kind_is_not_a_page() -> None:
    app, state, log, _said, sink = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        assert client.get(f"/admin/advert/{stub.entity_id}/burst").status_code == 404
        response = client.post(
            f"/admin/advert/{stub.entity_id}/burst", data={TOKEN_FIELD: csrf(client)}
        )

    assert response.status_code == 404
    assert sink.submissions == []
    assert _audited(log) == [], "no advert action exists to have been refused"


def test_the_confirmation_for_an_identity_not_held_says_so() -> None:
    app, _state, _log, _said, _sink = _built()

    with _client(app) as client:
        response = client.get("/admin/advert/somebody-else/flood")

    assert response.status_code == 404
    assert "this run does not hold that identity" in response.text
    assert 'name="nonce"' not in response.text


# --- 3.2 Submitting ---------------------------------------------------------


def test_a_zero_hop_advert_is_submitted_and_leaves_the_schedule() -> None:
    app, state, log, said, sink = _built()
    stub = state.adverts.stubs[0]
    scheduled = stub.next_flood_at

    with _client(app) as client:
        response = _confirm(client, stub.entity_id, "zero-hop")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/identities"
    (submission,) = sink.submissions
    assert submission.entity_name == stub.name
    packet = decode_packet(submission.packet)
    assert packet.route_type is RouteType.DIRECT
    assert stub.next_flood_at == scheduled
    assert stub.adverts_sent == 0

    (event,) = _audited(log)
    assert event["action"] == ADVERT_ZERO_HOP
    assert event["outcome"] == "success"
    assert event["target"] == stub.entity_id
    assert event["entity_name"] == stub.name
    assert event["node_hash"] == stub.node_hash
    assert event["actor"] == OPERATOR
    assert said == [
        render_advert_request("zero-hop", stub.name, actor=OPERATOR, next_flood_at=scheduled)
    ]


def test_a_flood_advert_is_submitted_and_moves_the_schedule() -> None:
    app, state, log, said, sink = _built()
    stub = state.adverts.stubs[0]
    stub.next_flood_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)

    with _client(app) as client:
        response = _confirm(client, stub.entity_id, "flood")

    assert response.status_code == 303
    (submission,) = sink.submissions
    assert decode_packet(submission.packet).route_type is RouteType.FLOOD
    assert stub.adverts_sent == 1
    assert stub.last_flood_at is not None
    assert stub.next_flood_at is not None
    assert stub.next_flood_at >= stub.last_flood_at + dt.timedelta(hours=18)

    (event,) = _audited(log)
    assert event["action"] == ADVERT_FLOOD
    assert event["outcome"] == "success"
    assert event["actor"] == OPERATOR
    assert event["next_flood_at"] == stub.next_flood_at.isoformat()
    assert len(said) == 1 and "flood advert requested" in said[0]
    assert OPERATOR in said[0]


@pytest.mark.parametrize("kind", ["zero-hop", "flood"])
def test_a_closed_gate_refuses_and_charges_nothing(kind: str) -> None:
    app, state, log, said, sink = _built(_state(transmit_enabled=False))
    stub = state.adverts.stubs[0]
    scheduled = stub.next_flood_at
    before = state.scheduler.status().as_json()

    with _client(app) as client:
        response = _confirm(client, stub.entity_id, kind)

    assert response.status_code == 409
    assert "transmission is disabled" in response.text
    assert sink.submissions == []
    assert stub.next_flood_at == scheduled
    assert state.scheduler.status().as_json() == before, "airtime was charged"
    (event,) = _audited(log)
    assert event["outcome"] == "refused"
    assert "transmission is disabled" in str(event["reason"])
    assert event["actor"] == OPERATOR
    assert said == []


@pytest.mark.parametrize("kind", ["zero-hop", "flood"])
def test_no_radio_readback_refuses(kind: str) -> None:
    app, state, log, _said, sink = _built(_state(radio=None))
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        response = _confirm(client, stub.entity_id, kind)

    assert response.status_code == 409
    assert "until the board has answered" in response.text
    assert sink.submissions == []
    (event,) = _audited(log)
    assert event["outcome"] == "refused"
    assert "until the board has answered" in str(event["reason"])


def test_a_flood_inside_the_gap_is_refused_with_the_seconds_remaining() -> None:
    app, state, log, _said, sink = _built()
    first, second = state.adverts.stubs

    with _client(app) as client:
        assert _confirm(client, first.entity_id, "flood").status_code == 303
        scheduled = second.next_flood_at
        response = _confirm(client, second.entity_id, "flood")
        again = _confirm(client, first.entity_id, "flood")

    assert response.status_code == 409
    assert again.status_code == 409, "the same identity flooded twice inside the gap"
    assert "another flood is accepted in" in response.text
    assert len(sink.submissions) == 1
    assert second.next_flood_at == scheduled
    refused = [event for event in _audited(log) if event["outcome"] == "refused"]
    assert len(refused) == 2
    remaining = refused[0]["gap_remaining_seconds"]
    assert isinstance(remaining, int) and 590 <= remaining <= 600
    assert f"accepted in {remaining} s" in response.text


def test_a_zero_hop_inside_the_gap_is_accepted() -> None:
    app, state, _log, _said, sink = _built()
    first, second = state.adverts.stubs

    with _client(app) as client:
        assert _confirm(client, first.entity_id, "flood").status_code == 303
        response = _confirm(client, second.entity_id, "zero-hop")

    assert response.status_code == 303
    assert len(sink.submissions) == 2


def test_two_zero_hops_in_a_row_are_both_submitted() -> None:
    app, state, _log, _said, sink = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        assert _confirm(client, stub.entity_id, "zero-hop").status_code == 303
        assert _confirm(client, stub.entity_id, "zero-hop").status_code == 303

    assert len(sink.submissions) == 2


@pytest.mark.parametrize("mismatch", ["other-identity", "other-kind", "reused", "missing"])
def test_a_confirmation_not_minted_for_this_advert_is_refused(mismatch: str) -> None:
    app, state, log, said, sink = _built()
    stub, other = state.adverts.stubs
    path = f"/admin/advert/{stub.entity_id}/flood"

    with _client(app) as client:
        if mismatch == "other-identity":
            nonce = _nonce(client.get(f"/admin/advert/{other.entity_id}/flood").text)
        elif mismatch == "other-kind":
            nonce = _nonce(client.get(f"/admin/advert/{stub.entity_id}/zero-hop").text)
        elif mismatch == "reused":
            nonce = _nonce(client.get(path).text)
            state.scheduler.enable_transmit(False)
            client.post(path, data={TOKEN_FIELD: csrf(client), "nonce": nonce})
            state.scheduler.enable_transmit(True)
            log.events.clear()
        else:
            nonce = ""
        response = client.post(path, data={TOKEN_FIELD: csrf(client), "nonce": nonce})

    assert response.status_code == 403
    assert sink.submissions == []
    (event,) = _audited(log)
    assert event["action"] == ADVERT_FLOOD
    assert event["outcome"] == "refused"
    assert event["target"] == stub.entity_id
    assert event["actor"] == OPERATOR
    assert said == []


def test_an_identity_this_run_does_not_hold_is_refused() -> None:
    app, _state, log, _said, sink = _built()

    with _client(app) as client:
        response = client.post(
            "/admin/advert/somebody-else/zero-hop", data={TOKEN_FIELD: csrf(client)}
        )

    assert response.status_code == 404
    assert "this run does not hold that identity" in response.text
    assert sink.submissions == []
    (event,) = _audited(log)
    assert event["outcome"] == "refused"
    assert event["target"] == "somebody-else"


# --- 3.3 Never by a link, never without provenance --------------------------


def test_no_get_performs_an_advert() -> None:
    app, state, log, said, sink = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        for kind in ("zero-hop", "flood"):
            for _ in range(2):
                assert client.get(f"/admin/advert/{stub.entity_id}/{kind}").status_code == 200

    assert sink.submissions == []
    assert _audited(log) == []
    assert said == []


def test_a_post_without_the_token_is_refused_before_the_scheduler_sees_it() -> None:
    app, state, log, _said, sink = _built()
    stub = state.adverts.stubs[0]
    path = f"/admin/advert/{stub.entity_id}/zero-hop"

    with _client(app, send_token=False) as client:
        nonce = _nonce(client.get(path).text)
        response = client.post(path, data={"nonce": nonce})

    assert response.status_code == 403
    assert sink.submissions == []
    assert _audited(log) == [], "the handler ran despite the request being refused"


# --- 3.4 The schedule on the identities page --------------------------------


def test_an_identity_that_has_not_flooded_shows_none_this_run() -> None:
    app, state, _log, _said, _sink = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        body = client.get("/admin/identities").text

    assert stub.next_flood_at is not None
    assert stub.next_flood_at.isoformat() in body
    assert "none this run" in body
    assert f'href="/admin/advert/{stub.entity_id}/zero-hop"' in body
    assert f'href="/admin/advert/{stub.entity_id}/flood"' in body
    assert 'class="table-wrap"' in body


def test_an_active_override_is_shown_with_its_expiry() -> None:
    app, state, _log, _said, _sink = _built()
    stub = state.adverts.stubs[0]
    override = state.adverts.set_override(stub, 7200.0, expires_in=1800.0)

    with _client(app) as client:
        body = client.get("/admin/identities").text

    assert "2 h until" in body
    assert f'datetime="{override.expires_at.isoformat()}"' in body


def test_the_schedule_after_a_requested_flood() -> None:
    app, state, _log, _said, _sink = _built()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        assert _confirm(client, stub.entity_id, "flood").status_code == 303
        body = client.get("/admin/identities").text

    assert stub.last_flood_at is not None and stub.next_flood_at is not None
    assert stub.last_flood_at.isoformat() in body
    assert stub.next_flood_at.isoformat() in body


# --- 3.5 From a room or a bot -----------------------------------------------


class _Served:
    """A room this run serves, as the rooms list reads it."""

    def __init__(self, room: object, entity: object) -> None:
        self.room = room
        self.entity = entity


class _Running:
    """A bot worker this run runs, as the bots list reads it."""

    def __init__(self, record: object, entity: object) -> None:
        self.record = record
        self.entity = entity
        self.name = record.entity_name  # type: ignore[attr-defined]


async def _stored(persistence: Persistence, state: StubState, name: str, node_type: NodeType):
    identity = generate_identity(avoid_node_hashes={stub.node_hash for stub in state.adverts.stubs})
    stored = await persistence.entities.store(
        name=name,
        identity=identity,
        secret=SECRET,
        node_type=node_type,
        entity_type=BOT_ENTITY_TYPE if name.endswith("bot") else None,
    )
    assert isinstance(stored, Succeeded), stored
    return stored.value, identity


@pytest.mark.database
async def test_a_served_room_links_to_its_identitys_adverts(database: Database) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence, stub_names=())
    served_entity, served_identity = await _stored(
        persistence, state, "served-host", NodeType.ROOM_SERVER
    )
    idle_entity, _ = await _stored(persistence, state, "idle-host", NodeType.ROOM_SERVER)
    served = await persistence.rooms.create(
        entity_id=served_entity.id, name="served", admin_password_hash="x"
    )
    idle = await persistence.rooms.create(
        entity_id=idle_entity.id, name="idle", admin_password_hash="x"
    )
    assert isinstance(served, Succeeded) and isinstance(idle, Succeeded)
    stub = state.adverts.add_identity("served-host", served_identity)
    state.rooms.append(_Served(served.value, stub))  # type: ignore[arg-type]
    app, _built_state, _log, _said, _sink = _built(state)

    async with _live(app) as client:
        body = (await client.get("/rooms")).text

    assert f'href="/admin/advert/{stub.entity_id}/zero-hop"' in body
    assert f'href="/admin/advert/{stub.entity_id}/flood"' in body
    assert body.count("/admin/advert/") == 2, "the unserved room offers advert links"


@pytest.mark.database
async def test_a_running_bot_links_to_its_identitys_adverts(database: Database) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence, stub_names=())
    running_entity, running_identity = await _stored(
        persistence, state, "running-bot", NodeType.CHAT
    )
    stopped_entity, _ = await _stored(persistence, state, "stopped-bot", NodeType.CHAT)
    config = bot_drivers.default_config("greeter")
    running = await persistence.bots.create(
        entity_id=running_entity.id, driver="greeter", config=config, entity_name="running-bot"
    )
    stopped = await persistence.bots.create(
        entity_id=stopped_entity.id, driver="greeter", config=config, entity_name="stopped-bot"
    )
    assert isinstance(running, Succeeded) and isinstance(stopped, Succeeded)
    stub = state.adverts.add_identity("running-bot", running_identity)
    state.bots.workers.append(_Running(running.value, stub))  # type: ignore[arg-type]
    app, _built_state, _log, _said, _sink = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{running_entity.id}")).text
        idle = (await client.get(f"/admin/identities/{stopped_entity.id}")).text

    assert f'href="/admin/advert/{stub.entity_id}/zero-hop"' in body
    assert f'href="/admin/advert/{stub.entity_id}/flood"' in body
    assert body.count("/admin/advert/") == 2
    assert "/admin/advert/" not in idle, "the stopped bot offers advert links"


# --- Renaming an identity, and the advert it offers --------------------------
#
# The rename and the advert are two outcomes. The rename is applied first so
# the advert carries the new name, and a refused advert never undoes it.


async def _stored_identity(persistence: Persistence, state: StubState, name: str):
    """A stored identity that this run also holds, so the advert is offered."""
    stub = next(s for s in state.adverts.stubs if s.name == name)
    stored = await persistence.entities.store(name=name, identity=stub.identity, secret=SECRET)
    assert isinstance(stored, Succeeded)
    return stored.value, stub


async def _apost(client: httpx2.AsyncClient, app: FastAPI, path: str, **fields: str):
    return await client.post(path, data={TOKEN_FIELD: csrf(client), **fields})


def _kind_nonce(body: str, field: str) -> str:
    marker = f'name="{field}" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


@pytest.mark.database
async def test_the_rename_form_states_the_mesh_consequence_and_offers_both_adverts(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    record, _stub = await _stored_identity(persistence, state, "dev-room")
    app, _, _log, _said, _rec = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{record.id}/rename")).text

    assert "travels in this identity" in body and "adverts" in body
    assert "keep showing the old name until" in body
    assert 'value="zero-hop"' in body and 'value="flood"' in body
    assert 'value="none" checked' in body, "an advert was chosen by default"
    assert "repeated by every repeater" in body
    assert 'type="password"' not in body, "a rename asked for a password"


@pytest.mark.database
async def test_renaming_with_no_advert_transmits_nothing(database: Database) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    record, stub = await _stored_identity(persistence, state, "dev-room")
    app, _, _log, _said, recorder = _built(state)
    before = stub.next_flood_at

    async with _live(app) as client:
        response = await _apost(
            client, app, f"/admin/identities/{record.id}/rename", name="dev-room-2", advert="none"
        )

    assert response.status_code == 200
    assert recorder.submissions == [], "a rename put a packet on the air"
    assert stub.name == "dev-room-2", "the run did not adopt the new name"
    assert stub.next_flood_at == before, "the schedule moved"
    listed = await persistence.entities.list_all()
    assert [row.name for row in listed.value] == ["dev-room-2"]


@pytest.mark.database
async def test_renaming_with_a_flood_advert_sends_the_new_name(database: Database) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    record, stub = await _stored_identity(persistence, state, "dev-room")
    app, _, log, said, recorder = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{record.id}/rename")).text
        response = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/rename",
            name="dev-room-2",
            advert="flood",
            zero_hop_nonce=_kind_nonce(body, "zero_hop_nonce"),
            flood_nonce=_kind_nonce(body, "flood_nonce"),
        )

    assert response.status_code == 200
    assert "One flood advert was submitted" in response.text
    assert len(recorder.submissions) == 1
    assert stub.name == "dev-room-2"
    [audited] = [e for e in log.named("web_guarded_action") if e["action"] == ADVERT_FLOOD]
    assert audited["outcome"] == "success"
    assert audited["entity_name"] == "dev-room-2", "the advert carried the old name"
    assert said, "a successful advert was not stated in the run's own output"


@pytest.mark.database
async def test_a_refused_advert_leaves_the_rename_applied(database: Database) -> None:
    """The two outcomes are independent, and both are reported."""
    persistence = Persistence(database=database)
    state = _state(persistence=persistence, transmit_enabled=False)
    record, stub = await _stored_identity(persistence, state, "dev-room")
    app, _, log, _said, recorder = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{record.id}/rename")).text
        response = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/rename",
            name="dev-room-2",
            advert="flood",
            zero_hop_nonce=_kind_nonce(body, "zero_hop_nonce"),
            flood_nonce=_kind_nonce(body, "flood_nonce"),
        )

    assert response.status_code == 200
    assert "the advert was not sent" in response.text
    assert (
        "Transmission is disabled" in response.text or "transmission is disabled" in response.text
    )
    assert recorder.submissions == []
    assert stub.name == "dev-room-2", "the refused advert rolled the rename back"
    listed = await persistence.entities.list_all()
    assert [row.name for row in listed.value] == ["dev-room-2"]
    refused = [
        e
        for e in log.named("web_guarded_action")
        if e["action"] == ADVERT_FLOOD and e["outcome"] == "refused"
    ]
    assert refused, "the refused advert was not recorded as its own event"


@pytest.mark.database
async def test_a_refused_rename_submits_no_advert(database: Database) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    record, stub = await _stored_identity(persistence, state, "dev-room")
    app, _, _log, _said, recorder = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{record.id}/rename")).text
        response = await _apost(
            client,
            app,
            f"/admin/identities/{record.id}/rename",
            name="   ",
            advert="flood",
            zero_hop_nonce=_kind_nonce(body, "zero_hop_nonce"),
            flood_nonce=_kind_nonce(body, "flood_nonce"),
        )

    assert response.status_code == 400
    assert recorder.submissions == []
    assert stub.name == "dev-room"
    listed = await persistence.entities.list_all()
    assert [row.name for row in listed.value] == ["dev-room"]


@pytest.mark.database
async def test_a_rename_colliding_with_another_loaded_identity_is_refused(
    database: Database,
) -> None:
    """A keyfile identity is loaded and not stored, so only the run can see this."""
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    record, _stub = await _stored_identity(persistence, state, "dev-room")
    app, _, _log, _said, recorder = _built(state)

    async with _live(app) as client:
        response = await _apost(
            client, app, f"/admin/identities/{record.id}/rename", name="dev-bot", advert="none"
        )

    assert response.status_code == 400
    assert "dev-bot" in response.text
    assert "may not share a name" in response.text
    assert recorder.submissions == []
    listed = await persistence.entities.list_all()
    assert [row.name for row in listed.value] == ["dev-room"]


@pytest.mark.database
async def test_an_identity_this_run_does_not_hold_is_offered_no_advert(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    stored = await persistence.entities.store(
        name="elsewhere", identity=generate_identity(), secret=SECRET
    )
    assert isinstance(stored, Succeeded)
    app, _, _log, _said, recorder = _built(state)

    async with _live(app) as client:
        body = (await client.get(f"/admin/identities/{stored.value.id}/rename")).text
        assert "No advert is offered" in body
        assert "does not hold that identity" in body
        response = await _apost(
            client, app, f"/admin/identities/{stored.value.id}/rename", name="elsewhere-2"
        )

    assert response.status_code == 200
    assert recorder.submissions == []
    listed = await persistence.entities.list_all()
    assert [row.name for row in listed.value] == ["elsewhere-2"]


@pytest.mark.database
async def test_a_created_identitys_advert_schedule_shows_on_the_identities_page(
    database: Database,
) -> None:
    """6.5: the identities page shows the schedule of what this run actually
    holds, not just that a row was created — `next_flood_at` is set the
    moment adoption stages it (design D15), and the page reads it live."""
    persistence = Persistence(database=database)
    state = _state(persistence=persistence)
    state.channel_secret = SECRET
    app = create_app(
        state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger(), sealing_secret=SECRET
    )

    async with _live(app) as client:
        created = await _apost(
            client, app, "/admin/identities/create", name="scheduled-now", node_type="CHAT"
        )
        assert created.status_code == 303
        body = (await client.get("/admin/identities")).text

    (stub,) = [s for s in state.adverts.stubs if s.name == "scheduled-now"]
    assert stub.next_flood_at is not None, "adoption must stage a flood, not leave it unscheduled"
    loaded_section = body[body.index("loaded by this run") : body.index("<h2>stored")]
    assert "scheduled-now" in loaded_section
