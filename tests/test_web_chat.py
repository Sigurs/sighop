"""The companion chat client (`web-chat`, §8 area 4).

The surface that changes what sighop *is*: a person driving a transmission from
a browser rather than a command line. What the tests are about is showing the
protocol's delivery semantics as they actually are, and refusing what cannot be
sent before anything is queued.

Two of the assertions are negative and are the important ones:

* **reading transmits nothing** — no receipt, no presence, no read marker;
* **a refusal is not history** — a message that was not sent does not appear in
  the conversation, and the author's text is handed back unchanged.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sighop.db.engine import Database
from sighop.net.bus import Submission
from sighop.net.contacts import Contact
from sighop.net.dm import (
    INBOUND,
    MAX_TEXT_LEN,
    OUTBOUND,
    DirectMessageRecord,
    RecordedOutcome,
)
from sighop.net.paths import LearnedPath, PathKey
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import WireText
from sighop.radio.modem import EU868_NARROW
from sighop.web.app import allowed_hosts, create_app
from sighop.web.chat import ConversationLog
from sighop.web.guard import TOKEN_FIELD
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    StubState,
    authenticator,
    csrf,
    signed_async_client,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def _contact(name: str = "[redacted]", *, verified: bool = True) -> Contact:
    return Contact(
        public_key=generate_identity().public_key,
        name=WireText.from_bytes(name.encode()),
        advert_verified=verified,
        first_heard=NOW,
        last_heard=NOW,
    )


def _built(
    *,
    contacts: list[Contact] | None = None,
    stub_names: tuple[str, ...] = ("companion",),
    transmit_enabled: bool = True,
    routed: bool = True,
    log: ConversationLog | None = None,
) -> tuple[FastAPI, StubState, ConversationLog]:
    # A radio readback, because a send with none resolves as dropped before it
    # reaches the scheduler — `time_on_air_ms` refuses to guess (§4.1).
    state = stub_state(stub_names=stub_names, transmit_enabled=transmit_enabled, radio=EU868_NARROW)
    for contact in contacts or []:
        state.contacts.restore([contact])
        if routed:
            state.pipeline.paths._insert(
                PathKey.for_public_key(contact.public_key),
                LearnedPath(
                    path=b"",
                    hash_size=1,
                    hop_count=0,
                    snr_db=8.0,
                    confirmed_at=NOW,
                    packet_id="seed",
                ),
            )
    conversations = log or ConversationLog()
    app = create_app(
        state,
        auth=authenticator(),
        hosts=HOSTS,
        logger=RecordingLogger(),
        conversations=conversations,
    )
    return app, state, conversations


def _client(app: FastAPI) -> TestClient:
    return signed_client(app, base_url="http://127.0.0.1:8080", follow_redirects=False)


def _record(
    entity: bytes,
    peer: bytes,
    *,
    ref: str = "m1",
    direction: str = OUTBOUND,
    text: bytes = b"hello",
    outcome: RecordedOutcome = RecordedOutcome.IN_FLIGHT,
    attempts: int = 0,
    ack_latency_ms: float | None = None,
    at: dt.datetime = NOW,
) -> DirectMessageRecord:
    return DirectMessageRecord(
        entity_public_key=entity,
        peer_public_key=peer,
        direction=direction,
        text=text,
        wire_timestamp=1_757_000_000,
        handled_at=at,
        ref=ref,
        outcome=outcome,
        attempts=attempts,
        ack_latency_ms=ack_latency_ms,
    )


# --- 15.1 A conversation is one identity and one contact --------------------


def test_two_identities_and_one_contact_are_two_conversations() -> None:
    """15.1: never merged — that would attribute one identity's words to another."""
    contact = _contact()
    app, state, log = _built(contacts=[contact], stub_names=("first", "second"))
    one, two = state.adverts.stubs

    log.offer(_record(one.identity.public_key, contact.public_key, ref="a", text=b"from one"))
    log.offer(_record(two.identity.public_key, contact.public_key, ref="b", text=b"from two"))

    with _client(app) as client:
        listing = client.get("/chat").text
        first = client.get(f"/chat/{one.identity.public_key.hex()}/{contact.public_key.hex()}").text

    assert listing.count("open</a>") >= 2
    assert "from one" in first
    assert "from two" not in first, "one identity's conversation showed another's message"


