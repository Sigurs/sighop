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
import base64
import datetime as dt
import re
import uuid
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import sighop.web
from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.net.bus import Submission
from sighop.net.channels import MAX_CHANNEL_TEXT_LEN
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
from sighop.protocol.packet import PayloadType
from sighop.protocol.payloads import WireText
from sighop.radio.modem import EU868_NARROW
from sighop.web.app import allowed_hosts, create_app
from sighop.web.chat import ConversationLog
from sighop.web.guard import TOKEN_FIELD
from tests.protocol.corpus import first_received_frame
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    MemoryAccounts,
    StubState,
    authenticator,
    csrf,
    session_of,
    signed_async_client,
    signed_client,
    signed_in,
    stub_state,
)

pytestmark = pytest.mark.usefixtures("default_persistence")

STATIC_DIR = Path(sighop.web.__file__).parent / "static"
HOSTS = allowed_hosts("127.0.0.1", 8080)
NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def _contact(name: str = "syn-alder", *, verified: bool = True) -> Contact:
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


def test_the_composer_counts_the_bytes_the_refusal_counts() -> None:
    """The count is predictive, not authoritative: it carries the same limit the
    refusal applies, and for a channel post the identity's name and separator
    that ride inside that limit (`web-chat`)."""
    contact = _contact()
    app, state, _ = _built(contacts=[contact])
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        conversation = client.get(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"
        ).text

    assert f'data-limit="{MAX_TEXT_LEN}"' in conversation
    assert '<span class="count"' in conversation
    # A direct message carries nothing besides its text.
    assert "data-prefix-from" not in conversation

    script = (STATIC_DIR / "display.js").read_text()
    # Bytes, because the limit is in bytes: a two-byte character must not count
    # as one.
    assert "TextEncoder" in script and "encoder.encode(text).length" in script
    # And never a disabled send control: the refusal at submission decides.
    assert not re.search(r"\.disabled\s*=", script)
    assert 'setAttribute("disabled"' not in script


def test_a_channel_post_counts_the_name_that_rides_inside_its_limit() -> None:
    app, state, _ = _channel_app()
    stub = state.adverts.stubs[0]

    with _client(app) as client:
        page = client.get("/chat/channel/2?identity=" + stub.identity.public_key.hex()).text

    assert f'data-limit="{MAX_CHANNEL_TEXT_LEN}"' in page
    assert 'data-prefix-from="identity"' in page
    assert 'data-prefix-separator=": "' in page


def test_a_message_is_submittable_from_the_keyboard_and_without_a_script() -> None:
    """Ctrl+Enter submits the form the button submits. Where no script runs at
    all, the composer is an ordinary form and the refusal at submission is
    unchanged — which is what makes the counter safe to be wrong."""
    script = (STATIC_DIR / "display.js").read_text()
    assert 'event.key !== "Enter" || !(event.ctrlKey || event.metaKey)' in script
    assert "form.requestSubmit()" in script
    assert "preventDefault" in script

    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    long = "x" * (MAX_TEXT_LEN + 3)

    # The same POST a keyboard send makes, with nothing the script contributes.
    with _client(app) as client:
        refused = client.post(
            f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={TOKEN_FIELD: csrf(client), "text": long},
        )

    assert refused.status_code == 400
    assert "3 over" in refused.text
    assert log.conversation(stub.identity.public_key, contact.public_key) == []
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


