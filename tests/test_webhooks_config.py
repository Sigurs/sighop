"""The stored-configuration rules (webhook-notifications task 3.2)."""

from __future__ import annotations

import pytest

from sighop.webhooks.config import (
    WebhookConfigError,
    WebhookFormat,
    is_plaintext_http,
    parse_format,
    parse_max_hops,
    parse_name,
    parse_triggers,
    parse_url,
)
from sighop.webhooks.triggers import Trigger

SECRET_PATH = "/api/webhooks/123/s3cr3t-token"


@pytest.mark.parametrize(
    ("url", "host"),
    [
        (f"https://discord.com{SECRET_PATH}?wait=true", "https://discord.com"),
        ("HTTPS://Example.COM:8443/x", "https://example.com:8443"),
        ("http://user:pw@10.0.0.5:5678/webhook/abc", "http://10.0.0.5:5678"),
        ("http://[::1]:8080/hook", "http://[::1]:8080"),
        ("  https://h.example/p  \n", "https://h.example"),
    ],
)
def test_a_url_keeps_scheme_and_host_for_display(url: str, host: str) -> None:
    parsed = parse_url(url)
    assert parsed.url == url.strip()
    assert parsed.url_host == host


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "http or https"),
        ("ftp://host/", "http or https"),
        ("discord.com/api/webhooks/1/x", "http or https"),
        ("https://", "host"),
        ("https:///path-only", "host"),
        ("", "empty"),
        ("https://h.example/a b", "whitespace"),
        ("https://h.example:99999/", "port"),
    ],
)
def test_unusable_urls_are_refused_without_echoing_them(url: str, reason: str) -> None:
    with pytest.raises(WebhookConfigError) as excinfo:
        parse_url(url)
    message = str(excinfo.value)
    assert reason in message
    if url.strip():
        assert url.strip() not in message


def test_plaintext_http_is_detected() -> None:
    assert is_plaintext_http("http://h/x")
    assert parse_url("http://h/x").is_plaintext_http
    assert not is_plaintext_http("https://h/x")


def test_triggers_are_deduplicated_in_order() -> None:
    assert parse_triggers(["new_companion", "new_repeater", "new_companion"]) == (
        Trigger.NEW_COMPANION,
        Trigger.NEW_REPEATER,
    )


def test_an_unknown_trigger_lists_the_known_ones() -> None:
    with pytest.raises(WebhookConfigError) as excinfo:
        parse_triggers(["new_repeater", "new_chatter"])
    message = str(excinfo.value)
    assert "new_chatter" in message
    assert "new_repeater" in message and "new_companion" in message


def test_an_empty_trigger_set_is_refused() -> None:
    with pytest.raises(WebhookConfigError, match="at least one trigger"):
        parse_triggers([])


def test_formats() -> None:
    assert parse_format("discord") is WebhookFormat.DISCORD
    assert parse_format(" json ") is WebhookFormat.JSON
    with pytest.raises(WebhookConfigError, match="json, discord"):
        parse_format("slack")


@pytest.mark.parametrize(("given", "expected"), [(None, None), ("", None), (0, 0), ("3", 3)])
def test_hop_limits(given: int | str | None, expected: int | None) -> None:
    assert parse_max_hops(given) == expected


@pytest.mark.parametrize("given", [-1, "-2", "two"])
def test_bad_hop_limits_are_refused(given: int | str) -> None:
    with pytest.raises(WebhookConfigError):
        parse_max_hops(given)


@pytest.mark.parametrize(
    ("given", "reason"), [("", "empty"), ("a b", "whitespace"), ("x" * 65, "at most")]
)
def test_bad_names_are_refused(given: str, reason: str) -> None:
    with pytest.raises(WebhookConfigError, match=reason):
        parse_name(given)
