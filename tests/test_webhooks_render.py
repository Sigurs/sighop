"""Events and their bodies (webhook-notifications tasks 4.1, 4.2)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from sighop.net.contacts import Contact, ContactStore
from sighop.protocol.crypto import VerifiedAdvert, sign_advert, verify_advert
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.payloads import NodeType, WireText, build_appdata
from sighop.webhooks.events import (
    PathHop,
    Position,
    WebhookEvent,
    event_from_observation,
    resolve_hop,
    sample_event,
)
from sighop.webhooks.render import (
    DISCORD_FIELD_LIMIT,
    NO_POSITION,
    escape_discord,
    maps_url,
    render,
    render_discord,
    render_json,
)
from sighop.webhooks.triggers import Trigger
from tests.botfixtures import advert_record

NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)
SEED = bytes(range(32))


def _advert(
    *, name: str | None, latitude: int | None = None, longitude: int | None = None
) -> VerifiedAdvert:
    identity = LocalIdentity.from_seed(SEED)
    appdata = build_appdata(NodeType.REPEATER, name=name, latitude=latitude, longitude=longitude)
    verification = verify_advert(sign_advert(identity, 1_700_000_000, appdata))
    assert isinstance(verification, VerifiedAdvert)
    return verification


def _contact(key_hex: str, name: str | None = None) -> Contact:
    return Contact(
        public_key=bytes.fromhex(key_hex.ljust(64, "0")),
        name=None if name is None else WireText.from_bytes(name.encode()),
    )


async def _event_for(
    verified: VerifiedAdvert,
    *,
    hop_count: int = 2,
    hash_size: int = 1,
    path: bytes | None = None,
    known: tuple[Contact, ...] = (),
    resolve: bool = True,
) -> WebhookEvent:
    """An event raised through a real store; `known` contacts are held before the advert."""
    seen: list[WebhookEvent] = []
    store = ContactStore()
    store.restore(known)
    lookup = store if resolve else None
    store.add_observation_listener(
        lambda observation, record: seen.append(
            event_from_observation(observation, record, Trigger.NEW_REPEATER, NOW, lookup)
        )
    )
    await store.handle(
        advert_record(
            verified, hop_count=hop_count, snr_db=-4.25, at=NOW, hash_size=hash_size, path=path
        )
    )
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
    assert event.hash_size == 2
    assert event.hop_count == 2
    assert event.position == Position(latitude=59.329460, longitude=18.068580)
    assert event.path == (
        PathHop(hash=bytes.fromhex("c3d4"), name="dev-hop", matches=1),
        PathHop(hash=bytes.fromhex("e5f6"), name=None, matches=0),
    )
    assert [hop.label for hop in event.path] == ["dev-hop", "<unknown>"]


@pytest.mark.parametrize(
    ("hop", "label"),
    [
        (PathHop(hash=b"\xc3", name="Hilltop", matches=1), "Hilltop"),
        (PathHop(hash=b"\xc3", name=None, matches=1, key_prefix="c3d4e5f60708"), "c3d4e5f60708"),
        (PathHop(hash=b"\xc3", name=None, matches=0), "<unknown>"),
        (PathHop(hash=b"\xc3", name=None, matches=2), "<ambiguous>"),
    ],
)
def test_a_hop_label(hop: PathHop, label: str) -> None:
    assert hop.label == label


@pytest.mark.parametrize(("hash_size", "sized"), [(1, "a1"), (2, "a1b2"), (3, "a1b2c3")])
def test_the_node_hash_at_the_size_it_was_heard(hash_size: int, sized: str) -> None:
    event = _fixed(public_key=bytes.fromhex("a1b2c3".ljust(64, "0")), hash_size=hash_size)
    assert event.sized_hash == sized
    assert event.node_hash == 0xA1


async def test_a_two_hop_path_with_one_known_repeater() -> None:
    event = await _event_for(
        _advert(name="Wanderer"),
        hash_size=2,
        path=bytes.fromhex("c3d4e5f6"),
        known=(_contact("c3d4", "Hilltop"), _contact("e5aa", "Elsewhere")),
    )
    assert event.hash_size == 2
    assert event.hop_count == 2
    assert event.path == (
        PathHop(hash=bytes.fromhex("c3d4"), name="Hilltop", matches=1),
        PathHop(hash=bytes.fromhex("e5f6"), name=None, matches=0),
    )
    assert [hop.label for hop in event.path] == ["Hilltop", "<unknown>"]


async def test_a_colliding_1_byte_hop_is_ambiguous() -> None:
    event = await _event_for(
        _advert(name="Wanderer"),
        path=bytes.fromhex("7a"),
        known=(_contact("7a01", "one"), _contact("7a02", "two")),
    )
    [hop] = event.path
    assert hop == PathHop(hash=b"\x7a", name=None, matches=2)
    assert hop.label == "<ambiguous>"


async def test_the_advertiser_does_not_count_as_a_match_for_its_own_path() -> None:
    advertiser_byte = LocalIdentity.from_seed(SEED).public_key[:1]
    event = await _event_for(
        _advert(name="Wanderer"),
        path=advertiser_byte,
        known=(_contact(advertiser_byte.hex() + "ff", "Hilltop"),),
    )
    [hop] = event.path
    assert (hop.name, hop.matches) == ("Hilltop", 1)


def test_without_an_advertiser_every_matching_contact_counts() -> None:
    store = ContactStore()
    store.restore((_contact("c3d4", "Hilltop"),))
    hop = resolve_hop(bytes.fromhex("c3"), store)
    assert (hop.name, hop.matches) == ("Hilltop", 1)


async def test_a_single_unnamed_match_is_shown_by_key_prefix() -> None:
    event = await _event_for(
        _advert(name="Wanderer"), path=bytes.fromhex("c3"), known=(_contact("c3d4e5f60708"),)
    )
    [hop] = event.path
    assert hop.key_prefix == "c3d4e5f60708"
    assert hop.label == "c3d4e5f60708"


async def test_a_flood_advert_heard_directly_keeps_its_hash_size() -> None:
    event = await _event_for(_advert(name="Wanderer"), hop_count=0, hash_size=2)
    assert event.hash_size == 2
    assert event.path == ()
    assert event.sized_hash == LocalIdentity.from_seed(SEED).public_key[:2].hex()


async def test_without_a_lookup_every_hop_is_unknown() -> None:
    event = await _event_for(
        _advert(name="Wanderer"),
        path=bytes.fromhex("c3d4"),
        known=(_contact("c3", "Hilltop"), _contact("d4", "Valley")),
        resolve=False,
    )
    assert [hop.label for hop in event.path] == ["<unknown>", "<unknown>"]
    assert all(hop.matches == 0 for hop in event.path)


# --- 3.4 Maps link -----------------------------------------------------------


@pytest.mark.parametrize(
    ("position", "query"),
    [
        (Position(latitude=60.169856, longitude=24.938379), "60.169856,24.938379"),
        (Position(latitude=-33.868820, longitude=-151.209290), "-33.868820,-151.209290"),
        (Position(latitude=51.0, longitude=0.0), "51.000000,0.000000"),
        (Position(latitude=0.000059, longitude=0.000001), "0.000059,0.000001"),
    ],
)
def test_the_maps_url_carries_the_coordinates_at_advert_precision(
    position: Position, query: str
) -> None:
    assert maps_url(position) == f"https://www.google.com/maps/search/?api=1&query={query}"


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
        "hash_size": 2,
        "path": (
            PathHop(hash=bytes.fromhex("c3d4"), name="Relay", matches=1),
            PathHop(hash=bytes.fromhex("e5f6"), name=None, matches=0),
        ),
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
            "hash": "ab01",
            "hash_size": 2,
            "name": "Hilltop",
            "node_type": "repeater",
            "position": {
                "latitude": 60.169856,
                "longitude": 24.938379,
                "map_url": "https://www.google.com/maps/search/?api=1&query=60.169856,24.938379",
            },
        },
        "reception": {
            "hop_count": 2,
            "snr_db": -4.25,
            "rssi_dbm": -101,
            "received_at": "2026-09-13T12:00:00Z",
            "path": [
                {"hash": "c3d4", "name": "Relay", "matches": 1},
                {"hash": "e5f6", "name": None, "matches": 0},
            ],
        },
    }


def test_a_multi_byte_event_keeps_node_hash_at_one_byte() -> None:
    node = json.loads(render_json(_fixed(hash_size=3)))["node"]
    assert node["node_hash"] == "ab"
    assert node["hash"] == "ab0101"
    assert node["hash_size"] == 3


def test_a_zero_hop_reception_has_an_empty_path_in_json() -> None:
    body = json.loads(render_json(_fixed(hop_count=0, hash_size=1, path=())))
    assert body["reception"]["path"] == []
    assert body["node"]["hash"] == "ab"


def test_unknown_values_are_null_in_json() -> None:
    body = json.loads(
        render_json(
            _fixed(name=None, position=None, hop_count=None, snr_db=None, rssi_dbm=None, path=())
        )
    )
    assert body["node"]["name"] is None
    assert body["node"]["position"] is None  # no position, so no map URL either
    assert body["reception"] == {
        "hop_count": None,
        "snr_db": None,
        "rssi_dbm": None,
        "received_at": "2026-09-13T12:00:00Z",
        "path": [],
    }


def test_an_unlocated_node_keeps_its_snr_in_json() -> None:
    """SNR left the Discord message; schema 1 keeps it."""
    body = json.loads(render_json(_fixed(position=None)))
    assert body["node"]["position"] is None
    assert body["reception"]["snr_db"] == -4.25


def test_rendering_the_same_event_twice_gives_the_same_event_id() -> None:
    event = _fixed()
    assert (
        json.loads(render(event, "json"))["event_id"]
        == json.loads(render(event, "json"))["event_id"]
    )


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
    assert [(field["name"], field["value"], field["inline"]) for field in embed["fields"]] == [
        ("Name", "Hilltop", True),
        ("Type", "repeater", True),
        ("Node hash", "ab01", True),
        ("Hops", "2", True),
        (
            "Location",
            "[60.169856, 24.938379]"
            "(https://www.google.com/maps/search/?api=1&query=60.169856,24.938379)",
            True,
        ),
        ("Public key", "`ab" + "01" * 31 + "`", False),
        ("Path", "`c3d4` Relay → `e5f6` <unknown>", False),
    ]
    assert "SNR" not in [field["name"] for field in embed["fields"]]


def test_a_mention_in_a_name_notifies_nobody() -> None:
    body = _discord(
        _fixed(trigger=Trigger.NEW_COMPANION, node_type=NodeType.CHAT, name="@everyone")
    )
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
    assert "node hash ab01 " in embed["description"]
    assert ("ab" + "01" * 31) in embed["description"]
    assert "…" not in embed["description"]
    assert embed["fields"][0]["value"] == "unnamed node"


def _field(event: WebhookEvent, name: str) -> str:
    [value] = [
        field["value"] for field in _discord(event)["embeds"][0]["fields"] if field["name"] == name
    ]
    return value


def test_a_located_node_links_its_coordinates_to_a_map() -> None:
    assert _field(_fixed(), "Location") == (
        "[60.169856, 24.938379]"
        "(https://www.google.com/maps/search/?api=1&query=60.169856,24.938379)"
    )


def test_a_node_with_no_advertised_position_says_so() -> None:
    assert _field(_fixed(position=None), "Location") == NO_POSITION


def test_negative_coordinates_keep_their_sign_in_the_link() -> None:
    position = Position(latitude=-33.868820, longitude=-151.209290)
    assert _field(_fixed(position=position), "Location") == (
        "[-33.868820, -151.209290]"
        "(https://www.google.com/maps/search/?api=1&query=-33.868820,-151.209290)"
    )


def test_the_snr_is_not_in_the_discord_message() -> None:
    """It stays in `json`: link quality of a first sighting is for a machine."""
    body = _discord(_fixed())
    assert "SNR" not in [field["name"] for field in body["embeds"][0]["fields"]]
    assert "dB" not in json.dumps(body)


def _path_field(event: WebhookEvent) -> str:
    return _field(event, "Path")


def test_a_contact_name_in_the_path_is_shown_literally() -> None:
    path = (PathHop(hash=b"\xc3", name="**x**", matches=1),)
    assert _path_field(_fixed(path=path)) == "`c3` \\*\\*x\\*\\*"


def test_a_mention_in_a_path_name_notifies_nobody() -> None:
    body = _discord(_fixed(path=(PathHop(hash=b"\xc3", name="@everyone", matches=1),)))
    assert body["allowed_mentions"] == {"parse": []}
    assert body["embeds"][0]["fields"][-1]["value"] == "`c3` \\@everyone"


def test_an_empty_path_is_heard_directly() -> None:
    assert _path_field(_fixed(hop_count=0, path=())) == "heard directly"


def test_an_ambiguous_or_unnamed_hop_is_labelled_without_escaping() -> None:
    path = (
        PathHop(hash=b"\x7a", name=None, matches=2),
        PathHop(hash=b"\xc3", name=None, matches=1, key_prefix="c3d4e5f60708"),
    )
    assert _path_field(_fixed(path=path)) == "`7a` <ambiguous> → `c3` c3d4e5f60708"


def test_a_very_long_path_is_cut_on_a_hop_boundary() -> None:
    long_name = "n" * 32
    path = tuple(
        PathHop(hash=bytes([i, i]), name=f"{long_name}{i:02d}", matches=1) for i in range(64)
    )
    value = _path_field(_fixed(hop_count=64, path=path))
    assert len(value) <= DISCORD_FIELD_LIMIT
    assert value.endswith(" → …")
    hops = value.removesuffix(" → …").split(" → ")
    assert 0 < len(hops) < 64
    assert hops == [f"`{i:02x}{i:02x}` {long_name}{i:02d}" for i in range(len(hops))]


def test_a_sample_is_marked_as_a_test_in_both_formats() -> None:
    event = sample_event(Trigger.NEW_REPEATER, NOW)
    embed = _discord(event)["embeds"][0]
    assert embed["title"].startswith("[test] ")
    assert _path_field(event) == "`c3d4` dev\\-hop → `e5f6` <unknown>"
    assert _field(event, "Location") == (
        "[59.329460, 18.068580]"
        "(https://www.google.com/maps/search/?api=1&query=59.329460,18.068580)"
    )
    body = json.loads(render_json(event))
    assert body["test"] is True
    assert body["node"]["hash_size"] == 2
    assert body["node"]["position"]["map_url"].endswith("query=59.329460,18.068580")
    assert body["reception"]["path"] == [
        {"hash": "c3d4", "name": "dev-hop", "matches": 1},
        {"hash": "e5f6", "name": None, "matches": 0},
    ]