def test_with_no_identity_nothing_can_be_composed() -> None:
    """15.1: an identity must be chosen before a message exists to send."""
    app, _state, _log = _built(contacts=[_contact()], stub_names=())

    with _client(app) as client:
        body = client.get("/chat").text

    assert "No identity to send as" in body
    assert "identity must be chosen" in body or "no identities" in body.lower()


def test_chat_points_to_contacts_rather_than_repeating_a_grid() -> None:
    """consolidate-web-pages 6.2: conversations are started from the contact list."""
    contact = _contact()
    app, state, _log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        body = client.get("/chat").text

    assert "start a conversation" not in body
    assert f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}" not in body
    assert '<a href="/contacts">contacts</a> page' in body


def test_a_conversation_for_an_unknown_pair_is_not_found() -> None:
    """15.1: both halves must exist; inventing either would invent a peer."""
    app, state, _log = _built(contacts=[_contact()])
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        response = client.get(f"/chat/{stub.identity.public_key.hex()}/{'00' * 32}")

    assert response.status_code == 404


# --- 15.2 Sending, and its state as it progresses ---------------------------


def test_an_acknowledged_send_shows_its_attempts_and_latency() -> None:
    """15.2: delivered, with the number of attempts and the measured latency."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            outcome=RecordedOutcome.ACKNOWLEDGED,
            attempts=2,
            ack_latency_ms=812.0,
        )
    )

    with _client(app) as client:
        body = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text

    state_cell = body.split('class="message-state"')[1].split("</td>")[0]
    assert "status-delivered" in state_cell and "✓✓" in state_cell
    assert '<span class="status-figures" aria-hidden="true">2 · 812 ms</span>' in state_cell
    assert 'title="delivered — acknowledged after 2 attempts, 812 ms"' in state_cell


def test_a_send_that_is_never_acknowledged_is_not_shown_as_delivered() -> None:
    """15.2: unacknowledged is *unknown*, and the interface says which."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            outcome=RecordedOutcome.UNACKNOWLEDGED,
            attempts=4,
        )
    )

    with _client(app) as client:
        body = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text

    state_cell = body.split('class="message-state"')[1].split("</td>")[0]
    assert "status-unacknowledged" in state_cell and "✓✓" not in state_cell
    assert '<span class="status-figures" aria-hidden="true">4</span>' in state_cell
    assert "unacknowledged after 4 attempts" in state_cell
    assert "cannot tell whether it arrived" in state_cell
    assert "delivered" not in body.split("conversation")[-1]


def test_a_submitted_message_is_in_the_conversation_before_it_resolves() -> None:
    """15.2: awaiting transmission is a state, not an absence."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(_record(stub.identity.public_key, contact.public_key))

    with _client(app) as client:
        body = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text

    assert "awaiting transmission" in body


async def test_a_send_goes_through_the_messengers_own_path() -> None:
    """15.2: the same composition, routing, retry and acknowledgement path.

    Asserted at the scheduler: a message from the browser arrives with the
    priority class and the deadline every direct message gets, because it went
    through `DirectMessenger.send` rather than around it.
    """
    from sighop.net.bus import PriorityClass

    contact = _contact()
    app, state, _log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    state.scheduler.attach_sender(None)
    submissions: list[Submission] = []
    original = state.messenger.submit

    def record(submission: Submission) -> object:
        submissions.append(submission)
        return original(submission)

    state.messenger.submit = record  # type: ignore[assignment]

    async with signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={TOKEN_FIELD: csrf(client), "text": "hej"},
        )
        assert response.status_code == 303
        # The send runs as its own task; give it a turn to reach the scheduler.
        for _ in range(20):
            await asyncio.sleep(0)
            if submissions:
                break

    assert submissions, "the send never reached the scheduler"
    assert submissions[0].priority is PriorityClass.MESSAGE
    assert submissions[0].origin == "direct_message"


# --- 15.3 / 15.4 Refusals, and flooding as a choice -------------------------


def test_text_over_the_limit_is_refused_with_the_overage_and_kept() -> None:
    """15.3, design D11: refuse rather than corrupt — nothing is truncated."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    long = "x" * (MAX_TEXT_LEN + 7)

    with _client(app) as client:
        response = client.post(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={TOKEN_FIELD: csrf(client), "text": long},
        )

    assert response.status_code == 400
    assert str(MAX_TEXT_LEN) in response.text
    assert "7 over" in response.text
    assert long in response.text, "the author's text was not preserved"
    assert log.conversation(stub.identity.public_key, contact.public_key) == [], (
        "a refused message entered the conversation's history"
    )
    assert state.scheduler.status().stats.submitted == 0, "a refused message was queued"