def test_a_refresh_updates_the_rows_that_changed_rather_than_all_of_them() -> None:
    """A conversation refreshes every three seconds. Replacing the whole table
    threw away whatever the reader had selected and wherever they had scrolled
    to; morphing touches only what differs, and each row carries the `ref` it is
    matched by, so a message arriving at the top leaves the rest alone
    (`web-chat`)."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    path = f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"

    with _client(app) as client:
        log.offer(
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="pkt-1",
                direction=INBOUND,
                text=b"first",
                outcome=RecordedOutcome.RECEIVED,
            )
        )
        first = client.get(f"{path}/messages").text
        log.offer(
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="pkt-2",
                direction=INBOUND,
                text=b"second",
                outcome=RecordedOutcome.RECEIVED,
            )
        )
        second = client.get(f"{path}/messages").text

    assert 'hx-ext="morph"' in first and 'hx-swap="morph:outerHTML"' in first
    assert 'id="m-pkt-1"' in first
    # The row drawn before is still identified the same way after a message
    # arrives above it, which is what the morph matches on.
    assert 'id="m-pkt-1"' in second and 'id="m-pkt-2"' in second
    assert second.index('id="m-pkt-2"') < second.index('id="m-pkt-1"'), (
        "newest first: the new row is above the one already on screen"
    )


def test_a_state_that_changed_is_redrawn_and_a_timestamp_stays_current() -> None:
    """The other half of the rule: morphing must not mean a stale row. A
    delivery state that moved is different content, so it is rewritten; and the
    relative times `display.js` maintains are re-rendered after every swap."""
    contact = _contact()
    app, state, log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    path = f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"

    with _client(app) as client:
        log.offer(
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="pkt-1",
                direction=OUTBOUND,
                text=b"hei",
                outcome=RecordedOutcome.IN_FLIGHT,
            )
        )
        awaiting = client.get(f"{path}/messages").text
        log.offer(
            _record(
                stub.identity.public_key,
                contact.public_key,
                ref="pkt-1",
                direction=OUTBOUND,
                text=b"hei",
                outcome=RecordedOutcome.ACKNOWLEDGED,
                attempts=1,
                ack_latency_ms=3010.0,
            )
        )
        acknowledged = client.get(f"{path}/messages").text

    assert "awaiting transmission" in awaiting
    assert "delivered — acknowledged after 1 attempt" in acknowledged
    assert "awaiting transmission" not in acknowledged
    # Same row, rewritten in place rather than a second one appended.
    assert acknowledged.count('id="m-pkt-1"') == 1

    script = (STATIC_DIR / "display.js").read_text()
    assert "htmx:afterSwap" in script, "relative times would stop advancing after a morph"


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
    contact = _contact("syn-harbor-repeater", verified=True)
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

    grp_txt = first_received_frame(PayloadType.GRP_TXT)
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
    contact = _contact("syn-alder", verified=True)
    state.contacts.restore([contact])
    log.offer(_received(2, "p1", "syn-alder"))

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
    # Rows now open with the `ref` the refresh matches them by.
    rows = body.split('<tr id="p-')[1:]
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


# --- The identity an operator chats as (`web-chat`) --------------------------


def _chatting_as(client: object, entity_id: str | None) -> None:
    """Put a default on the signed-in session, as sign-in and revalidation do."""
    session_of(client).default_entity_id = entity_id


def _selected(html: str) -> list[str]:
    """The option values the rendered select opens with."""
    return re.findall(r'<option value="([^"]*)"[^>]*selected', html)


def test_the_channel_composer_opens_with_the_operators_default_selected() -> None:
    app, state, _log = _channel_app(stub_names=("dev-companion", "dev-second"))
    first, second = tuple(state.adverts.stubs)

    with _client(app) as client:
        _chatting_as(client, second.entity_id)
        page = client.get("/chat/channel/1").text

    assert _selected(page) == [second.identity.public_key.hex()]
    assert first.identity.public_key.hex() in page, "every identity stays selectable"


def test_a_default_this_run_does_not_hold_selects_nothing_and_says_so() -> None:
    app, _state, _log = _channel_app(stub_names=("dev-companion",))

    with _client(app) as client:
        _chatting_as(client, "an-identity-this-run-never-loaded")
        page = client.get("/chat/channel/1").text
        posted = client.post(
            "/chat/channel/1",
            data={"text": "hej", "identity": "", TOKEN_FIELD: csrf(client)},
        )

    assert _selected(page) == [""], "the unchosen option, never a substitute"
    assert "An identity must be chosen" in posted.text
    assert posted.status_code == 400


def test_an_identity_named_in_the_query_wins_over_the_default() -> None:
    app, state, _log = _channel_app(stub_names=("dev-companion", "dev-second"))
    first, second = tuple(state.adverts.stubs)

    with _client(app) as client:
        _chatting_as(client, second.entity_id)
        page = client.get(f"/chat/channel/1?identity={first.identity.public_key.hex()}").text

    assert _selected(page) == [first.identity.public_key.hex()]


def test_only_stored_identities_are_offered_as_a_default() -> None:
    """A generated key does not outlive the run; the preference does."""
    app, _state, _log = _channel_app(stub_names=("dev-companion",))

    with _client(app) as client:
        index = client.get("/chat").text

    assert 'action="/chat/identity"' not in index, "no identity here can be kept"


def test_an_identity_with_no_stored_row_is_refused_as_a_default() -> None:
    app, state, _log = _channel_app(stub_names=("dev-companion",))
    generated = state.adverts.stubs[0]

    with _client(app) as client:
        refused_post = client.post(
            "/chat/identity",
            data={"identity": generated.identity.public_key.hex(), TOKEN_FIELD: csrf(client)},
        )

    assert refused_post.status_code == 400
    assert "not stored" in refused_post.text
    assert "any single message" in refused_post.text


def test_an_identity_this_run_does_not_hold_is_refused_as_a_default() -> None:
    app, _state, _log = _channel_app(stub_names=("dev-companion",))

    with _client(app) as client:
        refused_post = client.post(
            "/chat/identity",
            data={"identity": generate_identity().public_key.hex(), TOKEN_FIELD: csrf(client)},
        )

    assert refused_post.status_code == 400
    assert "not one this run holds" in refused_post.text


def test_setting_a_default_needs_the_session_token() -> None:
    app, state, _log = _channel_app(stub_names=("dev-companion",))
    stored = state.adverts.add_identity(
        "dev-stored", generate_identity(), entity_id=str(uuid.uuid4())
    )

    with signed_client(
        app, send_token=False, base_url="http://127.0.0.1:8080", follow_redirects=False
    ) as client:
        without = client.post("/chat/identity", data={"identity": stored.identity.public_key.hex()})

    assert without.status_code == 403


async def test_a_default_is_written_to_the_account_and_applied_at_once(
    database: Database,
) -> None:
    """The row is the durable home, the session is what the next page reads."""
    from sighop.db.persistence import Persistence
    from sighop.db.repositories import EntityRepository, WebUserRepository
    from sighop.protocol.payloads import NodeType

    persistence = Persistence(database=database)
    users = WebUserRepository(database=database)
    added = await users.add("dev-operator", password_hash="$argon2id$v=19$m=1,t=1,p=1$c2E$dGFn")
    assert isinstance(added, Succeeded)
    key = generate_identity()
    stored = await EntityRepository(database=database).store(
        name="dev-stored",
        identity=key,
        secret=base64.b64decode(generate_secret_key()),
        node_type=NodeType.CHAT,
    )
    assert isinstance(stored, Succeeded)

    state = stub_state(persistence=persistence, radio=EU868_NARROW)
    identity = state.adverts.add_identity("dev-stored", key, entity_id=str(stored.value.id))
    accounts = MemoryAccounts()
    accounts.add("dev-operator", "operator-password")
    app = create_app(
        state,
        auth=authenticator(accounts),
        hosts=HOSTS,
        logger=RecordingLogger(),
    )

    async with signed_async_client(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        session = session_of(client)
        set_it = await client.post(
            "/chat/identity",
            data={"identity": identity.identity.public_key.hex(), TOKEN_FIELD: session.csrf_token},
        )
        assert set_it.status_code == 303
        assert set_it.headers["location"] == "/chat"
        assert session.default_entity_id == str(stored.value.id), "applied to this session at once"
        held = await users.get("dev-operator")
        assert isinstance(held, Succeeded) and held.value is not None
        assert held.value.default_entity_id == stored.value.id, "and written to the account"

        channel_page = (await client.get("/chat")).text
        assert identity.identity.public_key.hex() in channel_page

        cleared = await client.post(
            "/chat/identity", data={"identity": "", TOKEN_FIELD: session.csrf_token}
        )
        assert cleared.status_code == 303
        assert session.default_entity_id is None
        held = await users.get("dev-operator")
        assert isinstance(held, Succeeded) and held.value is not None
        assert held.value.default_entity_id is None


async def test_a_default_only_ever_redirects_to_an_in_app_path(database: Database) -> None:
    """`safe_next`: a destination is a same-origin path or `/`, never a URL."""
    from sighop.db.persistence import Persistence
    from sighop.db.repositories import WebUserRepository

    persistence = Persistence(database=database)
    users = WebUserRepository(database=database)
    added = await users.add("dev-operator", password_hash="$argon2id$v=19$m=1,t=1,p=1$c2E$dGFn")
    assert isinstance(added, Succeeded)

    state = stub_state(persistence=persistence, radio=EU868_NARROW)
    accounts = MemoryAccounts()
    accounts.add("dev-operator", "operator-password")
    app = create_app(state, auth=authenticator(accounts), hosts=HOSTS, logger=RecordingLogger())

    async with signed_async_client(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        token = session_of(client).csrf_token
        for destination in ("https://evil.example/", "//evil.example", "/chat/channel/1"):
            answered = await client.post(
                "/chat/identity",
                data={"identity": "", "next": destination, TOKEN_FIELD: token},
            )
            assert answered.status_code == 303
            assert answered.headers["location"] in ("/", "/chat/channel/1")


def test_a_post_made_without_touching_the_selection_goes_as_the_default() -> None:
    """What the composer opens with is what a post is sent as — and any other
    loaded identity is still one selection away."""
    app, state, _log = _channel_app(stub_names=("dev-companion", "dev-second"))
    first, second = tuple(state.adverts.stubs)

    with _client(app) as client:
        _chatting_as(client, second.entity_id)
        opened = client.get("/chat/channel/2").text
        assert _selected(opened) == [second.identity.public_key.hex()]

        untouched = client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": "as my default",
                "identity": _selected(opened)[0],
            },
        )
        as_default = client.get(untouched.headers["location"]).text

        overridden = client.post(
            "/chat/channel/2",
            data={
                TOKEN_FIELD: csrf(client),
                "text": "as the other one",
                "identity": first.identity.public_key.hex(),
            },
        )
        as_other = client.get(overridden.headers["location"]).text

    assert untouched.status_code == 303 and overridden.status_code == 303
    assert "as my default" in as_default and "dev-second" in as_default
    assert "as the other one" in as_other and "dev-companion" in as_other


# --- The conversation composer chooses the identity (design D5) --------------


def test_the_conversation_composer_opens_on_the_conversations_own_identity() -> None:
    contact = _contact()
    app, state, _log = _built(contacts=[contact], stub_names=("first", "second"))
    one, two = state.adverts.stubs

    with _client(app) as client:
        page = client.get(f"/chat/{two.identity.public_key.hex()}/{contact.public_key.hex()}").text

    assert _selected(page) == [two.identity.public_key.hex()]
    assert one.identity.public_key.hex() in page, "every identity is one selection away"


def _recording_sends(state: StubState) -> list[tuple[str, str]]:
    """Every send the route makes, as (identity name, text) — the question the
    composer's selection answers."""
    made: list[tuple[str, str]] = []
    original = state.messenger.send

    async def record(entity, contact, text, **kwargs):
        made.append((entity.name, text))
        return await original(entity, contact, text, **kwargs)

    state.messenger.send = record  # type: ignore[method-assign]
    return made


