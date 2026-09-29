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
import unicodedata
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Failed, Succeeded
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
from sighop.protocol.payloads import GROUP_NAME_SEPARATOR
from sighop.web.auth import safe_next
from sighop.web.deps import Panel, panel
from sighop.web.guard import current_session
from sighop.web.render import (
    Refusal,
    Status,
    default_identity,
    duration,
    identity_for,
    identity_for_key,
    plural,
    refused,
)
from sighop.webhooks.events import HopLookup, resolve_hop

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

PUBLIC_NOTE = "A post to Public is flooded to the whole mesh and readable by anyone."

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
async def index(
    request: Request, page: PanelDep, added: str = "", removed: str = ""
) -> HTMLResponse:
    """Conversations, keyed by the pair of a local identity and a contact."""
    return await render_index(request, page, added=added, removed=removed)


async def render_index(
    request: Request,
    page: Panel,
    *,
    added: str = "",
    removed: str = "",
    refusal: Refusal | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The chat page, for a GET and for a refused channel write alike (design D3).

    One builder, so a refused addition is re-shown on the page the channel
    list is on — with the key field empty, because it is the same template code.
    """
    return page.page(
        request,
        "chat/index.html",
        conversations=await _conversations(page),
        identities=list(page.state.adverts.stubs),
        chatting_as=chatting_as(page, request),
        chatting_as_note=CHATTING_AS_NOTE,
        storable_identities=storable_identities(page),
        recorded=_recorded(page),
        not_recorded=NOT_RECORDED,
        **await channels_context(page, added=added, removed=removed),
        refusal=refusal,
        status_code=status_code,
    )


# --- Channels (channel-messaging, `web-admin`) -------------------------------
#
# Every write is `ChannelRepository`'s own call (in `routes/admin.py`), so a
# hashtag or key refused here is refused in the repository's own words. A pasted
# pre-shared key is never put back into a page, not even into the form re-shown
# after a refusal, and a stored one is never read back at all (`channel-store`).

CHANNEL_KEY_NEEDS_THE_SECRET = (
    "SIGHOP_SECRET_KEY is not available to this panel, and pre-shared keys are "
    "sealed under it; nothing was added"
)

STORED_KEYS_ARE_NEVER_READ_BACK = (
    "A stored pre-shared key is never shown — here or anywhere else — and sighop "
    "cannot give one back. To share a channel, share the key you added, from "
    "wherever you keep it. The key is the credential for reading and posting in "
    "the channel, and a key cannot be taken back from whoever has seen it, so a "
    "stolen session that could display it would hand the channel out for good."
)
"""`channel-store`: said where channels are listed, so an operator learns it
before they need the key rather than when they go looking for it."""

PSK_IS_NOT_SHOWN_AGAIN = (
    "The key will not be shown again, by this page or by anything else: keep the "
    "copy you are pasting from."
)
"""`channel-store`: said on the form itself, at the moment the key is supplied."""


async def channels_context(page: Panel, *, added: str, removed: str) -> dict[str, object]:
    """The channel list, the loaded and the stored joined by id, and its forms.

    A loaded channel carries what this run knows (unread markers, the open
    link); a stored one what the database knows (kind, message count, the
    rename and remove links). One row per channel either way, so a stored
    channel this run could not load is listed rather than left to a warning.
    """
    from sighop.db.repositories import GUESSABLE_STATEMENT

    loaded = {channel.id: channel for channel in page.state.channels.channels}
    counts: dict[int, int] = {}
    listed = await page.persistence.channels.list_all()
    counted = await page.persistence.channels.message_counts()
    if isinstance(counted, Succeeded):
        counts = counted.value
    records = listed.value if isinstance(listed, Succeeded) else []
    rows: list[dict[str, object]] = [
        {
            "id": record.id,
            "name": record.name,
            "kind": record.kind,
            "channel_hash": record.channel_hash,
            "guessable": record.guessable,
            "messages": counts.get(record.id, 0),
            "stored": True,
            "loaded": record.id in loaded,
            "new": page.channel_log.new_for(record.id) if record.id in loaded else 0,
        }
        for record in records
    ]
    stored = {record.id for record in records}
    rows.extend(
        {
            "id": channel.id,
            "name": channel.name,
            "kind": None,
            "channel_hash": channel.channel_hash,
            "guessable": channel.guessable,
            "messages": None,
            "stored": False,
            "loaded": True,
            "new": page.channel_log.new_for(channel.id),
        }
        for channel in page.state.channels.channels
        if channel.id not in stored
    )
    return {
        "channels": rows,
        "channels_unreadable": isinstance(listed, Failed),
        "channels_skipped": page.state.channels.channels.skipped,
        "public_stored": any(record.kind == "public" for record in records),
        "guessable_statement": GUESSABLE_STATEMENT,
        "guessable_status": GUESSABLE,
        "keys_note": STORED_KEYS_ARE_NEVER_READ_BACK,
        "psk_note": PSK_IS_NOT_SHOWN_AGAIN,
        "just_added": next((record for record in records if record.name == added), None),
        "removed": removed,
    }


def _recorded(page: Panel) -> bool:
    """Whether what happens now reaches the database. False only in an outage."""
    return not page.degraded


async def _conversations(page: Panel) -> list[dict[str, object]]:
    """Every conversation this run can see, durable and in-memory alike.

    Keyed by `(identity, peer)` in both, so the two views merge without ever
    putting one identity's messages under another's name.
    """
    seen: dict[tuple[bytes, bytes], dict[str, object]] = {}
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


CHATTING_AS_NOTE = (
    "Your composers open with this identity chosen; every message can still be "
    "sent as another. It belongs to your account, not to this run: another "
    "operator signed in here keeps their own."
)

DEFAULT_IDENTITY_NEEDS_DURABLE_STORAGE = (
    "A default identity to chat as belongs to your account, and accounts are "
    "stored in the database. It is unreachable, so nothing was changed and the "
    "default in force is the one you already had."
)

DEFAULT_IDENTITY_NOT_HELD = (
    "That identity is not one this run holds, so it was not made your default: "
    "a default names an identity this platform can compose as."
)

DEFAULT_IDENTITY_NOT_STORED = (
    "That identity is not stored: it was generated for this run or loaded from "
    "a keyfile, and a default is held against your account, which outlives both. "
    "Nothing was changed; it can still be chosen for any single message."
)


@router.post("/identity", response_model=None)
async def set_chatting_as(
    request: Request,
    page: PanelDep,
    identity: Annotated[str, Form()] = "",
    next_path: Annotated[str, Form(alias="next")] = "/chat",
) -> RedirectResponse | HTMLResponse:
    """Set or clear the identity this operator chats as. Transmits nothing.

    A preference, so no password step: it grants nothing, changes no key
    material and reaches no radio (design D7). An empty `identity` clears it.
    The session is updated with the row, so the composers change on the next
    page rather than at the next revalidation.
    """
    stub = None if not identity else _entity(page, identity)
    if identity and stub is None:
        return await render_index(
            request,
            page,
            refusal=refused(DEFAULT_IDENTITY_NOT_HELD, field="identity"),
            status_code=400,
        )
    # What was submitted is judged before the database is: an outage is not the
    # reason an identity this run cannot keep was refused.
    chosen = None
    if stub is not None:
        chosen = _stored_id(stub)
        if chosen is None:
            return await render_index(
                request,
                page,
                refusal=refused(DEFAULT_IDENTITY_NOT_STORED, field="identity"),
                status_code=400,
            )
    if page.degraded:
        return await render_index(
            request,
            page,
            refusal=refused(DEFAULT_IDENTITY_NEEDS_DURABLE_STORAGE, field="identity"),
            status_code=400,
        )
    from sighop.db.repositories import UnknownIdentityError

    try:
        written = await page.persistence.web_users.set_default_identity(page.actor(request), chosen)
    except UnknownIdentityError:
        # The identity was removed — by another process, or between this page
        # being drawn and submitted. Refused like any other unstored identity,
        # rather than surfacing as an error page.
        return await render_index(
            request,
            page,
            refusal=refused(DEFAULT_IDENTITY_NOT_STORED, field="identity"),
            status_code=400,
        )
    if isinstance(written, Failed):
        return await render_index(
            request,
            page,
            refusal=refused(DEFAULT_IDENTITY_NEEDS_DURABLE_STORAGE, field="identity"),
            status_code=400,
        )
    session = current_session(request)
    if session is not None:
        session.default_entity_id = None if stub is None else stub.entity_id
    return RedirectResponse(safe_next(next_path), status_code=SEE_OTHER)


def _stored_id(stub: object) -> uuid.UUID | None:
    """The `entity` row this identity is, or `None` when it has no row.

    A stored identity is adopted under its row id; one generated for this run or
    loaded from a keyfile is adopted under its name, and has no row for an
    account's default to reference (`runtime._adopt_entity`).
    """
    try:
        return uuid.UUID(stub.entity_id)  # type: ignore[attr-defined]
    except ValueError:
        return None


def storable_identities(page: Panel) -> list[object]:
    """The identities that can be an account's default: the stored ones.

    Every loaded identity stays selectable for a single message; only these can
    be *kept*, because the preference outlives the run and a generated key does
    not.
    """
    return [stub for stub in page.state.adverts.stubs if _stored_id(stub) is not None]


def chatting_as(page: Panel, request: Request) -> str:
    """The public key of the identity this operator chats as, or an empty string.

    One lookup behind every composer and the contact list: the account's choice
    travels on its session, so this awaits nothing and a degraded database
    leaves the default in force rather than changing who a post is composed as.
    An identity this run does not hold resolves to nothing at all, never to a
    substitute (`web-chat`).
    """
    session = current_session(request)
    stub = default_identity(
        page.state.adverts.stubs, None if session is None else session.default_entity_id
    )
    return "" if stub is None else stub.identity.public_key.hex()


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
    context = await _channel_context(page, loaded, request)
    # An identity named in the query is this page's own choice — the one a post
    # was just made as — so it wins over the standing default.
    context["chosen"] = identity or context["chosen"]
    return page.page(request, "chat/channel.html", **context)


@router.get("/channel/{channel_id}/messages", response_class=HTMLResponse)
async def channel_messages(channel_id: int, request: Request, page: PanelDep) -> HTMLResponse:
    """The message list alone, for the partial refresh. A GET; transmits nothing."""
    loaded = page.state.channels.channels.by_id(channel_id)
    if loaded is None:
        return page.page(request, "chat/missing.html", status_code=404)
    page.channel_log.opened(channel_id)
    context = await _channel_context(page, loaded, request)
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
            "An identity must be chosen: a channel post is sent as one of this run's identities."
        )
    elif not text.encode("utf-8"):
        refusal = "There is nothing to post."
    else:
        try:
            page.state.channels.post(channel_id, entity, text, actor=page.actor(request))
        except ChannelPostError as exc:
            refusal = str(exc)
    if refusal:
        context = await _channel_context(page, loaded, request)
        context.update(refusal=refusal, draft=text, chosen=identity)
        return page.page(request, "chat/channel.html", status_code=400, **context)
    return RedirectResponse(
        f"/chat/channel/{channel_id}?identity={identity}", status_code=SEE_OTHER
    )


async def _channel_context(page: Panel, loaded: LoadedChannel, request: Request) -> dict[str, Any]:
    messages, readable = await _channel_messages(page, loaded.id)
    return {
        "channel": loaded,
        "messages": messages,
        "identities": list(page.state.adverts.stubs),
        "chosen": chatting_as(page, request),
        "max_text_len": MAX_CHANNEL_TEXT_LEN,
        # What rides inside the limit besides the text, so the composer counts
        # the bytes `check_post_length` counts rather than restating the rule.
        "name_separator": GROUP_NAME_SEPARATOR,
        "claims_note": CLAIMS_NOTE,
        "guessable_note": GUESSABLE_NOTE,
        "guessable_status": GUESSABLE,
        "public_note": PUBLIC_NOTE,
        "post_note": POST_NOTE,
        "recorded": _recorded(page),
        "not_recorded": NOT_RECORDED,
        "history_readable": readable,
        "history_unreadable": HISTORY_UNREADABLE,
        "refusal": "",
        "draft": "",
    }


async def _channel_messages(page: Panel, channel_id: int) -> tuple[list[dict[str, object]], bool]:
    """Durable rows and this session's own, merged on `ref`; the session's copy wins."""
    merged: dict[str, ChannelMessageRecord] = {}
    readable = False
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
        None
        if record.wire_timestamp == handled
        else dt.datetime.fromtimestamp(record.wire_timestamp, dt.UTC)
    )
    return {
        "ref": record.ref,
        "inbound": record.inbound,
        "claimed": record.unverified_sender_name,
        "entity_name": (
            "" if record.entity_public_key is None else _entity_name(page, record.entity_public_key)
        ),
        "text": rendered.text,
        "displayable": rendered.is_valid_utf8,
        "raw": record.text.hex(),
        "handled_at": record.handled_at,
        "wire_time": wire_time,
        "state": _channel_state(record),
        "path_copy": _path_copy(record, page.state.contacts),
    }


DIRECT = "direct"
"""A copy heard without any hop, where a path would be written."""

PATH_ARROW = " → "
"""Between hops, as the Discord notification writes a path."""


def _path_hops(
    paths: tuple[bytes, ...] | None, hash_size: int | None
) -> tuple[tuple[bytes, ...], ...]:
    """Each recorded copy's path as its hop hashes, in arrival order; `()` for direct.

    A path whose length is not a whole number of hashes is left out rather than
    split at a guess.
    """
    if paths is None or not hash_size:
        return ()
    return tuple(
        tuple(path[i : i + hash_size] for i in range(0, len(path), hash_size))
        for path in paths
        if not len(path) % hash_size
    )


def _received_hops(record: ChannelMessageRecord) -> tuple[tuple[bytes, ...], ...]:
    """A received message's paths, or none when no copy came over a hop."""
    if not record.inbound:
        return ()
    paths = _path_hops(record.paths, record.path_hash_size)
    return paths if any(paths) else ()


def _hop_label(hop: bytes, contacts: HopLookup) -> str:
    """A hop as the Discord notification names it, as plain text. A name is advert
    content: control characters become spaces so a path stays on one line."""
    label = resolve_hop(hop, contacts).label
    flattened = "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in label
    )
    return f"{hop.hex()} {flattened}"