def test_a_routeless_contact_is_refused_and_flooding_is_offered() -> None:
    """15.3 / 15.4: offered as an explicit choice, never performed on request."""
    contact = _contact()
    app, state, log = _built(contacts=[contact], routed=False)
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        response = client.post(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={TOKEN_FIELD: csrf(client), "text": "hej"},
        )

    assert response.status_code == 400
    assert "no route is known" in response.text
    assert "explicit choice" in response.text
    assert "not performed on your behalf" in response.text
    assert state.scheduler.status().stats.submitted == 0
    assert log.conversation(stub.identity.public_key, contact.public_key) == []


async def test_a_routeless_send_floods_only_when_the_choice_was_made() -> None:
    """15.4: the same send, with the box ticked, is accepted and floods."""
    from sighop.protocol.packet import RouteType, decode

    contact = _contact()
    app, state, _log = _built(contacts=[contact], routed=False)
    stub = state.adverts.stubs[0]
    submissions: list[Submission] = []
    original = state.messenger.submit

    def record(submission: Submission) -> object:
        submissions.append(submission)
        return original(submission)

    state.messenger.submit = record  # type: ignore[assignment]

    async with signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={TOKEN_FIELD: csrf(client), "text": "hej", "flood": "yes"},
        )
        assert response.status_code == 303
        for _ in range(20):
            await asyncio.sleep(0)
            if submissions:
                break

    assert submissions, "the flooded send never reached the scheduler"
    assert decode(submissions[0].packet).route_type is RouteType.FLOOD


def test_a_closed_gate_refuses_and_queues_nothing() -> None:
    """15.3: so no message is delivered later by a change of gate."""
    contact = _contact()
    app, state, log = _built(contacts=[contact], transmit_enabled=False)
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        response = client.post(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={TOKEN_FIELD: csrf(client), "text": "hej"},
        )

    assert response.status_code == 400
    assert "Transmission is disabled" in response.text
    assert "change of gate" in response.text
    assert state.scheduler.status().stats.submitted == 0
    assert log.conversation(stub.identity.public_key, contact.public_key) == []


# --- 15.5 Received messages appear without a reload -------------------------


def test_a_received_message_appears_in_its_conversation() -> None:
    """15.5: the partial the page refreshes in place, driven directly."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    path = f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"

    with _client(app) as client:
        before = client.get(f"{path}/messages").text
        assert "No messages yet" in before

        log.offer(
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="pkt-1",
                direction=INBOUND,
                text=b"hej fran andra sidan",
                outcome=RecordedOutcome.RECEIVED,
            )
        )
        after = client.get(f"{path}/messages").text

    assert "hej fran andra sidan" in after
    assert "hx-trigger" in after, "the list does not refresh itself"


def test_a_conversation_that_is_not_open_is_marked_as_having_something_new() -> None:
    """15.5: and the message is there when it is opened."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            ref="pkt-1",
            direction=INBOUND,
            outcome=RecordedOutcome.RECEIVED,
            text=b"unread",
        )
    )

    with _client(app) as client:
        listing = client.get("/chat").text
        assert "1 new" in listing

        opened = client.get(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"
        ).text
        assert "unread" in opened

        after = client.get("/chat").text

    assert "1 new" not in after, "opening a conversation did not clear its marker"


# --- 15.6 Who a received message is from ------------------------------------


