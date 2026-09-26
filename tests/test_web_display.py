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
from sighop.net.adverts import EntityStub
from sighop.net.channels import ChannelMessageRecord, ChannelOutcome
from sighop.net.dm import DirectMessageRecord, RecordedOutcome
from sighop.protocol.identity import generate_identity
from sighop.web.render import (
    CLAIMED_MARK,
    UNVERIFIED_MARK,
    VERIFIED_MARK,
    IdentityView,
    RouteView,
    Status,
    default_identity,
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
        # The manual-copy holder ships empty and hidden: nothing to see, and
        # nothing occupying width, until there is no other way to copy.
        assert '<span class="key-full mono" hidden></span>' in drawn


def test_the_manual_copy_holder_takes_no_space_in_the_row() -> None:
    """Out of the flow, so revealing it cannot move the abbreviation beside it
    or widen the column the key sits in (`web-display`)."""
    css = (STATIC_DIR / "panel.css").read_text()
    holder = re.search(r"\.key-full\s*\{([^}]*)\}", css)
    assert holder, "the manual-copy holder has no rule of its own"
    assert "position: absolute" in holder.group(1)
    key_rule = re.search(r"^\.key\s*\{([^}]*)\}", css, re.MULTILINE)
    assert key_rule and "position: relative" in key_rule.group(1)


def test_copying_a_key_never_writes_over_the_abbreviation() -> None:
    """The bug this rule exists for: the old fallback put the full 64-character
    key in place of the three-byte abbreviation, and `window.isSecureContext` is
    false on every panel served over plain HTTP — so the fallback was the
    ordinary path, and every copy click widened the column for good."""
    source = (STATIC_DIR / "display.js").read_text()
    # The abbreviation lives in the `<code>`; no branch may assign to it.
    assert not re.search(r"""querySelector\(\s*["']code["']\s*\)""", source)
    assert not re.search(r"code\.(?:textContent|innerHTML|innerText)\s*=", source)
    # What is written to instead is the holder that occupies no space.
    assert re.search(r"""querySelector\(\s*["']\.key-full["']\s*\)""", source)


def test_a_key_is_copyable_without_a_secure_context() -> None:
    """Three tiers, in order: the clipboard API where the browser offers it, an
    off-screen textarea where it does not, and only then the key put up for
    copying by hand."""
    source = (STATIC_DIR / "display.js").read_text()
    assert "navigator.clipboard" in source and "isSecureContext" in source
    assert 'document.execCommand("copy")' in source
    assert 'createElement("textarea")' in source
    # The off-screen copy confirms exactly as the clipboard one does.
    assert re.search(r"if \(copyOffScreen\(full\)\) \{ confirm\(button\); return; \}", source)
    # And it is reached both when there is no clipboard and when writing fails.
    assert source.count("fallback(button, full);") == 2


def test_the_offered_key_is_withdrawn_once_the_operator_moves_on() -> None:
    source = (STATIC_DIR / "display.js").read_text()
    assert re.search(r"function withdraw\(\)", source)
    assert "holder.hidden = false" in source and "offered.hidden = true" in source
    # Escape, and a click anywhere that is not the offered key itself.
    assert re.search(r'event\.key === "Escape"', source)
    assert re.search(r"""closest\(["']\.key-full["']\)""", source)


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
    view = IdentityView(public_key=KEY, name="syn-harbor-repeater", verification=verification)
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


# --- The palette -------------------------------------------------------------------

# The surfaces text is drawn on, and the colours the palette gives to text.
# `--rule` and `--rule-soft` are hairlines and `--bg-track` is the empty part of
# a gauge; none of them carries text, so none is held to a reading contrast.
SURFACES = ("--bg", "--bg-raised", "--bg-sunken")
INK = (
    "--fg",
    "--fg-dim",
    "--accent",
    "--ok",
    "--warn",
    "--alarm",
    "--verified",
    "--unverified",
    "--key-only",
    "--rx",
    "--tx",
)
READABLE = 4.5
"""WCAG 2.1 AA for body text. The palette passes today; this is here for the day
a value is dimmed."""


def _palette() -> dict[str, str]:
    css = (STATIC_DIR / "panel.css").read_text()
    root = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    assert root, "the stylesheet has no :root palette"
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", root.group(1)))


def _luminance(colour: str) -> float:
    channels = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one: str, other: str) -> float:
    first, second = _luminance(one), _luminance(other)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def test_every_text_colour_is_readable_on_every_surface_it_lands_on() -> None:
    palette = _palette()
    for name in (*SURFACES, *INK):
        assert name in palette, f"{name} is not in the palette"

    worst = min(
        (_contrast(palette[ink], palette[surface]), ink, surface)
        for ink in INK
        for surface in SURFACES
    )
    ratio, ink, surface = worst
    assert ratio >= READABLE, f"{ink} on {surface} is {ratio:.2f}:1, under {READABLE}:1"


