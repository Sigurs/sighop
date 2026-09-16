"""The companion chat client (`web-chat`, §8 area 4).

The first surface from which a person drives a transmission without a command
line, and the place where the protocol's delivery semantics have to be shown as
they actually are — an acknowledgement is a receipt, an unacknowledged message
is *unknown* rather than lost, and a decrypted message's sender is a claimed
contact rather than a proven one.

Three shapes here are deliberate:

* **A send does not block the request.** `DirectMessenger.send` retries for up
  to four attempts across several seconds; a POST that waited for it would be a
  browser that appeared to hang. The record is written at submission, the send
  runs as its own task, and the conversation shows the state as it progresses.
* **Refusals happen before anything is queued** (design D11). Too long, no
  route without flooding chosen, and a closed gate are all decided here, with
  the author's text handed back — refuse rather than corrupt, on the one side of
  the protocol where a refusal can actually be heard.
* **Reading transmits nothing.** No receipt, no presence, no read marker. The
  mesh does not learn that a conversation was opened.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Succeeded
from sighop.net.channels import (
    MAX_CHANNEL_TEXT_LEN,
    ChannelMessageRecord,
    ChannelOutcome,
    ChannelPostError,
    LoadedChannel,
)
from sighop.net.contacts import Contact
from sighop.net.dm import (
    MAX_TEXT_LEN,
    DirectMessageRecord,
    NoRouteError,
    RecordedOutcome,
    choose_route,
)
from sighop.web.deps import Panel, panel
from sighop.web.render import identity_for, identity_for_key

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/chat")

SEE_OTHER = 303

CLAIMS_NOTE = (
    "Channel sender names are not authenticated: anyone holding the channel key "
    "can claim any name, so a name here is what a message says about itself."
)

GUESSABLE_NOTE = (
    "This channel's key is derivable by anyone who knows or guesses its name, so "
    "anyone can read and post in it."
)

PUBLIC_NOTE = (
    "A post to Public is flooded to the whole mesh and readable by anyone."
)

POST_NOTE = (
    "A post is one flooded transmission with no retry. No acknowledgement exists "
    "for channel messages: a repeat heard is evidence that a repeater forwarded "
    "it, not that anyone received it."
)

AUTHENTICATION_NOTE = (
    "A received message is identified by the contact whose key decrypted it. "
    "The protocol authenticates possession of that key, never the identity of "
    "whoever holds it — a decrypted message's sender is a claimed contact."
)

NOT_RECORDED = (
    "The database is unreachable, so messages from this point are not being "
    "recorded and will not survive the run: what is shown comes from this "
    "session's own memory. Sending and receiving continue."
)
"""The degraded state. Since milestone 9 a served panel always has a database —
its accounts live there — so "not recorded" only ever means an outage."""

HISTORY_UNREADABLE = (
    "Stored history cannot be read while the database is unreachable: only the "
    "messages this run has seen are shown, and new messages are not being "
    "recorded. Sending is still available."
)
"""A conversation opened (or refreshed) while its durable half cannot be read.
Different from `NOT_RECORDED`: here the page is also missing messages that exist,
and an operator must not read a short conversation as the whole of it."""


# --- The conversation list --------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def index(request: Request, page: PanelDep) -> HTMLResponse:
    """Conversations, keyed by the pair of a local identity and a contact."""
    rows = await _conversations(page)
    contacts = sorted(
        page.state.contacts.contacts(), key=lambda contact: contact.display_name.lower()
    )
    return page.page(
        request,
        "chat/index.html",
        conversations=rows,
        identities=list(page.state.adverts.stubs),
        contacts=contacts,
        # Built here rather than in the template: an identity is drawn by one
        # macro over one view, and a template that constructed its own would be
        # a second place §8's verification rule could be got wrong (design D13).
        contact_views={
            contact.public_key.hex(): identity_for(contact) for contact in contacts
        },
        channels=[
            {"channel": channel, "new": page.channel_log.new_for(channel.id)}
            for channel in page.state.channels.channels
        ],
        channels_skipped=page.state.channels.channels.skipped,
        recorded=_recorded(page),
        not_recorded=NOT_RECORDED,
    )


def _recorded(page: Panel) -> bool:
    """Whether what happens now reaches the database. False only in an outage
    on a served panel; a page test's stub with no persistence records nothing
    either, and says so rather than claiming otherwise."""
    return page.persistence is not None and not page.degraded


async def _conversations(page: Panel) -> list[dict[str, object]]:
    """Every conversation this run can see, durable and in-memory alike.

    Keyed by `(identity, peer)` in both, so the two views merge without ever
    putting one identity's messages under another's name.
    """
    seen: dict[tuple[bytes, bytes], dict[str, object]] = {}
    if page.persistence is not None:
        listed = await page.persistence.direct_messages.conversations()
        if isinstance(listed, Succeeded):
            for summary in listed.value:
                key = (summary.entity_public_key, summary.peer_public_key)
                seen[key] = _row(page, key, summary.messages, summary.latest_at)
    for key in page.chat.conversation_keys():
        latest = page.chat.latest(key)
        if key not in seen:
            seen[key] = _row(
                page,
                key,
                len(page.chat.conversation(*key)),
                latest.handled_at if latest else None,
            )
        seen[key]["new"] = page.chat.new_for(key)
    oldest = dt.datetime.min.replace(tzinfo=dt.UTC)

    def when(row: dict[str, object]) -> dt.datetime:
        latest = row["latest_at"]
        return latest if isinstance(latest, dt.datetime) else oldest

    return sorted(seen.values(), key=when, reverse=True)


def _row(
    page: Panel,
    key: tuple[bytes, bytes],
    messages: int,
    latest_at: dt.datetime | None,
) -> dict[str, object]:
    entity_key, peer_key = key
    return {
        "entity_key": entity_key.hex(),
        "peer_key": peer_key.hex(),
        "entity_name": _entity_name(page, entity_key),
        "peer": identity_for_key(peer_key, page.state.contacts),
        "messages": messages,
        "latest_at": latest_at,
        "new": page.chat.new_for(key),
    }


def _entity_name(page: Panel, public_key: bytes) -> str:
    for stub in page.state.adverts.stubs:
        if stub.identity.public_key == public_key:
            return stub.name
    return public_key.hex()[:16]


# --- One channel (channel-messaging D8) -------------------------------------
#
# Declared before the conversation routes, whose two path segments would
# otherwise also match `/chat/channel/{id}`.


@router.get("/channel/{channel_id}", response_class=HTMLResponse)
async def channel(
    channel_id: int, request: Request, page: PanelDep, identity: str = ""
) -> HTMLResponse:
    """One channel, newest first. Reading needs no identity and transmits nothing."""
    loaded = page.state.channels.channels.by_id(channel_id)
    if loaded is None:
        return page.page(request, "chat/missing.html", status_code=404)
    page.channel_log.opened(channel_id)
    context = await _channel_context(page, loaded)
    context["chosen"] = identity
    return page.page(request, "chat/channel.html", **context)


@router.get("/channel/{channel_id}/messages", response_class=HTMLResponse)
async def channel_messages(channel_id: int, request: Request, page: PanelDep) -> HTMLResponse:
    """The message list alone, for the partial refresh. A GET; transmits nothing."""
    loaded = page.state.channels.channels.by_id(channel_id)
    if loaded is None:
        return page.page(request, "chat/missing.html", status_code=404)
    page.channel_log.opened(channel_id)
    context = await _channel_context(page, loaded)
    return page.page(request, "chat/_channel_messages.html", **context)


@router.post("/channel/{channel_id}", response_model=None)
async def post_to_channel(
    channel_id: int,
    request: Request,
    page: PanelDep,
    text: Annotated[str, Form()] = "",
    identity: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Refuse, or submit one post through the platform's own path — never truncate.

    Every refusal the messenger makes is made before anything is composed, so
    the post is refused here with its reason and the author's text handed back,
    and nothing is recorded. A post that is submitted returns at once: there is
    no acknowledgement to wait for, and the outcome arrives in the refreshed list.
    """
    loaded = page.state.channels.channels.by_id(channel_id)
    if loaded is None:
        return page.page(request, "chat/missing.html", status_code=404)
    entity = _entity(page, identity) if identity else None
    refusal = ""
    if entity is None:
        refusal = (
            "An identity must be chosen: a channel post is sent as one of this "
            "run's identities."
        )
    elif not text.encode("utf-8"):
        refusal = "There is nothing to post."
    else:
        try:
            page.state.channels.post(channel_id, entity, text, actor=page.actor(request))
        except ChannelPostError as exc:
            refusal = str(exc)
    if refusal:
        context = await _channel_context(page, loaded)
        context.update(refusal=refusal, draft=text, chosen=identity)
        return page.page(request, "chat/channel.html", status_code=400, **context)
    return RedirectResponse(
        f"/chat/channel/{channel_id}?identity={identity}", status_code=SEE_OTHER
    )