async def _posted(app, path: str, **fields: str):
    """One POST, with the send's own task given a turn to run."""
    async with signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    ) as client:
        response = await client.post(path, data={TOKEN_FIELD: csrf(client), **fields})
        for _ in range(20):
            await asyncio.sleep(0)
        return response


async def test_sending_as_another_identity_lands_in_that_identitys_conversation() -> None:
    contact = _contact()
    app, state, _log = _built(contacts=[contact], stub_names=("first", "second"))
    one, two = state.adverts.stubs
    made = _recording_sends(state)

    sent = await _posted(
        app,
        f"/chat/{one.identity.public_key.hex()}/{contact.public_key.hex()}",
        text="sent as the second",
        identity=two.identity.public_key.hex(),
    )

    assert sent.status_code == 303
    assert sent.headers["location"] == (
        f"/chat/{two.identity.public_key.hex()}/{contact.public_key.hex()}"
    ), "the redirect lands in the conversation the message was sent as"
    assert made == [("second", "sent as the second")]


async def test_a_send_with_no_selection_behaves_as_it_always_has() -> None:
    """Every existing caller posts no `identity` at all; the path's own is used."""
    contact = _contact()
    app, state, _log = _built(contacts=[contact])
    stub = state.adverts.stubs[0]
    made = _recording_sends(state)

    sent = await _posted(
        app,
        f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}",
        text="unchanged",
    )

    assert sent.status_code == 303
    assert sent.headers["location"] == (
        f"/chat/{stub.identity.public_key.hex()}/{contact.public_key.hex()}"
    )
    assert made == [("companion", "unchanged")]