def test_a_received_message_from_an_unverified_contact_is_marked() -> None:
    """15.6: key possession, not identity — and the marking says so."""
    contact = _contact("pasted-in", verified=False)
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            ref="pkt-1",
            direction=INBOUND,
            outcome=RecordedOutcome.RECEIVED,
        )
    )

    with _client(app) as client:
        body = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text

    assert "identity-unverified" in body
    assert "authenticates possession" in body
    assert "never the identity of whoever holds it" in body


def test_a_message_from_a_verified_contact_uses_the_same_marking_as_elsewhere() -> None:
    """15.6: one macro, so the chat client cannot disagree with the contact table."""
    contact = _contact("[redacted]", verified=True)
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            ref="pkt-1",
            direction=INBOUND,
            outcome=RecordedOutcome.RECEIVED,
        )
    )

    with _client(app) as client:
        chat = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text
        contacts = client.get("/contacts").text

    assert "identity-verified" in chat
    assert "identity-verified" in contacts


# --- 15.7 Group text is not a direct message -------------------------------


def test_a_group_text_reception_produces_no_chat_message() -> None:
    """15.7: it belongs in the packet feed as the undecrypted payload it is."""
    from sighop.net.rx import decode_event
    from sighop.radio.modem import RxEvent, RxMeta
    from sighop.web.serialize import rx_record

    grp_txt = bytes.fromhex(
        "1e0054[redacted]8ecd77dfc078d4386ea8326f26e124e257869087027731a53dc31db"
        "9c0ecc0ced37f0a645a90e11bf324210277fe"
    )
    record = decode_event(
        RxEvent(packet=grp_txt, rx_meta=RxMeta(snr_db=12.0, rssi_dbm=-30), received_at=NOW)
    )
    log = ConversationLog()
    contact = _contact()
    app, state, _log = _built(contacts=[contact], log=log)
    stub = state.adverts.stubs[0]

    # It serialises for the feed, which is where it belongs...
    row = rx_record(record)
    assert row["direction"] == "rx"

    # ...and it produced no conversation entry.
    assert log.conversation_keys() == []
    with _client(app) as client:
        body = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text
    assert "No messages yet" in body


# --- 15.8 / milestone 9 10.2 With a degraded database -----------------------


class _Direct:
    """A `DirectMessageRepository` whose reads succeed until told otherwise."""

    def __init__(self, stored: list[DirectMessageRecord]) -> None:
        self.stored = stored
        self.failing = False

    def _failed(self, operation: str):
        from sighop.db.engine import DatabaseError, Failed

        return Failed(operation=operation, error=DatabaseError("the database is unreachable"))

    async def conversations(self, entity_public_key: bytes | None = None):
        from sighop.db.engine import Succeeded

        return self._failed("list_conversations") if self.failing else Succeeded(value=[])

    async def conversation(self, entity_public_key: bytes, peer_public_key: bytes, **_: object):
        from sighop.db.engine import Succeeded

        return self._failed("read_conversation") if self.failing else Succeeded(value=self.stored)


class _Channels:
    """A `ChannelRepository` that follows its `_Direct`: `/chat` lists the stored
    channels too, and in an outage those reads fail alongside the others."""

    def __init__(self, direct: _Direct) -> None:
        self.direct = direct

    async def list_all(self):
        from sighop.db.engine import Succeeded

        return self.direct._failed("list_channels") if self.direct.failing else Succeeded(value=[])

    async def message_counts(self):
        from sighop.db.engine import Succeeded

        return self.direct._failed("count_messages") if self.direct.failing else Succeeded(value={})


class _Persistence:
    state = "ok"
    degraded = False

    def __init__(self, direct: _Direct) -> None:
        self.direct_messages = direct
        self.channels = _Channels(direct)

    def as_json(self) -> dict[str, object]:
        return {}


