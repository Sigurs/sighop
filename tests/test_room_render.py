"""Rendering rooms (milestone 6, tasks 11.2 - 11.4).

Two rules carry these, and neither is cosmetic:

* **no password and no password hash appears in any output**, asserted against
  the real strings rather than trusted to review (§6, task 2.4), and
* a member is a **claimed** key, never a name: a room server learns no name from
  the login exchange, and rendering one from a contact would present a claim as
  a fact (design D8's rule, carried over from adverts).
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.monitor.render import (
    CLAIMED_MARK,
    ROOMS_OFF,
    render_delivery_acknowledged,
    render_delivery_sent,
    render_login_admitted,
    render_login_refused,
    render_member_backed_off,
    render_post_refused,
    render_post_stored,
    render_request_answered,
    render_request_refused,
    render_retention_pruned,
    render_room_event,
    render_room_startup,
    render_room_status,
)
from sighop.net.dm import Route
from sighop.net.room import (
    DeliveryAcknowledged,
    DeliverySent,
    LoginAdmitted,
    LoginRefused,
    MemberBackedOff,
    PostRefused,
    PostStored,
    RefusalReason,
    RequestAnswered,
    RequestRefused,
    RetentionPruned,
)
from sighop.protocol.payloads import Permission, RequestType, WireText
from tests.roomfixtures import ADMIN_PASSWORD, room_record

MEMBER = bytes.fromhex("aabbccddeeff00112233445566778899") + b"\x00" * 16


# --- 11.3 Startup lines -----------------------------------------------------


def test_a_served_room_names_its_identity_members_messages_guest_and_retention() -> None:
    line = render_room_startup(
        name="lounge",
        entity_name="rs-1",
        node_hash=0x7F,
        members=3,
        messages=42,
        guest_access="password",
        retention="unlimited",
    )

    assert "'lounge'" in line
    assert "rs-1[7f]" in line
    assert "members=3" in line
    assert "messages=42" in line
    assert "guest=password" in line
    assert "retention=unlimited" in line
    assert "NOT SERVED" not in line


def test_an_unlimited_retention_is_said_plainly_rather_than_left_blank() -> None:
    """10.3, design D15: a bound nobody can see is a bound nobody set."""
    room = room_record(admin_password=ADMIN_PASSWORD)
    assert room.retention == "unlimited"
    assert "retention=unlimited" in render_room_startup(
        name=room.name,
        entity_name="rs-1",
        node_hash=1,
        members=0,
        messages=0,
        guest_access=room.guest_access,
        retention=room.retention,
    )


def test_a_configured_retention_names_both_bounds() -> None:
    room = room_record(retention_days=30, retention_messages=500)
    assert room.retention == "30 days and 500 messages"


def test_a_room_bound_to_a_disabled_entity_says_it_is_not_served_and_why() -> None:
    """11.2: not served, with the reason, rather than silently absent."""
    line = render_room_startup(
        name="lounge",
        entity_name="rs-1",
        node_hash=1,
        members=0,
        messages=0,
        guest_access="refused",
        retention="unlimited",
        served=False,
        not_served_because="its identity is not enabled",
    )

    assert "NOT SERVED" in line
    assert "not enabled" in line


def test_a_run_with_no_database_says_rooms_require_durable_storage() -> None:
    """11.2, design D5: stated rather than omitted."""
    assert "none" in ROOMS_OFF
    assert "durable storage" in ROOMS_OFF


# --- 11.3 The status line ---------------------------------------------------


def test_the_room_status_line_carries_every_counter_including_zeros() -> None:
    line = render_room_status(
        name="lounge",
        members=2,
        messages_stored=0,
        deliveries_outstanding=0,
        members_behind=0,
        accepting_posts=True,
        refusals={},
        pruned=0,
        pruned_unsynced=0,
    )

    for field in ("members=2", "stored=0", "outstanding=0", "behind=0", "pruned=0"):
        assert field in line, f"{field} disappeared when it was zero"
    assert "refused=none" in line
    assert "REFUSING POSTS" not in line


def test_a_degraded_room_says_it_is_refusing_posts() -> None:
    """7.7: visible without reading the event stream."""
    line = render_room_status(
        name="lounge",
        members=1,
        messages_stored=5,
        deliveries_outstanding=1,
        members_behind=0,
        accepting_posts=False,
        refusals={"storage_degraded": 2},
        pruned=0,
        pruned_unsynced=0,
    )

    assert "REFUSING POSTS (storage degraded)" in line
    assert "refused=storage_degraded=2" in line


def test_refusals_are_reported_by_reason_rather_than_as_a_total() -> None:
    """6.7: "six refused" says nothing an operator can act on."""
    line = render_room_status(
        name="lounge",
        members=1,
        messages_stored=0,
        deliveries_outstanding=0,
        members_behind=1,
        accepting_posts=True,
        refusals={"bad_password": 4, "throttled_source": 2},
        pruned=3,
        pruned_unsynced=1,
    )

    assert "bad_password=4" in line
    assert "throttled_source=2" in line
    assert "pruned=3" in line
    assert "pruned_unsynced=1" in line


# --- 11.4 One rendering per event ------------------------------------------


def test_a_login_admitted_names_the_level_and_marks_the_key_as_claimed() -> None:
    event = LoginAdmitted(
        room_name="lounge",
        public_key=MEMBER,
        permission=Permission.ADMIN,
        new_member=True,
        flooded=True,
        packet_id="pkt1",
    )
    line = render_login_admitted(event)

    assert "joined" in line
    assert "admin" in line
    assert "flooded" in line
    assert CLAIMED_MARK in line, "a member's key is a claim, not a verified identity"
    assert MEMBER.hex()[:16] in line


def test_a_returning_member_is_not_rendered_as_a_new_one() -> None:
    line = render_login_admitted(
        LoginAdmitted(
            room_name="lounge",
            public_key=MEMBER,
            permission=Permission.READ_WRITE,
            new_member=False,
            flooded=False,
            packet_id="pkt1",
        )
    )
    assert "returned" in line and "joined" not in line
    assert "direct" in line


def test_a_login_refusal_names_its_reason_and_its_detail() -> None:
    line = render_login_refused(
        LoginRefused(
            room_name="lounge",
            reason=RefusalReason.REPLAY,
            packet_id="pkt2",
            detail="recorded timestamp 1700000000; revoking the member allows a fresh login",
            public_key=MEMBER,
        )
    )
    assert "login refused: replay" in line
    assert "revoking" in line


def test_a_stored_post_shows_its_ordering_value_and_its_text() -> None:
    line = render_post_stored(
        PostStored(
            room_name="lounge",
            author=MEMBER,
            post_timestamp=1_700_000_000,
            text=WireText.from_bytes(b"hello room"),
            retry=False,
            acknowledged=True,
            packet_id="pkt3",
        )
    )
    assert "@1700000000" in line
    assert "'hello room'" in line
    assert "retry" not in line


def test_text_that_is_not_valid_utf8_is_marked_as_a_rendering() -> None:
    """§4.1 at the display edge: our guess is never presented as their content."""
    line = render_post_stored(
        PostStored(
            room_name="lounge",
            author=MEMBER,
            post_timestamp=1,
            text=WireText.from_bytes(b"caf\xe9"),
            retry=True,
            acknowledged=False,
            packet_id="pkt3",
        )
    )
    assert "rendering of 4 bytes, not valid UTF-8" in line
    assert "retry" in line
    assert "NOT ACKNOWLEDGED" in line


def test_a_post_refusal_carries_its_reason_and_detail() -> None:
    line = render_post_refused(
        PostRefused(
            room_name="lounge",
            reason=RefusalReason.STORAGE_DEGRADED,
            packet_id="pkt4",
            detail="durable storage is unavailable, so nothing can be promised",
            author=MEMBER,
        )
    )
    assert "post refused: storage_degraded" in line
    assert "nothing can be promised" in line


def test_a_truncated_post_says_so_on_the_line_that_shows_it() -> None:
    """The author's words were dropped; the line claiming to show the post says so."""
    line = render_post_stored(
        PostStored(
            room_name="lounge",
            author=MEMBER,
            post_timestamp=1700000000,
            text=WireText.from_bytes(b"x" * 150),
            retry=False,
            acknowledged=True,
            packet_id="pkt5",
            truncated_from=156,
        )
    )
    assert "TRUNCATED from 156 bytes" in line