def test_a_refused_send_keeps_the_draft_and_the_identity_it_was_sent_as() -> None:
    contact = _contact()
    app, state, log = _built(contacts=[contact], stub_names=("first", "second"))
    one, two = state.adverts.stubs
    long = "x" * (MAX_TEXT_LEN + 3)

    with _client(app) as client:
        refused_send = client.post(
            f"/chat/{one.identity.public_key.hex()}/{contact.public_key.hex()}",
            data={
                TOKEN_FIELD: csrf(client),
                "text": long,
                "identity": two.identity.public_key.hex(),
            },
        )

    assert refused_send.status_code == 400
    assert long in refused_send.text, "the author's text was not preserved"
    assert _selected(refused_send.text) == [two.identity.public_key.hex()]
    assert log.conversation(two.identity.public_key, contact.public_key) == []


def test_one_operators_default_is_neither_seen_nor_changed_by_another() -> None:
    """The preference belongs to an account, not to the run (`web-auth`)."""
    app, state, _log = _channel_app(stub_names=("dev-companion", "dev-second"))
    first, second = tuple(state.adverts.stubs)
    accounts: MemoryAccounts = app.state.auth.accounts
    accounts.add("second-operator", "operator-password")

    with _client(app) as one, _client(app) as two:
        signed_in(two, "second-operator")
        _chatting_as(one, first.entity_id)
        _chatting_as(two, second.entity_id)

        assert _selected(one.get("/chat/channel/1").text) == [first.identity.public_key.hex()]
        assert _selected(two.get("/chat/channel/1").text) == [second.identity.public_key.hex()]
        assert session_of(one).default_entity_id == first.entity_id, "unchanged by the other"


def test_setting_clearing_and_applying_a_default_transmits_nothing() -> None:
    """Reading and preference-keeping are not radio events (`web-chat`)."""
    app, state, _log = _channel_app(stub_names=("dev-companion",))
    contact = _contact()
    state.contacts.restore([contact])
    stored = state.adverts.add_identity(
        "dev-stored", generate_identity(), entity_id=str(uuid.uuid4())
    )
    before = state.scheduler.status().stats.submitted

    with _client(app) as client:
        client.post(
            "/chat/identity",
            data={"identity": stored.identity.public_key.hex(), TOKEN_FIELD: csrf(client)},
        )
        client.post("/chat/identity", data={"identity": "", TOKEN_FIELD: csrf(client)})
        _chatting_as(client, stored.entity_id)
        client.get("/chat")
        client.get("/contacts")
        client.get("/chat/channel/1")
        client.get(f"/chat/{stored.identity.public_key.hex()}/{contact.public_key.hex()}")

    assert state.scheduler.status().stats.submitted == before