async def _channel_context(page: Panel, loaded: LoadedChannel) -> dict[str, Any]:
    messages, readable = await _channel_messages(page, loaded.id)
    return {
        "channel": loaded,
        "messages": messages,
        "identities": list(page.state.adverts.stubs),
        "chosen": "",
        "max_text_len": MAX_CHANNEL_TEXT_LEN,
        "claims_note": CLAIMS_NOTE,
        "guessable_note": GUESSABLE_NOTE,
        "public_note": PUBLIC_NOTE,
        "post_note": POST_NOTE,
        "recorded": _recorded(page),
        "not_recorded": NOT_RECORDED,
        "history_readable": readable,
        "history_unreadable": HISTORY_UNREADABLE,
        "refusal": "",
        "draft": "",
    }


async def _channel_messages(
    page: Panel, channel_id: int
) -> tuple[list[dict[str, object]], bool]:
    """Durable rows and this session's own, merged on `ref`; the session's copy wins."""
    merged: dict[str, ChannelMessageRecord] = {}
    readable = False
    if page.persistence is not None:
        stored = await page.persistence.channel_messages.recent(channel_id)
        if isinstance(stored, Succeeded):
            merged = {record.ref: record for record in stored.value}
            readable = True
    for record in page.channel_log.messages(channel_id):
        merged[record.ref] = record
    ordered = sorted(merged.values(), key=lambda record: record.handled_at, reverse=True)
    return [_channel_message_view(page, record) for record in ordered], readable