def test_the_palette_is_the_one_place_a_colour_is_decided() -> None:
    """A colour stated in a rule is a colour that will not follow the palette."""
    css = (STATIC_DIR / "panel.css").read_text()
    outside = re.sub(r":root\s*\{.*?\}", "", css, count=1, flags=re.DOTALL)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\([^)]*\)", outside)
    assert not literals, f"colours stated outside the palette: {literals}"


def test_nothing_is_told_apart_by_colour_alone() -> None:
    """Budget state, verification, a claim and direction each carry a glyph or a
    word as well as a hue (`web-display`, DESIGN.md §8's third bullet)."""
    verified = _macro("_identity.html", "identity", IdentityView(KEY, "a", "verified"), False)
    unverified = _macro("_identity.html", "identity", IdentityView(KEY, "a", "unverified"), False)
    assert VERIFIED_MARK in verified and UNVERIFIED_MARK in unverified
    assert VERIFIED_MARK != UNVERIFIED_MARK

    # The meter says its state in words beside the bar, not only in the bar.
    meter = (TEMPLATE_DIR / "_meter.html").read_text()
    assert "meter.state_text" in meter
    assert "meter.gate_text" in meter

    # Direction is a word in its own column before it is a colour on the row.
    messages = (TEMPLATE_DIR / "chat" / "_messages.html").read_text()
    assert 'class="message dir-' in messages
    assert "message.inbound" in messages


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


# --- The identity an operator chats as --------------------------------------


def _stub(entity_id: str, name: str) -> EntityStub:
    return EntityStub(entity_id=entity_id, name=name, identity=generate_identity())


def test_the_default_resolves_to_the_identity_this_run_holds() -> None:
    held = [_stub("one", "skogen"), _stub("two", "fjell")]
    assert default_identity(held, "two") is held[1]


def test_a_default_this_run_does_not_hold_resolves_to_nothing() -> None:
    """Never a substitute: composing as an identity the operator did not choose
    is the one thing the preference must not cause."""
    held = [_stub("one", "skogen"), _stub("two", "fjell")]
    assert default_identity(held, "three") is None


def test_no_default_set_resolves_to_nothing() -> None:
    held = [_stub("one", "skogen")]
    assert default_identity(held, None) is None
    assert default_identity(held, "") is None


def test_no_identity_loaded_resolves_to_nothing() -> None:
    assert default_identity([], "one") is None


def test_a_narrow_viewport_is_laid_out_rather_than_cut_down() -> None:
    """The phone-width rules exist, and hide nothing: a status an operator
    cannot see is the failure this interface is built against (`web-display`)."""
    css = (STATIC_DIR / "panel.css").read_text()
    narrow = re.search(r"@media \(max-width: 40rem\) \{(.*)\n\}", css, re.DOTALL)
    assert narrow, "there are no rules for a narrow viewport"
    body = narrow.group(1)
    assert "display: none" not in body, "a narrow viewport hides something a wide one shows"
    assert "visibility: hidden" not in body
    for wrapped in (".panel-head", ".nav", ".meter-row", ".composer p"):
        assert wrapped in body, f"{wrapped} is not laid out for a narrow viewport"

    # Nothing may demand more width than a phone has; a wide table scrolls in
    # its own box instead, which `.table-wrap` does.
    wide = [value for value in re.findall(r"min-width:\s*(\d+)px", css) if int(value) > 360]
    assert not wide, f"a rule demands {wide} px of width, which a phone does not have"
    assert re.search(r"textarea,\s*\n\s*input,\s*\n\s*select \{\s*\n\s*max-width: 100%;", css)
