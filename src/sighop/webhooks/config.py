"""The rules a stored webhook obeys, shared by the command line and the panel.

Every refusal is an exception carrying an operator-facing message, raised before
anything is written (webhook-notifications design D7). No message here repeats
the URL it refused: a webhook URL is a credential, and a refusal is printed to a
terminal and rendered into a page.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from sighop.webhooks.triggers import Trigger

MAX_WEBHOOK_NAME_LENGTH = 64
ALLOWED_SCHEMES = ("http", "https")


class WebhookFormat(StrEnum):
    JSON = "json"
    DISCORD = "discord"


class WebhookConfigError(ValueError):
    """A webhook setting that cannot be stored. Says which rule it broke."""


class WebhookExistsError(WebhookConfigError):
    """A webhook with this name exists."""


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    url: str
    url_host: str
    """`scheme://host[:port]` — what every surface shows instead of the URL."""

    @property
    def is_plaintext_http(self) -> bool:
        return is_plaintext_http(self.url)


def _known_triggers() -> str:
    return ", ".join(trigger.value for trigger in Trigger)


def _known_formats() -> str:
    return ", ".join(kind.value for kind in WebhookFormat)


def parse_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise WebhookConfigError("a webhook name cannot be empty")
    if len(name) > MAX_WEBHOOK_NAME_LENGTH:
        raise WebhookConfigError(
            f"a webhook name is at most {MAX_WEBHOOK_NAME_LENGTH} characters; this one "
            f"is {len(name)}"
        )
    for character in name:
        if character.isspace():
            raise WebhookConfigError("a webhook name cannot contain whitespace")
        if unicodedata.category(character).startswith("C"):
            raise WebhookConfigError(
                "a webhook name cannot contain control or unassigned characters "
                f"(found U+{ord(character):04X})"
            )
    return name


def parse_url(value: str) -> ParsedUrl:
    """An `http` or `https` URL with a host, and its displayable scheme and host."""
    url = value.strip()
    if not url:
        raise WebhookConfigError("a webhook URL cannot be empty")
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in url
    ):
        raise WebhookConfigError(
            "a webhook URL cannot contain whitespace or control characters"
        )
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        shown = f"{scheme!r}" if scheme else "no scheme"
        raise WebhookConfigError(
            f"a webhook URL must use http or https; this one has {shown}"
        )
    host = parts.hostname
    if not host:
        raise WebhookConfigError("a webhook URL must name a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise WebhookConfigError("a webhook URL's port is not a valid port number") from exc
    shown_host = f"[{host}]" if ":" in host else host
    url_host = f"{scheme}://{shown_host}" + ("" if port is None else f":{port}")
    return ParsedUrl(url=url, url_host=url_host)


def is_plaintext_http(url: str) -> bool:
    return urlsplit(url.strip()).scheme.lower() == "http"


PLAINTEXT_HTTP_WARNING = (
    "warning: this webhook uses plain http — the URL and every payload will cross "
    "the network unencrypted"
)


FIRST_RUN_BURST = (
    "note: a node whose contact table is empty announces every repeater and "
    "companion it hears as new, so a first run can send a burst within a day; "
    "--max-hops narrows it"
)


def parse_triggers(values: Iterable[str]) -> tuple[Trigger, ...]:
    """A non-empty set of known triggers, deduplicated, in the order given."""
    triggers: list[Trigger] = []
    for value in values:
        cleaned = value.strip()
        try:
            trigger = Trigger(cleaned)
        except ValueError:
            raise WebhookConfigError(
                f"unknown trigger {cleaned!r}; the triggers are {_known_triggers()}"
            ) from None
        if trigger not in triggers:
            triggers.append(trigger)
    if not triggers:
        raise WebhookConfigError(
            f"a webhook needs at least one trigger; the triggers are {_known_triggers()}"
        )
    return tuple(triggers)


def parse_format(value: str) -> WebhookFormat:
    try:
        return WebhookFormat(value.strip())
    except ValueError:
        raise WebhookConfigError(
            f"unknown format {value.strip()!r}; the formats are {_known_formats()}"
        ) from None


def parse_max_hops(value: int | str | None) -> int | None:
    """A hop limit of zero or more, or none. An empty string is none."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            value = int(cleaned)
        except ValueError:
            raise WebhookConfigError(
                f"a maximum hop count is a whole number; got {cleaned!r}"
            ) from None
    if isinstance(value, bool) or value < 0:
        raise WebhookConfigError(
            f"a maximum hop count cannot be negative; got {value}"
        )
    return value