def _degradable(contact: Contact, stored_text: bytes = b"from before"):
    state = stub_state(stub_names=("companion",), transmit_enabled=True)
    state.contacts.restore([contact])
    stub = state.adverts.stubs[0]
    stored = [
        _record(
            stub.identity.public_key,
            contact.public_key,
            ref="pkt-stored",
            direction=INBOUND,
            outcome=RecordedOutcome.RECEIVED,
            text=stored_text,
        )
    ]
    direct = _Direct(stored)
    persistence = _Persistence(direct)
    state.persistence = persistence  # type: ignore[assignment]
    log = ConversationLog()
    app = create_app(
        state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger(), conversations=log
    )
    return app, state, log, direct, persistence


def test_the_database_degrading_mid_conversation_is_said_on_the_next_refresh() -> None:
    """`web-chat`: sending and receiving continue, and the refreshed partial says
    messages from this point are not being recorded — no reload needed."""
    contact = _contact()
    app, state, log, _direct, persistence = _degradable(contact)
    stub = state.adverts.stubs[0]
    path = f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"

    with _client(app) as client:
        healthy = client.get(path).text
        assert "not being recorded" not in healthy
        assert "cannot be read" not in healthy

        persistence.degraded = True
        persistence.state = "degraded"
        log.offer(
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="pkt-during",
                direction=INBOUND,
                outcome=RecordedOutcome.RECEIVED,
                text=b"still arriving",
            )
        )
        partial = client.get(f"{path}/messages").text

    assert "messages from this point are not being recorded" in partial
    assert "still arriving" in partial, "receiving continued"
    assert "from before" in partial


def test_a_conversation_opened_while_degraded_says_stored_history_cannot_be_read() -> None:
    """`web-chat`: the messages this run has seen, sending available, and both
    facts said — history cannot be read, and new messages are not recorded."""
    contact = _contact()
    app, state, log, direct, persistence = _degradable(contact)
    direct.failing = True
    persistence.degraded = True
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            ref="pkt-seen",
            direction=INBOUND,
            outcome=RecordedOutcome.RECEIVED,
            text=b"seen this run",
        )
    )

    with _client(app) as client:
        body = client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}").text

    assert "Stored history cannot be read" in body
    assert "new messages are not being recorded" in body
    assert "seen this run" in body
    assert "from before" not in body
    assert 'class="composer"' in body, "sending is available"


def test_a_degraded_database_says_so_on_the_conversation_list() -> None:
    contact = _contact()
    app, _state, _log, direct, persistence = _degradable(contact)
    direct.failing = True
    persistence.degraded = True

    with _client(app) as client:
        body = client.get("/chat").text

    assert "not being recorded" in body


# --- 15.9 Reading transmits nothing -----------------------------------------


