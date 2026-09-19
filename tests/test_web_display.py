"""The panel's display conventions (`web-display`).

A status is a glyph and its figures with its meaning on hover and read to a
screen reader; a public key is three bytes with the whole key behind a copy
control; a timestamp is compact UTC that `display.js` rewrites relative to now;
durations and counts read as a person writes them. Checked here against the
helpers and macros themselves, so every page that uses them inherits the rule.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

import sighop.web
from sighop.net.channels import ChannelMessageRecord, ChannelOutcome
from sighop.net.dm import DirectMessageRecord, RecordedOutcome
from sighop.web.render import (
    CLAIMED_MARK,
    UNVERIFIED_MARK,
    IdentityView,
    RouteView,
    Status,
    duration,
    install_display,
    iso_utc,
    plural,
    utc_short,
)
from sighop.web.routes.chat import _channel_state, _state

WEB_DIR = Path(sighop.web.__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

NOW = dt.datetime(2026, 9, 14, 17, 35, 56, 857462, tzinfo=dt.UTC)
KEY = bytes(range(32))


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    install_display(env)
    return env


def _macro(module: str, name: str, *args: object) -> str:
    template = _env().get_template(module)
    return str(getattr(template.module, name)(*args))


# --- Durations and counts ----------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (0.25, "250 ms"),
        (3.010, "3.0 s"),
        (59.0, "59.0 s"),
        (60.0, "1 m"),
        (90.0, "1 m 30 s"),
        (7200.0, "2 h"),
        (3900.0, "1 h 5 m"),
        (2 * 86400 + 3 * 3600, "2 d 3 h"),
    ],
)
def test_a_duration_is_in_the_largest_sensible_unit(seconds: float, shown: str) -> None:
    assert duration(seconds) == shown


def test_a_count_has_its_real_plural() -> None:
    assert plural(1, "attempt") == "1 attempt"
    assert plural(2, "attempt") == "2 attempts"
    assert plural(0, "hop") == "0 hops"
    assert plural(3, "reply", "replies") == "3 replies"


def test_timestamps_have_an_exact_and_a_compact_utc_form() -> None:
    assert iso_utc(NOW) == "2026-09-14T17:35:56.857462+00:00"
    assert utc_short(NOW) == "2026-09-14 17:35 UTC"
    # A naive stored time is UTC, and another zone is converted rather than relabelled.
    assert utc_short(NOW.replace(tzinfo=None)) == "2026-09-14 17:35 UTC"
    helsinki = NOW.astimezone(dt.timezone(dt.timedelta(hours=3)))
    assert iso_utc(helsinki) == iso_utc(NOW)


# --- The macros ----------------------------------------------------------------


def test_a_status_puts_its_meaning_on_hover_and_to_a_screen_reader() -> None:
    drawn = _macro(
        "_display.html",
        "status",
        Status("delivered", "✓✓", ("1", "3.0 s"), "delivered — acknowledged after 1 attempt"),
    )
    assert 'class="status status-delivered"' in drawn
    assert 'title="delivered — acknowledged after 1 attempt"' in drawn
    assert '<span class="status-glyph" aria-hidden="true">✓✓</span>' in drawn
    assert '<span class="status-figures" aria-hidden="true">1 · 3.0 s</span>' in drawn
    assert '<span class="visually-hidden">delivered — acknowledged after 1 attempt</span>' in drawn


def test_a_failure_keeps_its_reason_visible() -> None:
    (state,) = _channel_state(
        _channel(outcome=ChannelOutcome.NOT_TRANSMITTED, outcome_reason="the gate is closed")
    )
    drawn = _macro("_display.html", "status", state)
    assert '<span class="status-reason" aria-hidden="true">the gate is closed</span>' in drawn
    assert "not transmitted — the gate is closed; not retried" in drawn


def test_a_key_is_three_bytes_with_the_whole_key_copyable() -> None:
    full = KEY.hex()
    for value in (full, KEY):
        drawn = _macro("_display.html", "key", value)
        assert f'<code class="mono" title="{full}">{full[:6]}…</code>' in drawn
        assert f'data-copy="{full}"' in drawn
        # Never submits the form it sits in.
        assert '<button type="button" class="copy"' in drawn


def test_a_timestamp_carries_its_exact_value_and_a_utc_fallback() -> None:
    drawn = _macro("_display.html", "when", NOW)
    assert drawn == (
        '<time class="when" datetime="2026-09-14T17:35:56.857462+00:00" '
        'title="2026-09-14T17:35:56.857462+00:00">2026-09-14 17:35 UTC</time>'
    )
    assert _macro("_display.html", "when", None) == "—"
    assert _macro("_display.html", "when", None, "never") == "never"


# --- States, one glyph each ------------------------------------------------------


def _dm(
    outcome: RecordedOutcome,
    *,
    attempts: int = 0,
    latency: float | None = None,
    direction: str = "out",
) -> DirectMessageRecord:
    return DirectMessageRecord(
        entity_public_key=KEY,
        peer_public_key=KEY[::-1],
        direction=direction,
        text=b"hi",
        wire_timestamp=1,
        handled_at=NOW,
        ref="m",
        outcome=outcome,
        attempts=attempts,
        ack_latency_ms=latency,
    )


def _channel(**fields: object) -> ChannelMessageRecord:
    base: dict[str, object] = {
        "channel_id": 1,
        "direction": "out",
        "ref": "p",
        "text": b"hi",
        "wire_timestamp": 1,
        "handled_at": NOW,
        "outcome": ChannelOutcome.TRANSMITTED,
    }
    base.update(fields)
    return ChannelMessageRecord(**base)  # type: ignore[arg-type]


def test_an_acknowledged_message_shows_attempts_and_latency() -> None:
    (state,) = _state(_dm(RecordedOutcome.ACKNOWLEDGED, attempts=1, latency=3010.0))
    assert state.glyph == "✓✓"
    assert state.figures == ("1", "3.0 s")
    assert state.explanation == "delivered — acknowledged after 1 attempt, 3.0 s"


def test_every_direct_message_state_has_its_own_glyph() -> None:
    states = [
        _state(_dm(RecordedOutcome.IN_FLIGHT))[0],
        _state(_dm(RecordedOutcome.IN_FLIGHT, attempts=2))[0],
        _state(_dm(RecordedOutcome.ACKNOWLEDGED, attempts=1))[0],
        _state(_dm(RecordedOutcome.UNACKNOWLEDGED, attempts=4))[0],
        _state(_dm(RecordedOutcome.DROPPED))[0],
    ]
    assert len({state.glyph for state in states}) == len(states)
    assert states[1].figures == ("2",)
    assert states[3].explanation.startswith("unacknowledged after 4 attempts")


def test_channel_states_have_their_own_glyphs_and_numbers() -> None:
    (received,) = _channel_state(
        _channel(direction="in", outcome=ChannelOutcome.RECEIVED, hop_count=2)
    )
    assert (received.glyph, received.figures) == ("↓", ("2",))
    assert received.explanation == "received over 2 hops"

    (unknown_hops,) = _channel_state(_channel(direction="in", outcome=ChannelOutcome.RECEIVED))
    assert unknown_hops.figures == ("?",)

    transmitted, repeats = _channel_state(_channel(repeats_heard=2))
    assert transmitted.explanation == (
        "transmitted; no acknowledgement exists for channel messages"
    )
    assert (repeats.glyph, repeats.figures) == ("⟲", ("2",))
    assert repeats.explanation == "repeat heard 2 times — a repeater forwarded it"

    (quiet,) = _channel_state(_channel())
    assert "no repeat heard — which does not mean it was not received" in quiet.explanation

    glyphs = {
        _channel_state(_channel(outcome=outcome))[0].glyph
        for outcome in (
            ChannelOutcome.AWAITING,
            ChannelOutcome.TRANSMITTED,
            ChannelOutcome.NOT_TRANSMITTED,
        )
    }
    assert len(glyphs | {received.glyph, repeats.glyph}) == 5


# --- Routes ------------------------------------------------------------------------


def _route(path: str, hops: int, *, ambiguous: bool = False) -> RouteView:
    return RouteView(path=path, hop_count=hops, snr_db=None, confirmed_at=NOW, ambiguous=ambiguous)


def test_a_zero_hop_route_reads_direct() -> None:
    status = _route("", 0).status
    assert status.figures == ("0", "direct")
    assert status.explanation == "direct, zero hops"


def test_a_route_matched_by_hash_keeps_its_caveat() -> None:
    status = _route("bed0", 2, ambiguous=True).status
    assert status.kind == "route-ambiguous"
    assert status.figures == ("2", "bed0")
    assert status.explanation.startswith("2 hops via bed0; matched by node hash")


def test_a_one_hop_route_is_singular() -> None:
    assert _route("be", 1).text == "1 hop via be"


# --- Identity and claim marks ------------------------------------------------------


def test_a_short_key_is_three_bytes() -> None:
    view = IdentityView(public_key=KEY, name=None, verification="key_only")
    assert view.short_key == KEY.hex()[:6]


@pytest.mark.parametrize("verification", ["verified", "unverified", "key_only"])
def test_an_identity_mark_is_a_glyph_with_its_meaning_on_hover(verification: str) -> None:
    view = IdentityView(public_key=KEY, name="[redacted]", verification=verification)
    drawn = _macro("_identity.html", "identity", view, False)
    assert f'title="{view.marking_text}"' in drawn
    assert f'<span class="identity-mark" aria-hidden="true">{view.mark}</span>' in drawn
    # The word is for screen readers now, not for every row.
    assert 'class="identity-status visually-hidden"' in drawn


def test_a_claimed_name_has_its_own_glyph() -> None:
    assert CLAIMED_MARK != UNVERIFIED_MARK
    drawn = _macro("_claimed.html", "claimed", "alice")
    assert f'<span class="claimed-mark" aria-hidden="true">{CLAIMED_MARK}</span>' in drawn
    assert UNVERIFIED_MARK not in drawn
    assert "identity-verified" not in drawn
    assert '<span class="claimed-status visually-hidden">claimed, unverified</span>' in drawn


def test_the_legend_explains_every_mark_including_a_claim() -> None:
    legend = _macro("_identity.html", "verification_legend")
    for mark in ("✓", "✗", "?", CLAIMED_MARK):
        assert mark in legend


# --- Where the conventions are applied ---------------------------------------------


def test_the_display_script_is_first_party_and_on_every_page() -> None:
    script = STATIC_DIR / "display.js"
    assert script.is_file()
    source = script.read_text()
    assert "htmx:afterSwap" in source and "time.when" in source
    assert "navigator.clipboard" in source and "isSecureContext" in source
    base = (TEMPLATE_DIR / "base.html").read_text()
    assert '<script src="/static/display.js" defer></script>' in base


def test_no_template_draws_a_raw_iso_timestamp() -> None:
    offenders = [
        path.name for path in TEMPLATE_DIR.rglob("*.html") if ".isoformat()" in path.read_text()
    ]
    assert not offenders


def test_a_revealed_private_key_is_never_abbreviated() -> None:
    source = (TEMPLATE_DIR / "admin" / "revealed.html").read_text()
    assert re.search(r'<p class="mono revealed">\{\{ private_key \}\}</p>', source)