def _channel_message_view(page: Panel, record: ChannelMessageRecord) -> dict[str, object]:
    rendered = record.rendered()
    handled = int(record.handled_at.timestamp())
    wire_time = (
        ""
        if record.wire_timestamp == handled
        else dt.datetime.fromtimestamp(record.wire_timestamp, dt.UTC).isoformat()
    )
    return {
        "ref": record.ref,
        "inbound": record.inbound,
        "claimed": record.unverified_sender_name,
        "entity_name": (
            ""
            if record.entity_public_key is None
            else _entity_name(page, record.entity_public_key)
        ),
        "text": rendered.text,
        "displayable": rendered.is_valid_utf8,
        "raw": record.text.hex(),
        "handled_at": record.handled_at,
        "wire_time": wire_time,
        "state": _channel_state_text(record),
    }


def _channel_state_text(record: ChannelMessageRecord) -> str:
    """A channel message's state, in the only terms a channel offers."""
    if record.inbound:
        hops = "?" if record.hop_count is None else str(record.hop_count)
        return f"received, {hops} hop(s)"
    repeats = (
        "no repeat heard — which does not mean it was not received"
        if record.repeats_heard == 0
        else f"repeat heard {record.repeats_heard}x — a repeater forwarded it"
    )
    match record.outcome:
        case ChannelOutcome.AWAITING:
            return "awaiting transmission"
        case ChannelOutcome.TRANSMITTED:
            return f"transmitted; no acknowledgement exists for channel messages; {repeats}"
        case ChannelOutcome.NOT_TRANSMITTED:
            return f"not transmitted — {record.outcome_reason or 'no reason given'}; not retried"
        case _:
            return "outcome unknown — the run stopped before it resolved"


# --- One conversation -------------------------------------------------------