def test_opening_scrolling_and_refreshing_transmit_nothing() -> None:
    """15.9: no receipt, no presence, no read marker — the rule, asserted."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    log.offer(
        _record(
            stub.identity.public_key,
            contact.public_key,
            ref="pkt-1",
            direction=INBOUND,
            outcome=RecordedOutcome.RECEIVED,
        )
    )
    before = state.scheduler.status().as_json()
    path = f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"

    with _client(app) as client:
        for _ in range(3):
            client.get("/chat")
            client.get(path)
            client.get(f"{path}/messages")

    assert state.scheduler.status().as_json() == before, "reading a conversation transmitted"


# --- The in-memory log itself -----------------------------------------------


def test_the_log_updates_a_message_in_place_rather_than_twice() -> None:
    """One message, offered at submission and again when it resolves."""
    log = ConversationLog()
    entity, peer = b"\x01" * 32, b"\x02" * 32

    log.offer(_record(entity, peer, ref="m1"))
    log.offer(_record(entity, peer, ref="m1", outcome=RecordedOutcome.ACKNOWLEDGED, attempts=1))

    held = log.conversation(entity, peer)
    assert len(held) == 1
    assert held[0].outcome is RecordedOutcome.ACKNOWLEDGED


def test_the_log_is_bounded() -> None:
    """A tab left open for a week is not an unbounded list of everything said."""
    log = ConversationLog(capacity=3)
    entity, peer = b"\x01" * 32, b"\x02" * 32

    for index in range(10):
        log.offer(_record(entity, peer, ref=f"m{index}"))

    held = log.conversation(entity, peer)
    assert len(held) == 3
    assert [record.ref for record in held] == ["m9", "m8", "m7"]


@pytest.mark.database
async def test_a_conversation_survives_a_restart(database: Database) -> None:
    """`dm-history`, and the milestone's exit criterion in miniature.

    The in-memory log is this run's; the table is what makes the conversation
    outlive it. A fresh panel with an empty log reads the same messages back.
    """
    from sighop.db.persistence import Persistence

    persistence = Persistence(database=database)
    contact = _contact()
    state = stub_state(stub_names=("companion",), persistence=persistence)
    state.contacts.restore([contact])
    stub = state.adverts.stubs[0]

    from sighop.db.engine import Succeeded

    written = await persistence.direct_messages.upsert_many(
        [
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="m1",
                text=b"before the restart",
                outcome=RecordedOutcome.ACKNOWLEDGED,
                attempts=1,
            )
        ]
    )
    assert isinstance(written, Succeeded)

    # A fresh panel: no in-memory log at all, as after a restart.
    app = create_app(
        state,
        auth=authenticator(),
        hosts=HOSTS,
        logger=RecordingLogger(),
        conversations=ConversationLog(),
    )
    async with signed_async_client(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        body = (
            await client.get(f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}")
        ).text

    assert "before the restart" in body
    assert "delivered" in body


# --- Channels (change `channel-messaging`, tasks 8.1, 8.2, 8.4) --------------


def _channels(state: StubState, *names: str) -> None:
    from sighop.net.channels import ChannelKind, ChannelSet, LoadedChannel
    from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, channel_key_from_hashtag

    loaded = [LoadedChannel(1, "Public", ChannelKind.PUBLIC, PUBLIC_CHANNEL_KEY)]
    for index, name in enumerate(names, start=2):
        loaded.append(
            LoadedChannel(index, name, ChannelKind.HASHTAG, channel_key_from_hashtag(name))
        )
    state.channels.replace_channels(ChannelSet(channels=tuple(loaded)))


def _channel_app(
    *, stub_names: tuple[str, ...] = ("dev-companion",), transmit_enabled: bool = True
):
    from sighop.web.chat import ChannelLog

    state = stub_state(stub_names=stub_names, transmit_enabled=transmit_enabled, radio=EU868_NARROW)
    _channels(state, "#dev-sighop")
    channel_log = ChannelLog()
    state.channels.add_record_sink(channel_log)
    app = create_app(
        state,
        auth=authenticator(),
        hosts=HOSTS,
        logger=RecordingLogger(),
        channel_log=channel_log,
    )
    return app, state, channel_log


def _received(channel_id: int, ref: str, name: str | None, text: bytes = b"hej"):
    from sighop.net.channels import ChannelMessageRecord, ChannelOutcome

    return ChannelMessageRecord(
        channel_id=channel_id,
        direction="in",
        ref=ref,
        text=text,
        wire_timestamp=int(NOW.timestamp()),
        handled_at=NOW,
        outcome=ChannelOutcome.RECEIVED,
        unverified_sender_name=name,
        hop_count=2,
    )


def test_the_channel_log_updates_in_place_and_is_bounded() -> None:
    from dataclasses import replace

    from sighop.net.channels import ChannelMessageRecord, ChannelOutcome
    from sighop.web.chat import ChannelLog

    log = ChannelLog(capacity=3)
    post = ChannelMessageRecord(
        channel_id=2,
        direction="out",
        ref="post",
        text=b"hi",
        wire_timestamp=1,
        handled_at=NOW,
        outcome=ChannelOutcome.AWAITING,
        entity_public_key=b"\x01" * 32,
    )
    log.offer(post)
    log.offer(replace(post, outcome=ChannelOutcome.TRANSMITTED, repeats_heard=1))
    assert [r.outcome for r in log.messages(2)] == [ChannelOutcome.TRANSMITTED]
    assert log.new_for(2) == 0, "our own post is not something new"

    for index in range(10):
        log.offer(_received(2, f"p{index}", "alice"))
    assert [r.ref for r in log.messages(2)] == ["p9", "p8", "p7"]
    assert log.new_for(2) == 10
    log.opened(2)
    assert log.new_for(2) == 0


def test_channels_are_listed_guessable_and_readable_without_an_identity() -> None:
    app, _state, log = _channel_app(stub_names=())
    log.offer(_received(2, "p1", "alice"))

    with _client(app) as client:
        index = client.get("/chat").text
        channel = client.get("/chat/channel/1")

    assert "Public" in index and "#dev-sighop" in index
    assert index.count('class="status status-guessable"') == 2
    assert 'href="/chat/channel/2"' in index and "1 new" in index
    assert "Channel messaging is not supported" not in index
    assert channel.status_code == 200
    assert "No identity to post as" in channel.text


def test_a_message_arriving_in_an_open_channel_appears_on_refresh() -> None:
    app, _state, log = _channel_app()
    with _client(app) as client:
        before = client.get("/chat/channel/2/messages").text
        log.offer(_received(2, "p1", "alice", b"arrived just now"))
        after = client.get("/chat/channel/2/messages").text
    assert "arrived just now" not in before
    assert "arrived just now" in after
    assert '<span class="status-figures" aria-hidden="true">2</span>' in after
    assert 'title="received over 2 hops"' in after
    assert 'hx-trigger="every 3s"' in after


def test_a_claim_matching_a_verified_contact_is_not_drawn_as_that_contact() -> None:
    app, state, log = _channel_app()
    contact = _contact("[redacted]", verified=True)
    state.contacts.restore([contact])
    log.offer(_received(2, "p1", "[redacted]"))

    with _client(app) as client:
        body = client.get("/chat/channel/2").text

    start = body.index('<div id="channel"')
    listing = body[start:]
    assert "claimed-name" in listing and "claimed, unverified" in listing
    assert "identity-verified" not in listing
    assert "identity " not in listing, "the identity component drew a claimed name"
    assert contact.public_key.hex() not in body, "the claim was linked to the contact"
    assert "Channel sender names are not authenticated" in body


def test_a_post_needs_an_identity_and_is_shown_under_it() -> None:
    app, state, _log = _channel_app()
    stub = state.adverts.stubs[0]
    submitted: list[Submission] = []

    def recording(submission: Submission):
        submitted.append(submission)
        return state.scheduler.submit(submission)

    state.channels.submit = recording

    with _client(app) as client:
        missing = client.post("/chat/channel/2", data={TOKEN_FIELD: csrf(client), "text": "hi"})
        posted = client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": "hello channel",
                "identity": stub.identity.public_key.hex(),
            },
        )
        page = client.get(posted.headers["location"]).text

    assert missing.status_code == 400 and "An identity must be chosen" in missing.text
    assert posted.status_code == 303
    assert len(submitted) == 1
    assert "hello channel" in page and "dev-companion" in page and "(this station)" in page
    assert "awaiting transmission" in page


def test_a_transmitted_post_states_repeats_and_that_no_acknowledgement_exists() -> None:
    from sighop.net.channels import ChannelMessageRecord, ChannelOutcome

    app, state, log = _channel_app()
    stub = state.adverts.stubs[0]
    log.offer(
        ChannelMessageRecord(
            channel_id=2,
            direction="out",
            ref="post",
            text=b"hi",
            wire_timestamp=1,
            handled_at=NOW,
            outcome=ChannelOutcome.TRANSMITTED,
            entity_public_key=stub.identity.public_key,
            repeats_heard=3,
        )
    )
    log.offer(
        ChannelMessageRecord(
            channel_id=2,
            direction="out",
            ref="quiet",
            text=b"hi",
            wire_timestamp=1,
            handled_at=NOW,
            outcome=ChannelOutcome.TRANSMITTED,
            entity_public_key=stub.identity.public_key,
        )
    )
    with _client(app) as client:
        body = client.get("/chat/channel/2/messages").text
        page = client.get("/chat/channel/2?identity=" + stub.identity.public_key.hex()).text
    rows = body.split('<tr class="message')[1:]
    repeated = next(row for row in rows if "status-repeats" in row)
    quiet = next(row for row in rows if "status-repeats" not in row)
    # The transmitted glyph's hover says no acknowledgement exists; the row
    # itself no longer spells it out in visible text.
    assert 'title="transmitted; no acknowledgement exists for channel messages"' in repeated
    assert '<span class="status-figures" aria-hidden="true">3</span>' in repeated
    assert 'title="repeat heard 3 times — a repeater forwarded it"' in repeated
    assert "status-transmitted" in quiet
    assert "which does not mean it was not received" in quiet
    # Stated once on the page, in the channel's standing note.
    assert page.count("No acknowledgement exists for channel messages") == 1


def test_a_post_over_the_limit_is_refused_with_the_text_kept_and_nothing_recorded() -> None:
    app, state, log = _channel_app()
    stub = state.adverts.stubs[0]
    text = "x" * 150

    with _client(app) as client:
        refused = client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": text,
                "identity": stub.identity.public_key.hex(),
            },
        )

    assert refused.status_code == 400
    assert "at most 160 bytes" in refused.text and "5 over" in refused.text
    assert "name" in refused.text
    assert f">{text}</textarea>" in refused.text
    assert log.messages(2) == [] and state.scheduler.status().stats.submitted == 0


def test_a_closed_gate_refuses_a_post_and_queues_nothing() -> None:
    app, state, log = _channel_app(transmit_enabled=False)
    stub = state.adverts.stubs[0]
    with _client(app) as client:
        refused = client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": "hi",
                "identity": stub.identity.public_key.hex(),
            },
        )
    assert refused.status_code == 400 and "Transmission is disabled" in refused.text
    assert log.messages(2) == [] and state.scheduler.status().stats.submitted == 0


def test_the_public_composer_states_its_audience() -> None:
    app, _state, _log = _channel_app()
    with _client(app) as client:
        public = client.get("/chat/channel/1").text
        hashtag = client.get("/chat/channel/2").text
    assert "flooded to the whole mesh and readable by anyone" in public
    assert "flooded to the whole mesh and readable by anyone" not in hashtag


def test_opening_a_channel_transmits_nothing() -> None:
    app, state, log = _channel_app()
    log.offer(_received(2, "p1", "alice"))
    before = state.scheduler.status().as_json()
    with _client(app) as client:
        for _ in range(3):
            client.get("/chat/channel/2")
            client.get("/chat/channel/2/messages")
    assert state.scheduler.status().as_json() == before


def test_a_degraded_database_is_said_in_an_open_channel_while_posting_continues() -> None:
    from sighop.db.engine import DatabaseError, Failed, Succeeded

    class _ChannelMessages:
        failing = False

        async def recent(self, channel_id: int, **_: object):
            if self.failing:
                return Failed(operation="read_channel_history", error=DatabaseError("down"))
            return Succeeded(value=[])

    class _Persist:
        state = "ok"
        degraded = False
        channel_messages = _ChannelMessages()

        def as_json(self) -> dict[str, object]:
            return {}

    app, state, log = _channel_app()
    persistence = _Persist()
    state.persistence = persistence
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        healthy = client.get("/chat/channel/2/messages").text
        persistence.degraded = True
        persistence.channel_messages.failing = True
        log.offer(_received(2, "p1", "alice", b"still arriving"))
        partial = client.get("/chat/channel/2/messages").text
        posted = client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": "still posting",
                "identity": stub.identity.public_key.hex(),
            },
        )

    assert "not being recorded" not in healthy
    assert "new messages are not being recorded" in partial
    assert "still arriving" in partial
    assert posted.status_code == 303


def test_a_post_from_the_interface_is_run_output_naming_the_account() -> None:
    from sighop.monitor.render import render_channel_event
    from tests.webfixtures import OPERATOR

    app, state, _log = _channel_app()
    stub = state.adverts.stubs[0]
    output: list[str] = []
    state.channels._on_event = lambda event: output.append(render_channel_event(event))

    with _client(app) as client:
        client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": "hello",
                "identity": stub.identity.public_key.hex(),
            },
        )

    [line] = [line for line in output if "post as" in line]
    assert "#dev-sighop" in line and "dev-companion" in line
    assert f"by account '{OPERATOR}'" in line