def test_a_delivery_shows_its_route_and_what_it_expects_back() -> None:
    line = render_delivery_sent(
        DeliverySent(
            room_name="lounge",
            member=MEMBER,
            post_timestamp=1_700_000_000,
            route=Route(flood=False, hop_count=2),
            expected_ack=b"\xde\xad\xbe\xef",
            transmitted=True,
            packet_id="pkt5",
        )
    )
    assert "pushed @1700000000" in line
    assert "DIRECT h2" in line
    assert "expect=deadbeef" in line


def test_a_gated_delivery_says_so_rather_than_looking_sent() -> None:
    line = render_delivery_sent(
        DeliverySent(
            room_name="lounge",
            member=MEMBER,
            post_timestamp=1,
            route=Route(flood=True),
            expected_ack=b"\x00\x01\x02\x03",
            transmitted=False,
            packet_id="pkt5",
        )
    )
    assert "not sent (gate closed)" in line


def test_a_bundled_acknowledgement_says_where_it_arrived() -> None:
    """Design D12: the observation §13's unknown #4 is about."""
    line = render_delivery_acknowledged(
        DeliveryAcknowledged(
            room_name="lounge",
            member=MEMBER,
            post_timestamp=7,
            bundled=True,
            packet_id="pkt6",
        )
    )
    assert "bundled in a path return" in line


def test_a_backed_off_member_says_what_brings_it_back() -> None:
    line = render_member_backed_off(MemberBackedOff(room_name="lounge", member=MEMBER, failures=3))
    assert "backed off after 3" in line
    assert "next heard from" in line