@router.get("/{entity_key}/{peer_key}", response_class=HTMLResponse)
async def conversation(
    entity_key: str, peer_key: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """One conversation, newest first. Transmits nothing (`web-chat`)."""
    entity = _entity(page, entity_key)
    contact = _contact(page, peer_key)
    if entity is None or contact is None:
        return page.page(request, "chat/missing.html", status_code=404)
    page.chat.opened(entity.identity.public_key, contact.public_key)
    context = await _conversation_context(page, entity, contact)
    return page.page(request, "chat/conversation.html", **context)


@router.get("/{entity_key}/{peer_key}/messages", response_class=HTMLResponse)
async def messages(
    entity_key: str, peer_key: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """The message list alone, for the panel's own partial refresh.

    What makes a received message appear without a reload, and what shows a
    send's state moving from awaiting to acknowledged. It is a GET and it
    transmits nothing.
    """
    entity = _entity(page, entity_key)
    contact = _contact(page, peer_key)
    if entity is None or contact is None:
        return page.page(request, "chat/missing.html", status_code=404)
    page.chat.opened(entity.identity.public_key, contact.public_key)
    context = await _conversation_context(page, entity, contact)
    return page.page(request, "chat/_messages.html", **context)


async def _conversation_context(
    page: Panel, entity: object, contact: Contact
) -> dict[str, Any]:
    entity_key = entity.identity.public_key  # type: ignore[attr-defined]
    messages, history_readable = await _messages(page, entity_key, contact.public_key)
    return {
        "entity": entity,
        "entity_key": entity_key.hex(),
        "contact": contact,
        "peer": identity_for(contact),
        "peer_key": contact.public_key.hex(),
        "messages": messages,
        "max_text_len": MAX_TEXT_LEN,
        "route": _route_note(page, contact),
        "authentication_note": AUTHENTICATION_NOTE,
        "recorded": _recorded(page),
        "not_recorded": NOT_RECORDED,
        "history_readable": history_readable,
        "history_unreadable": HISTORY_UNREADABLE,
        "refusal": "",
        "draft": "",
    }


async def _messages(
    page: Panel, entity_key: bytes, peer_key: bytes
) -> tuple[list[dict[str, object]], bool]:
    """This conversation, durable rows and this session's own, merged on `ref`.

    The session's copy wins where both hold a message: it is the more recent
    knowledge of the same row, because the durable write happens behind the
    message path rather than in front of it. The second value says whether the
    durable half was read at all.
    """
    merged: dict[str, DirectMessageRecord] = {}
    readable = False
    if page.persistence is not None:
        stored = await page.persistence.direct_messages.conversation(
            entity_key, peer_key
        )
        if isinstance(stored, Succeeded):
            merged = {record.ref: record for record in stored.value}
            readable = True
    for record in page.chat.conversation(entity_key, peer_key):
        merged[record.ref] = record
    ordered = sorted(merged.values(), key=lambda record: record.handled_at, reverse=True)
    return [_message_view(record) for record in ordered], readable


def _message_view(record: DirectMessageRecord) -> dict[str, object]:
    rendered = record.rendered()
    return {
        "ref": record.ref,
        "inbound": record.inbound,
        "direction": record.direction,
        "text": rendered.text,
        "displayable": rendered.is_valid_utf8,
        "raw": record.text.hex(),
        "handled_at": record.handled_at,
        "wire_timestamp": record.wire_timestamp,
        "outcome": str(record.outcome),
        "attempts": record.attempts,
        "ack_latency_ms": record.ack_latency_ms,
        "state": _state_text(record),
    }


def _state_text(record: DirectMessageRecord) -> str:
    """A message's delivery state, in the terms the protocol actually offers."""
    if record.inbound:
        return "received"
    match record.outcome:
        case RecordedOutcome.IN_FLIGHT:
            return (
                "awaiting transmission"
                if record.attempts == 0
                else f"attempt {record.attempts} in progress"
            )
        case RecordedOutcome.ACKNOWLEDGED:
            latency = (
                "" if record.ack_latency_ms is None else f", {record.ack_latency_ms:.0f} ms"
            )
            return f"delivered — acknowledged after {record.attempts} attempt(s){latency}"
        case RecordedOutcome.UNACKNOWLEDGED:
            return (
                f"unacknowledged after {record.attempts} attempt(s) — the platform "
                "cannot tell whether it arrived"
            )
        case RecordedOutcome.DROPPED:
            return "never reached the air — the scheduler dropped it"
        case _:
            return str(record.outcome)


def _route_note(page: Panel, contact: Contact) -> dict[str, object]:
    """Whether a message to this contact can go without flooding, and why not."""
    try:
        route = choose_route(page.state.pipeline.paths, contact, allow_flood=False)
    except NoRouteError as exc:
        return {"known": False, "label": "", "reason": str(exc)}
    return {"known": True, "label": route.label, "reason": ""}


# --- Sending ----------------------------------------------------------------


@router.post("/{entity_key}/{peer_key}", response_model=None)
async def send(
    entity_key: str,
    peer_key: str,
    request: Request,
    page: PanelDep,
    text: Annotated[str, Form()] = "",
    flood: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Compose, refuse or submit — and never truncate (design D11).

    Every refusal below hands the author's text back. The room server truncates
    an inbound post because there is no way to say no to a radio; there is every
    way to say no to a form, and this is the side of the protocol where that is
    true.
    """
    entity = _entity(page, entity_key)
    contact = _contact(page, peer_key)
    if entity is None or contact is None:
        return page.page(request, "chat/missing.html", status_code=404)

    flooding = flood == "yes"
    refusal = _refusal(page, contact, text, flooding=flooding)
    if refusal:
        context = await _conversation_context(page, entity, contact)
        context["refusal"] = refusal
        context["draft"] = text
        return page.page(request, "chat/conversation.html", status_code=400, **context)

    # The send runs as its own task: four attempts across several seconds is not
    # something a request should hold a browser open for. The record is written
    # at submission, so the message is in the conversation before this returns.
    asyncio.create_task(  # noqa: RUF006 - the messenger owns the send's lifetime
        _send(page, entity, contact, text, flooding=flooding),
        name="web-chat-send",
    )
    return RedirectResponse(f"/chat/{entity_key}/{peer_key}", status_code=SEE_OTHER)


def _refusal(page: Panel, contact: Contact, text: str, *, flooding: bool) -> str:
    """Why this message cannot be sent, or an empty string.

    Checked here rather than left to the send path, because the send path's
    refusal arrives seconds later and by then the author's text is gone.
    """
    raw = text.encode("utf-8")
    if not raw:
        return "There is nothing to send."
    if len(raw) > MAX_TEXT_LEN:
        over = len(raw) - MAX_TEXT_LEN
        return (
            f"A direct message carries at most {MAX_TEXT_LEN} bytes; this is "
            f"{len(raw)}, which is {over} over. Your text is below, unchanged — "
            "nothing was truncated and nothing was sent."
        )
    if not page.state.scheduler.transmit_enabled:
        return (
            "Transmission is disabled for this run, so nothing was queued: a "
            "message held now would be delivered later by a change of gate, "
            "which is not what sending meant when you pressed the button."
        )
    if not flooding:
        try:
            choose_route(page.state.pipeline.paths, contact, allow_flood=False)
        except NoRouteError as exc:
            return (
                f"{exc} Flooding is offered below as an explicit choice; it is "
                "not performed on your behalf."
            )
    return ""


async def _send(
    page: Panel, entity: object, contact: Contact, text: str, *, flooding: bool
) -> None:
    """One send through `DirectMessenger.send` — the platform's own path.

    The same composition, routing, retry and acknowledgement path a
    command-line send uses, so a message from the browser gets the priority
    class, retry bounds and acknowledgement window every direct message gets.
    """
    try:
        await page.state.messenger.send(
            entity,  # type: ignore[arg-type]
            contact,
            text,
            allow_flood=flooding,
        )
    except Exception as exc:
        page.logger.error(
            "web_chat_send_failed",
            outcome="error",
            peer=contact.public_key.hex()[:16],
            error=repr(exc),
        )


# --- Lookups ----------------------------------------------------------------


def _entity(page: Panel, entity_key: str):  # type: ignore[no-untyped-def]
    for stub in page.state.adverts.stubs:
        if stub.identity.public_key.hex() == entity_key:
            return stub
    return None


def _contact(page: Panel, peer_key: str) -> Contact | None:
    try:
        wanted = bytes.fromhex(peer_key)
    except ValueError:
        return None
    for contact in page.state.contacts.contacts():
        if contact.public_key == wanted:
            return contact
    return None
