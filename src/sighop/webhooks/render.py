"""Events as request bodies (design D2). Pure functions, golden-tested.

`json` is sighop's own documented event, schema version 1: fields may be added
within a schema version, never removed or repurposed. `discord` is a
Discord webhook message, and every advert-derived string in it was written by
whoever sent the advert — so markdown is escaped and mentions are disabled.

The two formats do not carry the same fields. The Discord message shows an
advertised position as a maps link a reader can click, and shows no SNR: link
quality of a first sighting is for a machine, and stays in `json`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata

from sighop.webhooks.config import WebhookFormat
from sighop.webhooks.events import PathHop, Position, WebhookEvent
from sighop.webhooks.triggers import Trigger

JSON_SCHEMA_VERSION = 1
CONTENT_TYPE = "application/json"
DISCORD_USERNAME = "sighop"
DISCORD_COLOURS: dict[Trigger, int] = {
    Trigger.NEW_REPEATER: 0x3B82F6,
    Trigger.NEW_COMPANION: 0x22C55E,
}
UNNAMED = "unnamed node"
HEARD_DIRECTLY = "heard directly"
NO_POSITION = "not advertised"
DISCORD_FIELD_LIMIT = 1024
PATH_SEPARATOR = " → "
ELLIPSIS = "…"
MAPS_URL = "https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
COORDINATE_DECIMALS = 6
"""Decimals shown for a coordinate: the advert's own precision, 1e-6° of a
degree (`payloads.GEO_SCALE`). Fixed-point, so a small value cannot reach a URL
as `5.9e-05`."""

_DISCORD_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#\-<:@])")


def _instant(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _coordinate(value: float) -> str:
    return f"{value:.{COORDINATE_DECIMALS}f}"


def maps_url(position: Position) -> str:
    """The advertised coordinates as a Google Maps link, shared by both formats."""
    return MAPS_URL.format(
        latitude=_coordinate(position.latitude), longitude=_coordinate(position.longitude)
    )


def render(event: WebhookEvent, format: WebhookFormat | str) -> bytes:
    if WebhookFormat(format) is WebhookFormat.DISCORD:
        return render_discord(event)
    return render_json(event)


def render_json(event: WebhookEvent) -> bytes:
    body = {
        "schema": JSON_SCHEMA_VERSION,
        "event": event.trigger.value,
        "event_id": event.event_id,
        "occurred_at": _instant(event.occurred_at),
        "test": event.test,
        "node": {
            "public_key": event.public_key.hex(),
            "node_hash": f"{event.node_hash:02x}",
            "hash": event.sized_hash,
            "hash_size": event.hash_size,
            "name": event.name,
            "node_type": event.node_type_name,
            "position": None
            if event.position is None
            else {
                "latitude": event.position.latitude,
                "longitude": event.position.longitude,
                "map_url": maps_url(event.position),
            },
        },
        "reception": {
            "hop_count": event.hop_count,
            "snr_db": event.snr_db,
            "rssi_dbm": event.rssi_dbm,
            "received_at": _instant(event.received_at),
            "path": [
                {"hash": hop.hash.hex(), "name": hop.name, "matches": hop.matches}
                for hop in event.path
            ],
        },
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def escape_discord(text: str) -> str:
    """Advert text shown literally: controls flattened, markdown and mentions escaped."""
    flattened = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    return _DISCORD_MARKDOWN.sub(r"\\\1", flattened)


def _discord_hop(hop: PathHop) -> str:
    """A hop's hash as inline code, then its label. Contact names are advert
    content and escaped; our fixed labels and key prefixes are not."""
    label = escape_discord(hop.name) if hop.matches == 1 and hop.name is not None else hop.label
    return f"`{hop.hash.hex()}` {label}"


def discord_path(path: tuple[PathHop, ...]) -> str:
    """The path in travel order, cut on a hop boundary to fit a Discord field."""
    if not path:
        return HEARD_DIRECTLY
    hops = [_discord_hop(hop) for hop in path]
    whole = PATH_SEPARATOR.join(hops)
    if len(whole) <= DISCORD_FIELD_LIMIT:
        return whole
    shown = ELLIPSIS
    for count in range(1, len(hops)):
        candidate = PATH_SEPARATOR.join([*hops[:count], ELLIPSIS])
        if len(candidate) > DISCORD_FIELD_LIMIT:
            break
        shown = candidate
    return shown


def discord_location(position: Position | None) -> str:
    """The coordinates as a masked maps link, or that none were advertised.

    Coordinates are numbers sighop formats, never advert text, so they are not
    escaped; the masked-link syntax around them is ours.
    """
    if position is None:
        return NO_POSITION
    label = f"{_coordinate(position.latitude)}, {_coordinate(position.longitude)}"
    return f"[{label}]({maps_url(position)})"


def render_discord(event: WebhookEvent) -> bytes:
    marker = "[test] " if event.test else ""
    public_key = event.public_key.hex()
    node_hash = event.sized_hash
    if event.name is None:
        shown_name = UNNAMED
        description = (
            f"An unnamed node, identified by node hash {node_hash} and key {public_key}"
        )
    else:
        shown_name = escape_discord(event.name)
        description = shown_name
    if event.test:
        description = f"{description}\nThis is a test message from sighop, not a real sighting."
    fields = [
        {"name": "Name", "value": shown_name, "inline": True},
        {"name": "Type", "value": event.node_type_name or "unknown", "inline": True},
        {"name": "Node hash", "value": node_hash, "inline": True},
        {
            "name": "Hops",
            "value": "unknown" if event.hop_count is None else str(event.hop_count),
            "inline": True,
        },
        {"name": "Location", "value": discord_location(event.position), "inline": True},
        {"name": "Public key", "value": f"`{public_key}`", "inline": False},
        {"name": "Path", "value": discord_path(event.path), "inline": False},
    ]
    body = {
        "username": DISCORD_USERNAME,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"{marker}{event.trigger.heading}",
                "description": description,
                "color": DISCORD_COLOURS[event.trigger],
                "fields": fields,
                "timestamp": _instant(event.occurred_at),
                "footer": {"text": f"sighop event {event.event_id}"},
            }
        ],
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
