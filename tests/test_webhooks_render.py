"""Events and their bodies (webhook-notifications tasks 4.1, 4.2)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from sighop.net.contacts import ContactStore
from sighop.protocol.crypto import VerifiedAdvert, sign_advert, verify_advert
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.payloads import NodeType, build_appdata
from sighop.webhooks.events import (
    Position,
    WebhookEvent,
    event_from_observation,
    sample_event,
)
from sighop.webhooks.render import escape_discord, render, render_discord, render_json
from sighop.webhooks.triggers import Trigger
from tests.botfixtures import advert_record

NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)
SEED = bytes(range(32))


def _advert(*, name: str | None, latitude: int | None = None, longitude: int | None = None) -> VerifiedAdvert:
    identity = LocalIdentity.from_seed(SEED)
    appdata = build_appdata(NodeType.REPEATER, name=name, latitude=latitude, longitude=longitude)
    verification = verify_advert(sign_advert(identity, 1_700_000_000, appdata))
    assert isinstance(verification, VerifiedAdvert)
    return verification


async def _event_for(verified: VerifiedAdvert) -> WebhookEvent:
    seen: list[WebhookEvent] = []
    store = ContactStore()
    store.add_observation_listener(
        lambda observation, record: seen.append(
            event_from_observation(observation, record, Trigger.NEW_REPEATER, NOW)
        )
    )
    await store.handle(advert_record(verified, hop_count=2, snr_db=-4.25, at=NOW))
    assert len(seen) == 1
    return seen[0]


# --- 4.1 Events --------------------------------------------------------------


async def test_an_event_from_a_verified_advert_with_a_location() -> None:
    event = await _event_for(_advert(name="Hilltop", latitude=60_169_856, longitude=24_938_379))

    assert event.trigger is Trigger.NEW_REPEATER
    assert event.public_key == LocalIdentity.from_seed(SEED).public_key
    assert event.name == "Hilltop"
    assert event.node_type is NodeType.REPEATER
    assert event.position == Position(latitude=60.169856, longitude=24.938379)
    assert event.hop_count == 2
    assert event.snr_db == -4.25
    assert event.rssi_dbm == -80
    assert event.received_at == NOW
    assert not event.test


async def test_an_event_from_an_advert_without_a_location_or_name() -> None:
    event = await _event_for(_advert(name=None))
    assert event.position is None
    assert event.name is None


def test_a_sample_event_is_marked_as_a_test() -> None:
    event = sample_event(Trigger.NEW_COMPANION, NOW)
    assert event.test
    assert event.node_type is NodeType.CHAT
    assert event.event_id != sample_event(Trigger.NEW_COMPANION, NOW).event_id


# --- 4.2 JSON ----------------------------------------------------------------


def _fixed(**overrides: object) -> WebhookEvent:
    fields: dict[str, object] = {
        "event_id": "8a1f0c52-0000-4000-8000-000000000001",
        "trigger": Trigger.NEW_REPEATER,
        "occurred_at": NOW,
        "public_key": bytes.fromhex("ab" + "01" * 31),
        "name": "Hilltop",
        "node_type": NodeType.REPEATER,
        "position": Position(latitude=60.169856, longitude=24.938379),
        "hop_count": 2,
        "snr_db": -4.25,
        "rssi_dbm": -101,
        "received_at": NOW,
    }
    fields.update(overrides)
    return WebhookEvent(**fields)  # type: ignore[arg-type]


def test_the_json_body_is_the_documented_schema_1_event() -> None:
    assert json.loads(render_json(_fixed())) == {
        "schema": 1,
        "event": "new_repeater",
        "event_id": "8a1f0c52-0000-4000-8000-000000000001",
        "occurred_at": "2026-09-13T12:00:00Z",
        "test": False,
        "node": {
            "public_key": "ab" + "01" * 31,
            "node_hash": "ab",
            "name": "Hilltop",
            "node_type": "repeater",
            "position": {"latitude": 60.169856, "longitude": 24.938379},
        },
        "reception": {
            "hop_count": 2,
            "snr_db": -4.25,
            "rssi_dbm": -101,
            "received_at": "2026-09-13T12:00:00Z",
        },
    }


def test_unknown_values_are_null_in_json() -> None:
    body = json.loads(
        render_json(_fixed(name=None, position=None, hop_count=None, snr_db=None, rssi_dbm=None))
    )
    assert body["node"]["name"] is None
    assert body["node"]["position"] is None
    assert body["reception"] == {
        "hop_count": None,
        "snr_db": None,
        "rssi_dbm": None,
        "received_at": "2026-09-13T12:00:00Z",
    }


def test_rendering_the_same_event_twice_gives_the_same_event_id() -> None:
    event = _fixed()
    assert json.loads(render(event, "json"))["event_id"] == json.loads(render(event, "json"))["event_id"]


# --- 4.2 Discord -------------------------------------------------------------


def _discord(event: WebhookEvent) -> dict:
    return json.loads(render_discord(event))


def test_the_discord_body_is_one_embed_with_mentions_disabled() -> None:
    body = _discord(_fixed())
    assert body["allowed_mentions"] == {"parse": []}
    assert body["username"] == "sighop"
    assert "content" not in body
    [embed] = body["embeds"]
    assert embed["title"] == "New repeater heard"
    assert {field["name"]: field["value"] for field in embed["fields"]} == {
        "Name": "Hilltop",
        "Type": "repeater",
        "Node hash": "ab",
        "Public key": "ab01010101010101…",
        "Hops": "2",
        "SNR": "-4.2 dB",
    }


def test_a_mention_in_a_name_notifies_nobody() -> None:
    body = _discord(_fixed(trigger=Trigger.NEW_COMPANION, node_type=NodeType.CHAT, name="@everyone"))
    assert body["allowed_mentions"] == {"parse": []}
    rendered = json.dumps(body)
    assert "@everyone" not in rendered.replace("\\\\@everyone", "")
    assert body["embeds"][0]["fields"][0]["value"] == "\\@everyone"


@pytest.mark.parametrize(
    ("name", "escaped"),
    [
        ("**bold**", "\\*\\*bold\\*\\*"),
        ("snake_case", "snake\\_case"),
        ("[x](http://y)", "\\[x\\]\\(http\\://y\\)"),
        ("line\nbreak", "line break"),
    ],
)
def test_markdown_in_a_name_is_shown_literally(name: str, escaped: str) -> None:
    assert escape_discord(name) == escaped
    body = _discord(_fixed(name=name))
    assert body["embeds"][0]["fields"][0]["value"] == escaped
    assert body["embeds"][0]["description"] == escaped


def test_an_unnamed_node_is_identified_by_hash_and_key_prefix() -> None:
    embed = _discord(_fixed(name=None))["embeds"][0]
    assert "unnamed" in embed["description"].lower()
    assert "ab" in embed["description"] and "ab01010101010101" in embed["description"]
    assert embed["fields"][0]["value"] == "unnamed node"


def test_a_sample_is_marked_as_a_test_in_both_formats() -> None:
    event = sample_event(Trigger.NEW_REPEATER, NOW)
    assert _discord(event)["embeds"][0]["title"].startswith("[test] ")
    assert json.loads(render_json(event))["test"] is True