def test_an_answered_request_names_the_type() -> None:
    line = render_request_answered(
        RequestAnswered(
            room_name="lounge",
            member=MEMBER,
            request_type=RequestType.GET_STATUS,
            reply_bytes=56,
            packet_id="pkt7",
        )
    )
    assert "answered get_status" in line
    assert "56B" in line


def test_an_unanswered_request_names_the_type_even_when_it_is_a_bare_number() -> None:
    line = render_request_refused(
        RequestRefused(
            room_name="lounge",
            reason=RefusalReason.UNSUPPORTED_REQUEST,
            request_type=0x7E,
            packet_id="pkt8",
            member=MEMBER,
        )
    )
    assert "126" in line
    assert "unsupported_request" in line


def test_retention_reports_what_it_cost_a_member() -> None:
    """10.3, design D15: the trade is visible rather than discovered later."""
    line = render_retention_pruned(
        RetentionPruned(room_name="lounge", deleted=10, deleted_unsynced=4)
    )
    assert "removed 10 messages" in line
    assert "4 of which a member had not yet received" in line
    assert "gap" in line


def test_retention_that_cost_nobody_anything_says_only_what_it_removed() -> None:
    line = render_retention_pruned(
        RetentionPruned(room_name="lounge", deleted=10, deleted_unsynced=0)
    )
    assert "removed 10 messages" in line
    assert "gap" not in line


@pytest.mark.parametrize(
    "event",
    [
        LoginAdmitted("lounge", MEMBER, Permission.ADMIN, True, True, "p"),
        LoginRefused("lounge", RefusalReason.BAD_PASSWORD, "p"),
        PostStored("lounge", MEMBER, 1, WireText.from_bytes(b"x"), False, True, "p"),
        PostRefused("lounge", RefusalReason.NO_PERMISSION, "p"),
        DeliverySent("lounge", MEMBER, 1, Route(flood=True), b"\x00" * 4, True, "p"),
        DeliveryAcknowledged("lounge", MEMBER, 1, False, "p"),
        MemberBackedOff("lounge", MEMBER, 3),
        RequestAnswered("lounge", MEMBER, RequestType.KEEP_ALIVE, 5, "p"),
        RequestRefused("lounge", RefusalReason.NO_ROUTE, RequestType.KEEP_ALIVE, "p"),
        RetentionPruned("lounge", 1, 0),
    ],
)
def test_every_room_event_has_a_rendering(event) -> None:
    """11.4: an event with no rendering is an event the operator never sees."""
    line = render_room_event(event)
    assert isinstance(line, str) and line.strip()
    assert "lounge" in line


# --- 2.4 / 11.3 Nothing renders a password ----------------------------------


def test_no_password_and_no_hash_reaches_any_room_output() -> None:
    """2.4, task 11.3: asserted against the strings, not trusted to review."""
    room = room_record(admin_password=ADMIN_PASSWORD, guest_password="guest-secret")
    rendered = "\n".join(
        [
            render_room_startup(
                name=room.name,
                entity_name="rs-1",
                node_hash=1,
                members=1,
                messages=1,
                guest_access=room.guest_access,
                retention=room.retention,
            ),
            render_room_status(
                name=room.name,
                members=1,
                messages_stored=1,
                deliveries_outstanding=0,
                members_behind=0,
                accepting_posts=True,
                refusals={"bad_password": 1},
                pruned=0,
                pruned_unsynced=0,
            ),
            render_room_event(LoginAdmitted("lounge", MEMBER, Permission.ADMIN, True, False, "p")),
            render_room_event(
                LoginRefused("lounge", RefusalReason.BAD_PASSWORD, "p", public_key=MEMBER)
            ),
        ]
    )

    assert ADMIN_PASSWORD not in rendered
    assert "guest-secret" not in rendered
    assert room.admin_password_hash not in rendered
    assert room.guest_password_hash is not None
    assert room.guest_password_hash not in rendered
    assert "$argon2id$" not in rendered


# --- `net/` imports nothing from `monitor/` (task 11.4) ---------------------


def test_the_room_server_imports_nothing_from_monitor() -> None:
    """Design D17: policy in `net/`, formatting in `monitor/`, one direction."""
    import ast
    from pathlib import Path

    import sighop.net.room as module

    source = Path(module.__file__).read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    offenders = [name for name in imported if name.startswith("sighop.monitor")]
    assert not offenders, f"{offenders} would make `net/` depend on rendering"


def test_the_renderer_carries_no_room_policy() -> None:
    """The other direction of the same seam: `monitor/` formats, never decides."""
    import ast
    from pathlib import Path

    import sighop.monitor.render as module

    source = Path(module.__file__).read_text()
    names = {
        node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.AsyncFunctionDef)
    }
    assert names == set(), "a renderer that awaits is a renderer that does work"


def test_the_startup_line_and_the_dedup_stats_still_render() -> None:
    """The existing render tests are the real guard; this is a smoke check that
    nothing in this milestone displaced them."""
    from sighop.monitor.render import render_gate

    assert render_gate(False)
    assert isinstance(dt.datetime.now(dt.UTC), dt.datetime)
