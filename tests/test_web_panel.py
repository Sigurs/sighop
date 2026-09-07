"""The panel's shell and its two rendering primitives (design D12, D13).

Both are hard rules from §8 rather than preferences, and both are the kind of
rule that decays into "mostly true" unless something structural holds it:

* **The duty-cycle meter is on every page.** Asserted by enumerating the route
  table rather than by checking the pages somebody remembered, because the way
  this rule breaks is a new page that does not extend the base template.
* **Unverified content is never drawn as verified.** Asserted by scanning the
  templates for a contact name rendered outside the macro, and by checking that
  the distinction survives with every colour stripped out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

import sighop.web
from sighop.net.contacts import Contact
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import WireText
from sighop.web.app import allowed_hosts, create_app
from sighop.web.render import (
    KEY_ONLY_MARK,
    UNVERIFIED_MARK,
    VERIFIED_MARK,
    Collection,
    PersistenceView,
    identity_for,
    identity_for_key,
    meter_for,
    persistence_view,
    read,
    unreadable,
)
from tests.test_web_state import RecordingLogger
from tests.webfixtures import StubState, stub_state

TEMPLATE_DIR = Path(sighop.web.__file__).parent / "templates"
HOSTS = allowed_hosts("127.0.0.1", 8080)


def _app(**kwargs: object) -> tuple[FastAPI, StubState]:
    state = stub_state(**kwargs)  # type: ignore[arg-type]
    return create_app(state, hosts=HOSTS, logger=RecordingLogger()), state


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8080")


def _pages(app: FastAPI) -> list[str]:
    return [
        route.path
        for route in app.routes
        if isinstance(route, Route)
        and "{" not in route.path
        and "GET" in (route.methods or set())
    ]


# --- 9.1 The meter is on every page -----------------------------------------


def test_every_registered_page_renders_the_meter() -> None:
    """9.1, design D12: impossible to render a page without it."""
    app, _state = _app()
    pages = _pages(app)
    assert pages, "no pages to enumerate; the assertion would be vacuous"

    with _client(app) as client:
        for path in pages:
            body = client.get(path).text
            assert 'aria-label="duty cycle"' in body, f"{path} has no duty-cycle meter"
            assert "duty cycle" in body
            assert "transmit" in body, f"{path} does not state the gate"
            assert "persistence" in body, f"{path} does not state durability"


def test_every_page_extends_the_base_template() -> None:
    """9.1: the structural half — one shell, so the meter cannot be forgotten.

    The error page is the deliberate exception and says why in its own comment:
    a page rendered *because* something failed must not depend on reading the
    live state that may be what failed.
    """
    for template in sorted(TEMPLATE_DIR.glob("*.html")):
        if template.name.startswith("_") or template.name == "base.html":
            continue
        source = template.read_text()
        if template.name == "error.html":
            assert "does not extend base.html" in source
            continue
        assert '{% extends "base.html" %}' in source, f"{template.name} has no shell"


# --- 9.2 What the meter says ------------------------------------------------


def test_a_closed_gate_says_nothing_will_be_transmitted() -> None:
    """9.2: a closed gate is not zero usage — packets are still charged."""
    app, _state = _app(transmit_enabled=False)

    with _client(app) as client:
        body = client.get("/").text

    assert "transmit disabled" in body
    assert "nothing will be transmitted" in body
    assert "would have been charged" in body


def test_an_open_gate_says_the_node_is_transmitting() -> None:
    """9.2: and the other way, because silence about it would be worse."""
    app, _state = _app(transmit_enabled=True)

    with _client(app) as client:
        body = client.get("/").text

    assert "transmit enabled" in body
    assert "nothing will be transmitted" not in body


def test_a_raised_ceiling_is_stated_as_raised() -> None:
    """9.2: a proportion of a limit the operator cannot see is not a meter."""
    app, _state = _app(ceiling_fraction=0.5)

    with _client(app) as client:
        body = client.get("/").text

    assert "ceiling raised to 50%" in body
    assert "regulatory default" in body
    assert "regulatory limit, not a tuning knob" in body


def test_the_default_ceiling_is_not_announced_as_raised() -> None:
    """9.2: the warning means something only if it is absent when it should be."""
    app, _state = _app()

    with _client(app) as client:
        body = client.get("/").text

    assert "ceiling raised" not in body
    assert f"ceiling {DEFAULT_CEILING_FRACTION * 100:g}%" in body


def test_a_budget_near_its_limit_reads_as_near_its_limit() -> None:
    """9.2: the numbers, and the words beside them, at three points."""
    _built, state = _app()
    scheduler = state.scheduler

    empty = meter_for(scheduler.status())
    assert empty.used_pct == 0.0
    assert empty.remaining_pct == 100.0
    assert empty.level == "ok"
    assert empty.state_text == "within budget"

    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    scheduler.budget.charge(scheduler.budget.ceiling_ms * 0.9, now)
    near = meter_for(scheduler.status())
    assert 89.0 < near.used_pct < 91.0
    assert near.bar_pct == pytest.approx(near.used_pct)
    assert near.level in ("reserve", "ok")

    scheduler.budget.charge(scheduler.budget.ceiling_ms * 0.5, now)
    over = meter_for(scheduler.status())
    assert over.used_pct > 100.0
    assert over.bar_pct == 100.0, "a meter cannot be more than full"
    assert over.remaining_pct == 0.0
    assert over.level == "full"
    assert "ceiling reached" in over.state_text


# --- 9.3 The identity macro is the only way an identity is drawn ------------


def _contact(name: str | None, *, verified: bool) -> Contact:
    return Contact(
        public_key=generate_identity().public_key,
        name=None if name is None else WireText.from_bytes(name.encode()),
        advert_verified=verified,
    )


def test_the_three_verification_states_are_three_different_claims() -> None:
    """9.3, design D13: verified, unverified and key-only are not two things."""
    verified = identity_for(_contact("[redacted]", verified=True))
    assert verified.verification == "verified"
    assert verified.mark == VERIFIED_MARK
    assert "verified" in verified.marking_text

    unverified = identity_for(_contact("pasted-in", verified=False))
    assert unverified.verification == "unverified"
    assert unverified.mark == UNVERIFIED_MARK
    assert "no signature" in unverified.marking_text

    state = stub_state()
    key_only = identity_for_key(generate_identity().public_key, state.contacts)
    assert key_only.verification == "key_only"
    assert key_only.mark == KEY_ONLY_MARK
    assert key_only.name is None
    assert key_only.label == key_only.short_key


def test_a_key_that_matches_a_contact_resolves_to_that_contact() -> None:
    """9.3: the resolution a feed row and a room author both go through."""
    state = stub_state()
    contact = _contact("[redacted]", verified=True)
    state.contacts.restore([contact])

    view = identity_for_key(contact.public_key, state.contacts)
    assert view.verification == "verified"
    assert view.name == "[redacted]"


def test_no_template_renders_a_contact_name_outside_the_macro() -> None:
    """9.3: a per-view `if` is how a hard rule becomes ninety per cent true.

    A static scan for the two attributes that would draw a name directly. The
    macro is where they are allowed; anywhere else is a view that could show an
    unverified name in the shape reserved for a verified one.
    """
    forbidden = re.compile(r"\{\{[^}]*\.(display_name|contact_name)\b")
    offenders: dict[str, list[str]] = {}
    for template in sorted(TEMPLATE_DIR.rglob("*.html")):
        if template.name == "_identity.html":
            continue
        found = forbidden.findall(template.read_text())
        if found:
            offenders[template.name] = found
    assert not offenders, (
        f"{offenders} draw a name without the identity macro; §8's rule is that "
        "unverified content is never presented as verified, everywhere"
    )


def test_the_distinction_survives_with_every_colour_stripped() -> None:
    """9.3: a glyph and a word, never colour alone (design D13)."""
    verified = identity_for(_contact("[redacted]", verified=True))
    unverified = identity_for(_contact("[redacted]", verified=False))

    rendered = {
        view.verification: f"{view.mark} {view.label} {view.verification}"
        for view in (verified, unverified)
    }
    assert rendered["verified"] != rendered["unverified"]
    assert VERIFIED_MARK in rendered["verified"]
    assert UNVERIFIED_MARK in rendered["unverified"]
    # Identical names, so the *only* difference is the marking itself.
    assert verified.label == unverified.label


def test_the_stylesheet_never_conveys_verification_by_colour_alone() -> None:
    """9.3 / 9.4: colour is added to a glyph, never used instead of one."""
    css = (Path(sighop.web.__file__).parent / "static" / "panel.css").read_text()
    for state in ("verified", "unverified", "key_only"):
        assert f".identity-{state} .identity-mark" in css, (
            f"{state} has no glyph rule; colour would be carrying it alone"
        )


# --- 9.4 The instrument panel's styling -------------------------------------


def test_the_stylesheet_states_the_four_rules_it_implements() -> None:
    """9.4: reviewed by assertion, against §8's four bullets."""
    css = (Path(sighop.web.__file__).parent / "static" / "panel.css").read_text()
    assert "Live state is the centre of gravity" in css
    assert "Monospace and tabular" in css
    assert "Colour carries meaning, not decoration" in css
    assert "Dark first" in css
    assert "tabular-nums" in css
    assert "--rx" in css and "--tx" in css, "direction has no colour of its own"