def _path_copy(record: ChannelMessageRecord, contacts: HopLookup) -> str | None:
    """What the copy control copies: one path per line, each hop named from the
    contacts known now. None hides the control."""
    return (
        "\n".join(
            PATH_ARROW.join(_hop_label(hop, contacts) for hop in hops) or DIRECT
            for hops in _received_hops(record)
        )
        or None
    )


def _channel_state(record: ChannelMessageRecord) -> tuple[Status, ...]:
    """A channel message's state, in the only terms a channel offers.

    That no acknowledgement exists is said in the transmitted glyph's hover and
    once on the page (`POST_NOTE`), not spelled out again on every row. A
    received message's paths are said in its hover too, comma-separated because
    a title's line breaks render differently from browser to browser; the
    figure stays the hop count, so the column never widens.
    """
    if record.inbound:
        if record.hop_count is None:
            return (Status("received", "↓", ("?",), "received over an unknown number of hops"),)
        hops = plural(record.hop_count, "hop")
        explanation = f"received over {hops}"
        if paths := _received_hops(record):
            lines = [PATH_ARROW.join(hop.hex() for hop in hops) or DIRECT for hops in paths]
            copies = plural(len(lines), "copy", "copies")
            explanation += f"; {copies} heard, via {', '.join(lines)}"
        return (Status("received", "↓", (str(record.hop_count),), explanation),)
    match record.outcome:
        case ChannelOutcome.AWAITING:
            return (Status("awaiting", "◷", explanation="awaiting transmission"),)
        case ChannelOutcome.TRANSMITTED:
            transmitted = "transmitted; no acknowledgement exists for channel messages"
            if record.repeats_heard == 0:
                return (
                    Status(
                        "transmitted",
                        "↑",
                        explanation=(
                            f"{transmitted}; no repeat heard — which does not mean "
                            "it was not received"
                        ),
                    ),
                )
            times = plural(record.repeats_heard, "time")
            return (
                Status("transmitted", "↑", explanation=transmitted),
                Status(
                    "repeats",
                    "⟲",
                    (str(record.repeats_heard),),
                    f"repeat heard {times} — a repeater forwarded it",
                ),
            )
        case ChannelOutcome.NOT_TRANSMITTED:
            reason = record.outcome_reason or "no reason given"
            return (
                Status(
                    "not-transmitted",
                    "✕",
                    explanation=f"not transmitted — {reason}; not retried",
                    reason=reason,
                ),
            )
        case _:
            return (UNKNOWN,)


