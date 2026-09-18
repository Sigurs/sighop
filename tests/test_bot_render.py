"""Bot output (`runtime-cli`, tasks 11.1, 11.2).

String comparisons, because these lines are the operator's whole view of a
component whose correct behaviour is usually to stay quiet. The rule with teeth
is 11.2's: **an observe-mode decision is never formatted like a transmission.**
A dry run whose output looked like a live one would defeat the entire staged
exercise, so it is asserted against the actual strings rather than trusted to
whoever edits the renderer next.

The import direction is asserted too: `bots/` must never import `monitor/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import sighop
from sighop.bots.base import (
    BotActed,
    BotDispatchDropped,
    BotFailed,
    BotMode,
    BotSendResult,
    BotSuppressed,
    BotWouldAct,
    SuppressionReason,
)
from sighop.monitor.render import (
    BOTS_OFF,
    render_bot_event,
    render_bot_mode_change,
    render_bot_startup,
    render_bot_status,
)
from sighop.net.contacts import Contact
from sighop.protocol.payloads import WireText

PEER = Contact(
    public_key=bytes.fromhex("aa") * 32,
    name=WireText.from_bytes(b"stranger"),
    advert_verified=True,
)


# --- 11.1 Startup and status ------------------------------------------------


def test_a_running_bot_names_its_driver_its_mode_and_its_limits() -> None:
    line = render_bot_startup(
        name="greeter-bot",
        driver="greeter",
        node_hash=0x2A,
        mode="observe",
        limits={"rate_per_hour": 6.0, "burst": 3, "max_hops": 1, "min_snr_db": None},
    )

    assert line == (
        "bot 'greeter-bot'[2a]  driver=greeter  mode=observe  "
        "rate_per_hour=6.0 burst=3 max_hops=1 min_snr_db=none"
    )


def test_a_bot_that_is_not_run_says_so_and_says_why() -> None:
    """11.1: a bot silent because it is disabled must be distinguishable from
    one silent because it decided to be."""
    line = render_bot_startup(
        name="greeter-bot",
        driver="greeter",
        node_hash=0x2A,
        mode="observe",
        limits={},
        served=False,
        not_served_because="the bot is disabled",
    )

    assert (
        line
        == "bot 'greeter-bot'[2a]  driver=greeter  mode=observe  NOT RUNNING (the bot is disabled)"
    )


def test_no_database_says_bots_are_off_rather_than_omitting_the_subject() -> None:
    assert "bots: none" in BOTS_OFF
    assert "durable storage" in BOTS_OFF


def test_the_status_line_renders_every_counter_including_the_zeros() -> None:
    """11.1: a field that disappears when it is zero cannot be told from a field
    nobody wrote."""
    line = render_bot_status(
        name="greeter-bot",
        driver="greeter",
        mode="active",
        actions=2,
        observations=0,
        suppressions={"too_many_hops": 11, "already_greeted": 340},
        dropped=0,
        failures=0,
        pending=1,
        announces=2,
    )

    assert line == (
        "== bot 'greeter-bot' driver=greeter mode=active acted=2 announced=2 "
        "observed=0 pending=1 dropped=0 failures=0 "
        "suppressed=already_greeted=340,too_many_hops=11"
    )


def test_a_bot_that_suppressed_nothing_says_none_rather_than_nothing() -> None:
    line = render_bot_status(
        name="greeter-bot",
        driver="greeter",
        mode="observe",
        actions=0,
        observations=0,
        suppressions={},
        dropped=0,
        failures=0,
    )

    assert line.endswith("suppressed=none")


# --- 11.1 Decisions ---------------------------------------------------------


def test_an_action_names_the_bot_the_peer_and_the_outcome() -> None:
    line = render_bot_event(
        BotActed(
            bot_name="greeter-bot",
            driver="greeter",
            contact=PEER,
            text="hello",
            result=BotSendResult.ACKNOWLEDGED,
            attempts=1,
            route="DIRECT h0",
        )
    )

    assert line == (
        "          -> bot 'greeter-bot'  sent to ✗aaaaaaaaaaaaaaaa  DIRECT h0  "
        "attempts=1  acknowledged  'hello'"
    )


def test_an_unacknowledged_send_says_so_in_capitals() -> None:
    line = render_bot_event(
        BotActed(
            bot_name="greeter-bot",
            driver="greeter",
            contact=PEER,
            text="hello",
            result=BotSendResult.UNACKNOWLEDGED,
            attempts=4,
            route="DIRECT h1",
        )
    )

    assert "NOT ACKNOWLEDGED" in line
    assert "attempts=4" in line


def test_a_suppression_names_its_reason_and_its_detail() -> None:
    line = render_bot_event(
        BotSuppressed(
            bot_name="greeter-bot",
            driver="greeter",
            reason=SuppressionReason.TOO_MANY_HOPS,
            contact=PEER,
            detail="4 hops, limit 1",
        )
    )

    assert line == (
        "          .. bot 'greeter-bot'  suppressed: too_many_hops  "
        "✗aaaaaaaaaaaaaaaa  4 hops, limit 1"
    )


def test_a_dropped_dispatch_says_reception_is_unaffected() -> None:
    line = render_bot_event(BotDispatchDropped(bot_name="greeter-bot", driver="greeter", dropped=3))

    assert "dropped the oldest pending dispatch" in line
    assert "reception is unaffected" in line


def test_a_driver_failure_names_the_bot_the_event_and_the_error() -> None:
    line = render_bot_event(
        BotFailed(
            bot_name="greeter-bot",
            driver="greeter",
            event="AdvertEvent",
            error="RuntimeError: boom",
            failures=2,
        )
    )

    assert "AdvertEvent" in line
    assert "RuntimeError: boom" in line
    assert "failures=2" in line
    assert "the run continues" in line


# --- 11.2 An observation is never a transmission ----------------------------


def test_an_observed_decision_says_plainly_that_it_transmitted_nothing() -> None:
    line = render_bot_event(
        BotWouldAct(bot_name="greeter-bot", driver="greeter", contact=PEER, text="hello")
    )

    assert line == (
        "          .. bot 'greeter-bot'  would send to ✗aaaaaaaaaaaaaaaa  "
        "transmitted nothing (observe mode)  'hello'"
    )


def test_an_observation_shares_no_shape_with_a_transmission() -> None:
    """11.2: not "sent", not the outbound arrow, and not the route and attempt
    fields a real send carries. An operator reading a dry run must not be able
    to mistake it for a live one at a glance."""
    observed = render_bot_event(
        BotWouldAct(bot_name="b", driver="greeter", contact=PEER, text="hello")
    )
    acted = render_bot_event(
        BotActed(
            bot_name="b",
            driver="greeter",
            contact=PEER,
            text="hello",
            result=BotSendResult.ACKNOWLEDGED,
            attempts=1,
            route="DIRECT h0",
        )
    )

    assert "->" in acted and "->" not in observed
    assert "sent to" in acted and "sent to" not in observed
    assert "attempts=" in acted and "attempts=" not in observed
    assert "transmitted nothing" in observed and "transmitted nothing" not in acted


def test_making_a_bot_active_says_the_run_flag_still_applies() -> None:
    """11.1, design D4: two gates, and the operator who just opened one is
    exactly the person who needs telling about the other."""
    line = render_bot_mode_change("greeter-bot", BotMode.ACTIVE)

    assert "may transmit" in line
    assert "--enable-transmit" in line
    assert "duty-cycle ceiling" in line


def test_making_a_bot_observe_says_it_transmits_nothing() -> None:
    line = render_bot_mode_change("greeter-bot", BotMode.OBSERVE)

    assert "observe mode" in line
    assert "transmits nothing" in line


# --- The import direction ---------------------------------------------------


def test_the_bot_package_never_imports_the_renderer() -> None:
    """11.1: `bots/` emits typed events and `monitor/` turns one into a line.

    The same rule `net/room.py` follows (milestone 6 design D17), asserted the
    same way: a static scan, because an import added in a hurry is exactly the
    one nobody notices in review.
    """
    package = Path(sighop.__file__).parent / "bots"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(name.startswith("sighop.monitor") for name in names):
                offenders.append(f"{path.name}:{getattr(node, 'lineno', 0)}")

    assert not offenders, f"{offenders} import the renderer; bots/ emits events"