def test_hash_and_path_columns_render_in_a_fixed_width_class() -> None:
    """9.4: fixed-width data is fixed-width, so columns of it align."""
    app, _state = _app()

    with _client(app) as client:
        body = client.get("/").text

    assert 'class="mono' in body, "no fixed-width column on the panel at all"
    css = (Path(sighop.web.__file__).parent / "static" / "panel.css").read_text()
    assert ".mono," in css or ".mono {" in css


def test_dark_first_is_the_stylesheet_and_not_a_preference() -> None:
    """9.4: it sits beside other radio tooling; a white page is the odd window."""
    css = (Path(sighop.web.__file__).parent / "static" / "panel.css").read_text()
    assert "--bg: #14161a" in css
    assert "background: var(--bg)" in css


# --- 9.5 Empty is not unavailable -------------------------------------------


def test_empty_and_unavailable_are_different_wordings() -> None:
    """9.5, `web-server`: "no rooms" and "cannot read rooms" are two screens."""
    empty: Collection[str] = read([])
    assert empty.readable is True
    assert empty.empty is True
    assert len(empty) == 0

    missing: Collection[str] = unreadable("the database is unreachable")
    assert missing.readable is False
    assert missing.empty is False
    assert missing.unavailable == "the database is unreachable"

    partial = read(["one", "two"])
    assert partial.readable is True
    assert partial.empty is False
    assert list(partial) == ["one", "two"]