UNKNOWN = Status("unknown", "⁇", explanation="outcome unknown — the run stopped before it resolved")

GUESSABLE = Status("guessable", "◌", explanation=GUESSABLE_NOTE)


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


async def _conversation_context(page: Panel, entity: object, contact: Contact) -> dict[str, Any]:
    entity_key = entity.identity.public_key  # type: ignore[attr-defined]
    messages, history_readable = await _messages(page, entity_key, contact.public_key)
    return {
        "entity": entity,
        "entity_key": entity_key.hex(),
        "identities": list(page.state.adverts.stubs),
        # The conversation shown is the one being sent in, so the composer opens
        # on its own identity: the default already decided which conversation the
        # contact list opened (design D5).
        "chosen": entity_key.hex(),
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
    stored = await page.persistence.direct_messages.conversation(entity_key, peer_key)
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
        "state": _state(record),
    }


def _state(record: DirectMessageRecord) -> tuple[Status, ...]:
    """A message's delivery state, in the terms the protocol actually offers."""
    if record.inbound:
        return (Status("received", "↓", explanation="received"),)
    attempts = plural(record.attempts, "attempt")
    match record.outcome:
        case RecordedOutcome.IN_FLIGHT:
            if record.attempts == 0:
                return (Status("awaiting", "◷", explanation="awaiting transmission"),)
            return (
                Status(
                    "attempt",
                    "↻",
                    (str(record.attempts),),
                    f"attempt {record.attempts} in progress",
                ),
            )
        case RecordedOutcome.ACKNOWLEDGED:
            if record.ack_latency_ms is None:
                figures: tuple[str, ...] = (str(record.attempts),)
                latency = ""
            else:
                figures = (str(record.attempts), duration(record.ack_latency_ms / 1000))
                latency = f", {figures[1]}"
            return (
                Status(
                    "delivered",
                    "✓✓",
                    figures,
                    f"delivered — acknowledged after {attempts}{latency}",
                ),
            )
        case RecordedOutcome.UNACKNOWLEDGED:
            return (
                Status(
                    "unacknowledged",
                    "⚠\ufe0e",
                    (str(record.attempts),),
                    (
                        f"unacknowledged after {attempts} — the platform cannot tell "
                        "whether it arrived"
                    ),
                ),
            )
        case RecordedOutcome.DROPPED:
            return (
                Status(
                    "dropped",
                    "⊘",
                    explanation="never reached the air — the scheduler dropped it",
                ),
            )
        case _:
            return (Status("unknown", "⁇", explanation=str(record.outcome)),)


def _route_note(page: Panel, contact: Contact) -> dict[str, object]:
    """Whether a message to this contact can go without flooding, and why not."""
    try:
        route = choose_route(
            page.state.pipeline.paths,
            contact,
            path_hash_size=page.state.messenger.path_hash_size,
            allow_flood=False,
        )
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
    identity: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Compose, refuse or submit — and never truncate (design D11).

    Every refusal below hands the author's text back. The room server truncates
    an inbound post because there is no way to say no to a radio; there is every
    way to say no to a form, and this is the side of the protocol where that is
    true.
    """
    # The composer's own selection is what this message is sent as; the path's
    # identity is the conversation it was composed in. Sending as another
    # identity belongs in that identity's conversation, so the redirect below
    # lands there (design D5).
    chosen_key = identity or entity_key
    entity = _entity(page, chosen_key)
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
    return RedirectResponse(f"/chat/{chosen_key}/{peer_key}", status_code=SEE_OTHER)


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
            choose_route(
                page.state.pipeline.paths,
                contact,
                path_hash_size=page.state.messenger.path_hash_size,
                allow_flood=False,
            )
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