def test_the_shared_partial_words_the_two_states_differently() -> None:
    """9.5: one partial, so the distinction is made once rather than per view."""
    source = (TEMPLATE_DIR / "_collection.html").read_text()
    assert "cannot be read" in source
    assert "No {{ noun }} stored" in source
    assert "there\n    is nothing in it" in source or "nothing in it" in source


def test_no_database_and_a_degraded_one_are_two_different_lines() -> None:
    """9.5: three states of durability, not two."""
    off = persistence_view(None)
    assert off.configured is False
    assert off.level == "off"
    assert "nothing here survives" in off.text

    class _Fake:
        state = "degraded"
        degraded = True

        def as_json(self) -> dict[str, object]:
            return {"packet_log_discarded": 3, "direct_messages_refused": 2}

    degraded = persistence_view(_Fake())
    assert degraded.level == "degraded"
    assert "degraded" in degraded.text
    assert degraded.discarded == 3
    assert degraded.refused == 2
    assert degraded.losses_text == "3 write(s) discarded, 2 refused"


def test_a_healthy_database_reports_no_losses_rather_than_zero_ones() -> None:
    """9.5: a count of nothing lost is noise; the absence of the phrase is the signal."""
    healthy = PersistenceView(configured=True, state="ok", degraded=False)
    assert healthy.losses_text == ""
    assert healthy.level == "ok"
