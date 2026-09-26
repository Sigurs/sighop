"""The dashboard's pages (`web-dashboard`, §8's observability area).

Every value on these pages comes from the running platform's own state, which
is what §6's "memory stays the authority" means for a display: the panel reads
the stores the radio is using, never a second copy of them.

Three of the assertions here are about telling two things apart that look alike
and are not:

* a **zero-hop route** and **no route at all** — the first is the most useful
  route a node can have;
* a value the board **did not answer** and a value that is **zero** — a board
  with no battery sensor is not a board with a flat battery (§4.1);
* a route matched by **public key** and one matched by **node hash** — one byte,
  1 in 256, and the display says which it was (§3).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sighop.net.contacts import Contact
from sighop.net.paths import LearnedPath, PathKey
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, WireText
from sighop.radio.modem import EU868_NARROW, RadioParams
from sighop.radio.probe import AbsenceReason, Absent, FirmwareVersion, ProbeResult
from sighop.web.app import allowed_hosts, create_app
from sighop.web.feed import EntityTraffic, FeedHub
from sighop.web.render import contact_rows, modem_readings, navigation, queue_rows
from tests.test_web_feed import _outcome, _reception, _submission
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    StubState,
    authenticator,
    session_of,
    signed_client,
    stub_state,
)

pytestmark = pytest.mark.usefixtures("default_persistence")

HOSTS = allowed_hosts("127.0.0.1", 8080)
NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def _client(app: FastAPI) -> TestClient:
    return signed_client(app, base_url="http://127.0.0.1:8080")


def _app(state: StubState, feed: FeedHub | None = None) -> FastAPI:
    return create_app(state, auth=authenticator(), feed=feed, hosts=HOSTS, logger=RecordingLogger())


def _contact(name: str, *, verified: bool = True, key: bytes | None = None) -> Contact:
    return Contact(
        public_key=key or generate_identity().public_key,
        name=WireText.from_bytes(name.encode()),
        node_type=NodeType.REPEATER,
        advert_verified=verified,
        first_heard=NOW - dt.timedelta(hours=3),
        last_heard=NOW,
    )


def _learn(state: StubState, key: PathKey, path: bytes, hops: int, *, snr: float = 8.0) -> None:
    state.pipeline.paths._insert(
        key,
        LearnedPath(
            path=path,
            hash_size=1,
            hop_count=hops,
            snr_db=snr,
            confirmed_at=NOW,
            packet_id="seed",
        ),
    )


# --- 11.1 The overview ------------------------------------------------------


def test_the_overview_shows_every_counter_against_a_stub_state() -> None:
    """11.1: queue depths, scheduler counts, dedup, paths, contacts, durability."""
    state = stub_state()
    contact = _contact("syn-harbor-repeater")
    state.contacts.restore([contact])
    _learn(state, PathKey.for_public_key(contact.public_key), b"\xbe\xd0", 2)
    state.pipeline.delivered = 41
    state.pipeline.dedup.observe(_reception())

    with _client(_app(state)) as client:
        body = client.get("/").text

    assert "transmit queue" in body
    for name in ("ack", "response", "message", "advert"):
        assert name in body, f"the {name} queue is not shown"
    assert "learned path destinations" in body
    assert ">1<" in body, "the learned path count is not rendered"
    assert "duplicates dropped" in body
    assert "contacts" in body
    assert "persistence on" in body


def test_the_queue_rows_name_the_priority_classes() -> None:
    """11.1: a priority class is `0` on the wire and `ack` to a reader."""
    state = stub_state()
    rows = queue_rows(state.scheduler.status())

    assert [row.name for row in rows] == ["ack", "response", "message", "advert"]
    assert all(row.depth == 0 for row in rows)


def test_the_meter_strip_shows_the_persistence_state_and_its_losses() -> None:
    """11.1: discarded writes are on the page, not only in the status line — in
    the strip on every page, which the overview no longer repeats."""

    class _Degraded:
        state = "degraded"
        degraded = True

        def as_json(self) -> dict[str, object]:
            return {"packet_log_discarded": 12, "direct_messages_refused": 4}

    state = stub_state(persistence=_Degraded())  # type: ignore[arg-type]

    with _client(_app(state)) as client:
        body = client.get("/").text

    meter = body[body.index('<section class="meter"') : body.index("</section>")]
    assert "persistence degraded" in meter
    assert "12 writes discarded, 4 refused" in meter
    assert "<h2>persistence</h2>" not in body


# --- 11.2 Modem health ------------------------------------------------------


def _probe(*, battery: object, radio: RadioParams | None = None) -> ProbeResult:
    return ProbeResult(
        configured_radio=EU868_NARROW,
        device_name="Heltec V3",
        radio=radio if radio is not None else EU868_NARROW,
        tx_power_dbm=14,
        firmware_version=FirmwareVersion(version=1, reserved=0),
        battery_mv=battery,  # type: ignore[arg-type]
        mcu_temp_tenths_c=Absent(reason=AbsenceReason.UNSUPPORTED, error_code=5),
        sensors_raw=Absent(reason=AbsenceReason.TIMEOUT),
    )


def test_a_value_the_board_did_not_answer_is_shown_as_absent() -> None:
    """11.2, §4.1: absent, never zero and never a default."""
    absent = Absent(reason=AbsenceReason.UNSUPPORTED, error_code=5)
    state = stub_state(probe_result=_probe(battery=absent), radio=EU868_NARROW)

    with _client(_app(state)) as client:
        body = client.get("/system").text

    assert "battery" in body
    assert "absent (unsupported)" in body
    assert ">0 mV<" not in body, "an unanswered battery was rendered as zero"


def test_a_value_the_board_did_answer_is_shown_as_the_value() -> None:
    """11.2: the assertion above means something only if the other way works."""
    state = stub_state(probe_result=_probe(battery=4021), radio=EU868_NARROW)

    readings = {reading.label: reading for reading in modem_readings(state.probe_result, None)}
    assert readings["battery"].absent is False
    assert readings["battery"].text == "4021 mV"
    assert readings["mcu temperature"].absent is True
    assert "unsupported" in readings["mcu temperature"].text


def test_a_run_with_no_probe_says_so_rather_than_showing_nothing() -> None:
    """11.2: empty and unavailable, again — a replay has no board to ask."""
    state = stub_state()

    with _client(_app(state)) as client:
        body = client.get("/system").text

    assert "No probe result" in body
    assert "no board to ask" in body


def test_a_readback_that_disagrees_with_the_configuration_is_shown() -> None:
    """11.2, design D5/D6: a disagreement is visible rather than averaged away."""
    other = RadioParams(freq_hz=868_500_000, bw_hz=62_500, sf=8, cr=5)
    state = stub_state(probe_result=_probe(battery=4000, radio=other), radio=other)

    with _client(_app(state)) as client:
        body = client.get("/system").text

    assert "does not match what was configured" in body


# --- 11.3 The contact table -------------------------------------------------


def test_a_contact_links_to_one_conversation_as_the_operators_default() -> None:
    """One link per contact, not one per identity: the choice is held, not repeated."""
    state = stub_state(stub_names=("first", "second"))
    contact = _contact("peer")
    state.contacts.restore([contact])
    one, two = state.adverts.stubs

    with _client(_app(state)) as client:
        session_of(client).default_entity_id = two.entity_id
        body = client.get("/contacts").text

    peer = contact.public_key.hex()
    assert f'<a href="/chat/{two.identity.public_key.hex()}/{peer}">as second</a>' in body
    assert f"/chat/{one.identity.public_key.hex()}/{peer}" not in body


def test_one_loaded_identity_needs_no_default_to_link() -> None:
    state = stub_state(stub_names=("only",))
    contact = _contact("peer")
    state.contacts.restore([contact])
    only = state.adverts.stubs[0]

    with _client(_app(state)) as client:
        body = client.get("/contacts").text

    assert (
        f'<a href="/chat/{only.identity.public_key.hex()}/{contact.public_key.hex()}">as only</a>'
        in body
    )


def test_several_identities_and_no_default_point_to_where_one_is_chosen() -> None:
    """A conversation cannot be opened without an identity, so the row says where
    to choose one rather than guessing."""
    state = stub_state(stub_names=("first", "second"))
    state.contacts.restore([_contact("peer")])

    with _client(_app(state)) as client:
        body = client.get("/contacts").text

    assert 'href="/chat/' not in body
    assert '<a href="/chat">choose an identity to chat as</a>' in body


def test_with_no_identity_the_contact_list_offers_no_conversation() -> None:
    state = stub_state()
    state.contacts.restore([_contact("peer")])

    with _client(_app(state)) as client:
        body = client.get("/contacts").text

    assert 'href="/chat/' not in body
    assert "No identity to send as" in body


def test_a_zero_hop_route_is_a_route_and_not_the_absence_of_one() -> None:
    """11.3: an empty path is a direct neighbour, not "no route known"."""
    state = stub_state()
    contact = _contact("neighbour")
    state.contacts.restore([contact])
    _learn(state, PathKey.for_public_key(contact.public_key), b"", 0)

    rows = contact_rows(state.contacts, state.pipeline.paths)
    assert len(rows) == 1
    assert rows[0].route is not None
    assert rows[0].route.zero_hop is True
    assert rows[0].route.text == "direct, zero hops"

    with _client(_app(state)) as client:
        body = client.get("/contacts").text
    assert "direct, zero hops" in body
    assert "no route known" not in body.split('<p class="note">')[0]


def test_a_contact_with_no_route_says_so() -> None:
    """11.3: the other half of the same distinction."""
    state = stub_state()
    state.contacts.restore([_contact("unreachable")])

    rows = contact_rows(state.contacts, state.pipeline.paths)
    assert rows[0].route is None
    assert rows[0].route_text == "no route known"

    with _client(_app(state)) as client:
        assert "no route known" in client.get("/contacts").text


def test_a_route_matched_by_node_hash_is_marked_ambiguous() -> None:
    """11.3, §3: one byte collides at 1 in 256, and the table says which match."""
    state = stub_state()
    contact = _contact("hash-matched")
    state.contacts.restore([contact])
    _learn(state, PathKey.for_node_hash(contact.node_hash), b"\xa1", 1)

    rows = contact_rows(state.contacts, state.pipeline.paths)
    assert rows[0].route is not None
    assert rows[0].route.ambiguous is True
    assert "1 in 256" in rows[0].route.caveat

    with _client(_app(state)) as client:
        body = client.get("/contacts").text
    assert "ambiguous" in body


def test_the_contact_table_carries_identity_route_and_signal_together() -> None:
    """11.3: the columns `web-dashboard` names, in one row."""
    state = stub_state()
    contact = _contact("syn-tower-repeater")
    state.contacts.restore([contact])
    _learn(state, PathKey.for_public_key(contact.public_key), b"\xbe\xd0", 2, snr=12.25)

    with _client(_app(state)) as client:
        body = client.get("/contacts").text

    full = contact.public_key.hex()
    assert f'<code class="mono" title="{full}">{full[:6]}…</code>' in body, (
        "the key is not abbreviated"
    )
    assert f'data-copy="{full}"' in body, "the copy control does not carry the whole key"
    assert f"0x{contact.node_hash:02x}" in body
    assert "REPEATER" in body
    assert '<span class="status-figures" aria-hidden="true">2 · bed0</span>' in body
    assert 'title="2 hops via bed0"' in body
    assert "+12.25" in body
    assert "syn-tower-repeater" in body


def test_an_unverified_contact_is_drawn_distinctly_in_the_table() -> None:
    """11.3 / 9.3: §8's hard rule, in the densest view there is."""
    state = stub_state()
    state.contacts.restore([_contact("verified-peer"), _contact("pasted-in", verified=False)])

    with _client(_app(state)) as client:
        body = client.get("/contacts").text

    assert "identity-verified" in body
    assert "identity-unverified" in body


# --- 11.4 Per-entity counters -----------------------------------------------


def test_an_entitys_transmissions_are_counted_against_that_entity() -> None:
    """11.4: TX is exact — every submission names the identity that made it."""
    traffic = EntityTraffic()
    traffic.on_transmission(_submission(), _outcome())
    traffic.on_transmission(_submission(), _outcome())
    from sighop.net.bus import TxResult

    traffic.on_transmission(_submission(), _outcome(TxResult.SUPPRESSED))

    counts = traffic.for_entity("entity-1", node_hash=0x00)
    assert counts["transmitted"] == 2
    assert counts["suppressed"] == 1
    assert traffic.for_entity("someone-else", node_hash=0x00)["transmitted"] == 0


def test_the_page_shows_an_entitys_counts_against_a_stub_state() -> None:
    """11.4: the numbers, and the caveat that makes the RX one honest."""
    state = stub_state(stub_names=("panel-identity",))
    hub = FeedHub()
    stub = state.adverts.stubs[0]
    submission = _submission()
    object.__setattr__(submission, "entity_id", stub.entity_id)
    hub.on_transmission(submission, _outcome(), at=NOW)
    hub.on_reception(_reception(), False)

    with _client(_app(state, feed=hub)) as client:
        body = client.get("/admin/identities").text
        overview = client.get("/").text

    row = body[body.index(stub.identity.public_key.hex()) :]
    row = row[: row.index("</tr>")]
    adverts, transmitted, suppressed, _addressed = re.findall(r'<td class="num">(\d+)</td>', row)
    assert (adverts, transmitted, suppressed) == ("0", "1", "0")
    assert "node hash" in body
    assert "1 in 256" in body, "the RX count is presented as more certain than it is"
    assert "<h2>identities</h2>" not in overview, "the overview still repeats the counters"


def test_a_run_with_no_identities_says_so_rather_than_showing_an_empty_table() -> None:
    """11.4 / 9.5: empty is a fact, and it is stated."""
    state = stub_state()

    with _client(_app(state)) as client:
        body = client.get("/admin/identities").text

    assert "No identities loaded" in body


# --- consolidate-web-pages: one page per concern ----------------------------

NAVIGATION = (
    "/",
    "/contacts",
    "/chat",
    "/rooms",
    "/admin/identities",
    "/admin/webhooks",
    "/system",
)

FORMER_PAGES = (
    "/modem",
    "/admin/radio",
    "/admin/schema",
    "/admin/rooms",
    "/admin/bots",
    "/admin/channels",
)


def test_the_navigation_names_exactly_the_seven_pages() -> None:
    with _client(_app(stub_state())) as client:
        body = client.get("/").text

    nav = body[body.index('<nav class="nav">') : body.index("</nav>")]
    assert tuple(re.findall(r'href="([^"]*)"', nav)) == NAVIGATION


@pytest.mark.parametrize(
    ("path", "marked"),
    [
        ("/", "/"),
        ("/contacts", "/contacts"),
        ("/chat", "/chat"),
        # A page beneath an entry marks the entry it is beneath.
        ("/chat/aabb/ccdd", "/chat"),
        ("/admin/identities/3", "/admin/identities"),
        # And a page whose path says one thing while the panel says another:
        # channels are administered from chat, rooms from rooms, a bot and an
        # advert from its identity, the gate from system.
        ("/admin/channels/2/rename", "/chat"),
        ("/admin/rooms/1/password", "/rooms"),
        ("/admin/bots/1/mode", "/admin/identities"),
        ("/admin/advert/1/flood", "/admin/identities"),
        ("/admin/transmit", "/system"),
        ("/system", "/system"),
        # Neither of the two pages served without a session carries navigation.
        ("/login", None),
        ("/setup", None),
    ],
)
def test_the_navigation_marks_the_page_being_viewed(path: str, marked: str | None) -> None:
    entries = navigation(path)
    assert tuple(entry.href for entry in entries) == NAVIGATION
    assert [entry.href for entry in entries if entry.current] == ([marked] if marked else [])


def test_exactly_one_navigation_link_is_marked_on_a_page() -> None:
    """Visibly by a class rather than by colour alone, and to assistive
    technology by `aria-current` (`web-dashboard`)."""
    with _client(_app(stub_state())) as client:
        body = client.get("/contacts").text

    nav = body[body.index('<nav class="nav">') : body.index("</nav>")]
    marked = [tag for tag in re.findall(r"<a\b[^>]*>", nav) if 'aria-current="page"' in tag]
    assert len(marked) == 1
    assert 'href="/contacts"' in marked[0]
    assert 'class="current"' in marked[0]


def test_a_former_page_is_not_found_and_nothing_links_to_it() -> None:
    from pathlib import Path

    import sighop.web

    with _client(_app(stub_state())) as client:
        for path in FORMER_PAGES:
            assert client.get(path).status_code == 404, path

    templates = Path(sighop.web.__file__).parent / "templates"
    for template in templates.rglob("*.html"):
        text = template.read_text()
        for path in FORMER_PAGES:
            # The exact page, with or without a query; `/admin/rooms/{id}/…`
            # confirmation views are still served and still linked.
            assert not re.search(rf'href="{re.escape(path)}(\?[^"]*)?"', text), (
                f"{template.name} links to {path}"
            )


def test_the_contact_list_reads_nothing_durable_to_resolve_the_default() -> None:
    """The account's choice travels on the session, so this page keeps reading
    only what the running platform holds in memory. A companion row: a repeater
    row reads its collection selection (repeater-metrics D10), which is not
    what this is about."""

    class _Counting:
        state = "ready"
        degraded = False
        reads = 0

        def __getattr__(self, name: str) -> object:
            _Counting.reads += 1
            raise AssertionError(f"the contacts page read persistence.{name}")

        def as_json(self) -> dict[str, object]:
            return {}

    state = stub_state(stub_names=("first", "second"), persistence=_Counting())  # type: ignore[arg-type]
    state.contacts.restore([dataclasses.replace(_contact("peer"), node_type=NodeType.CHAT)])

    with _client(_app(state)) as client:
        session_of(client).default_entity_id = state.adverts.stubs[0].entity_id
        response = client.get("/contacts")

    assert response.status_code == 200
    assert _Counting.reads == 0


# --- repeater-metrics-collection 6.2 / 6.3 ----------------------------------


def _collect_client(state: StubState):
    """In this event loop, because these pages read the database."""
    import httpx2

    from tests.webfixtures import signed_async_client

    return signed_async_client(
        transport=httpx2.ASGITransport(app=_app(state)), base_url="http://127.0.0.1:8080"
    )


def _repeater(name: str, *, key: bytes | None = None, days_ago: float = 0.1) -> Contact:
    return dataclasses.replace(
        _contact(name, key=key), last_heard=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    )


def _stats(**overrides: object):
    from sighop.protocol.payloads import RepeaterStats

    values: dict[str, object] = {
        "batt_milli_volts": 4012,
        "curr_tx_queue_len": 2,
        "noise_floor": -110,
        "last_rssi": -87,
        "n_packets_recv": 1234,
        "n_packets_sent": 567,
        "total_air_time_secs": 3600,
        "total_up_time_secs": 90000,
        "n_sent_flood": 1,
        "n_sent_direct": 2,
        "n_recv_flood": 3,
        "n_recv_direct": 4,
        "err_events": 0,
        "last_snr": -26,
        "n_direct_dups": 5,
        "n_flood_dups": 6,
        "total_rx_air_time_secs": 7200,
        "n_recv_errors": 9,
    }
    values.update(overrides)
    return RepeaterStats(**values)  # type: ignore[arg-type]


async def test_ticking_a_repeater_selects_it_and_survives_a_reload(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    client = _collect_client(state)
    key = repeater.public_key.hex()

    async with client:
        before = (await client.get("/contacts")).text
        ticked = await client.post(f"/contacts/{key}/collect", data={"collect": "true"})
        after = (await client.get("/contacts")).text

    assert f'hx-post="/contacts/{key}/collect"' in before
    assert ticked.status_code == 200
    assert "checked" in ticked.text and "not polled yet" in ticked.text
    assert "checked" in after[after.index(f"/contacts/{key}/collect") - 200 :][:400]
    selected = await fresh_persistence.repeater_targets.list_keys()
    assert selected.value == frozenset({repeater.public_key})

    async with _collect_client(state) as client:
        unticked = await client.post(f"/contacts/{key}/collect", data={})
    assert "checked" not in unticked.text
    assert (await fresh_persistence.repeater_targets.list_keys()).value == frozenset()


async def test_a_companion_row_has_no_checkbox_and_cannot_be_selected(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    companion = dataclasses.replace(_contact("friend"), node_type=NodeType.CHAT)
    state.contacts.restore([companion])
    client = _collect_client(state)
    key = companion.public_key.hex()

    async with client:
        body = (await client.get("/contacts")).text
        refused = await client.post(f"/contacts/{key}/collect", data={"collect": "true"})

    assert f"/contacts/{key}/collect" not in body
    assert refused.status_code == 400
    assert "only a repeater" in refused.text
    assert (await fresh_persistence.repeater_targets.list_keys()).value == frozenset()


async def test_a_selected_repeater_outside_the_window_is_marked_skipped(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    silent = _repeater("far-away", days_ago=10)
    state.contacts.restore([silent])
    await fresh_persistence.repeater_targets.select(silent.public_key, at=NOW)

    async with _collect_client(state) as client:
        body = (await client.get("/contacts")).text

    assert "skipped — not heard within 3 days" in body


async def test_a_polled_repeater_row_shows_when_and_how_and_links_its_metrics(
    fresh_persistence,
) -> None:
    from sighop.db.repositories import PollOutcome, PollRecord

    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    await fresh_persistence.repeater_targets.select(repeater.public_key, at=NOW)
    await fresh_persistence.repeater_polls.record(
        PollRecord(
            public_key=repeater.public_key,
            started_at=NOW,
            outcome=PollOutcome.LOGIN_UNANSWERED,
            route="FLOOD",
        )
    )

    async with _collect_client(state) as client:
        body = (await client.get("/contacts")).text

    assert "login unanswered" in body
    assert f'href="/contacts/{repeater.public_key.hex()}/metrics"' in body
    assert "2026-09-06 12:00 UTC" in body


async def test_the_metrics_page_of_a_repeater_never_polled_says_when_the_next_cycle_is(
    fresh_persistence,
) -> None:
    state = stub_state(persistence=fresh_persistence, stub_names=("collector",))
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    stored = await fresh_persistence.entities.store(
        name="collector-id",
        identity=generate_identity(),
        secret=b"\x01" * 32,
        node_type=NodeType.CHAT,
    )
    await fresh_persistence.repeater_collection.save(
        enabled=True,
        entity_id=stored.value.id,
        interval_minutes=60,
        recent_days=3,
        retention_days=30,
    )
    await fresh_persistence.repeater_collection.record_cycle(
        started_at=NOW, finished_at=NOW, polled=0, succeeded=0
    )
    await fresh_persistence.repeater_targets.select(repeater.public_key, at=NOW)

    async with _collect_client(state) as client:
        response = await client.get(f"/contacts/{repeater.public_key.hex()}/metrics")

    assert response.status_code == 200
    assert "No poll has been recorded" in response.text
    assert "The next cycle is due" in response.text
    assert "2026-09-06 13:00 UTC" in response.text


async def test_a_failed_latest_poll_shows_the_earlier_status_and_both_in_history(
    fresh_persistence,
) -> None:
    from sighop.db.repositories import PollOutcome, PollRecord
    from sighop.protocol.payloads import NeighbourEntry

    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    known = _repeater("valley")
    twin_a = _repeater("twin-a", key=b"\x99\x88\x77\x66\x55\x44" + b"\x01" * 26)
    twin_b = _repeater("twin-b", key=b"\x99\x88\x77\x66\x55\x44" + b"\x02" * 26)
    state.contacts.restore([repeater, known, twin_a, twin_b])
    earlier = NOW - dt.timedelta(hours=1)
    await fresh_persistence.repeater_polls.record(
        PollRecord(
            public_key=repeater.public_key,
            started_at=earlier,
            outcome=PollOutcome.SUCCEEDED,
            route="DIRECT h1",
            stats=_stats(),
            neighbours_total=2,
            neighbours=(
                NeighbourEntry(prefix=known.public_key[:6], heard_seconds_ago=90, snr=38),
                NeighbourEntry(prefix=b"\x99\x88\x77\x66\x55\x44", heard_seconds_ago=5, snr=-8),
            ),
        )
    )
    await fresh_persistence.repeater_polls.record(
        PollRecord(
            public_key=repeater.public_key,
            started_at=NOW,
            outcome=PollOutcome.LOGIN_UNANSWERED,
            route="DIRECT h1",
        )
    )

    async with _collect_client(state) as client:
        body = (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics")).text

    assert "Collected" in body and "2026-09-06 11:00 UTC" in body
    assert "4.012 V" in body and "-110 dBm" in body and "-6.50 dB" in body
    assert "1234" in body, "a counter is shown as reported"
    history = body[body.index("poll history") :]
    assert history.index("login unanswered") < history.index("succeeded")
    neighbours = body[body.index("<h2>neighbours</h2>") : body.index("poll history")]
    assert "valley" in neighbours and "+9.50" in neighbours and "1 m 30 s" in neighbours
    assert "ambiguous" in neighbours
    assert "twin-a" not in neighbours and "twin-b" not in neighbours


async def test_older_firmware_fields_are_not_reported_rather_than_zero(fresh_persistence) -> None:
    from sighop.db.repositories import PollOutcome, PollRecord

    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("old-timer")
    state.contacts.restore([repeater])
    await fresh_persistence.repeater_polls.record(
        PollRecord(
            public_key=repeater.public_key,
            started_at=NOW,
            outcome=PollOutcome.SUCCEEDED,
            route="FLOOD",
            stats=_stats(total_rx_air_time_secs=None, n_recv_errors=None),
            neighbours_total=0,
        )
    )

    async with _collect_client(state) as client:
        body = (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics")).text

    for label in ("receive airtime", "receive errors"):
        row = body[body.index(f"<td>{label}</td>") :][:120]
        assert "not reported" in row, f"{label} was not shown as not reported"
    assert body.count('absent">not reported<') == 2


async def test_the_metrics_page_of_a_non_repeater_is_not_found(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    companion = dataclasses.replace(_contact("friend"), node_type=NodeType.CHAT)
    state.contacts.restore([companion])

    async with _collect_client(state) as client:
        response = await client.get(f"/contacts/{companion.public_key.hex()}/metrics")

    assert response.status_code == 404


# --- repeater-metrics-history 2.3 / 2.4: battery ----------------------------


async def _status_poll(persistence, key: bytes, at: dt.datetime, **stats: object) -> int:
    from sighop.db.repositories import PollOutcome, PollRecord

    recorded = await persistence.repeater_polls.record(
        PollRecord(
            public_key=key,
            started_at=at,
            outcome=PollOutcome.SUCCEEDED,
            route="FLOOD",
            stats=_stats(**stats),
            neighbours_total=0,
        )
    )
    return recorded.value


async def _failed_poll(persistence, key: bytes, at: dt.datetime) -> int:
    from sighop.db.repositories import PollOutcome, PollRecord

    recorded = await persistence.repeater_polls.record(
        PollRecord(
            public_key=key, started_at=at, outcome=PollOutcome.LOGIN_UNANSWERED, route="FLOOD"
        )
    )
    return recorded.value


async def test_a_repeater_low_on_battery_is_marked_and_a_mains_one_is_not(
    fresh_persistence,
) -> None:
    state = stub_state(persistence=fresh_persistence)
    low = _repeater("solar")
    mains = _repeater("mains")
    state.contacts.restore([low, mains])
    await fresh_persistence.repeater_targets.select(mains.public_key, at=NOW)
    await _status_poll(fresh_persistence, low.public_key, NOW, batt_milli_volts=3600)
    await _status_poll(fresh_persistence, mains.public_key, NOW, batt_milli_volts=0)

    async with _collect_client(state) as client:
        body = (await client.get("/contacts")).text

    assert body.count("low battery") == 1, "the deselected low one only"
    mark = body[body.index("low battery") :][:300]
    assert "3.60 V" in mark and "~25%" in mark and "2026-09-06 12:00 UTC" in mark
    mains_cell = body[body.index(f"/contacts/{mains.public_key.hex()}/collect") :][:600]
    assert "low battery" not in mains_cell


async def test_a_failed_poll_after_a_low_reading_keeps_the_mark(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("solar")
    state.contacts.restore([repeater])
    await fresh_persistence.repeater_targets.select(repeater.public_key, at=NOW)
    earlier = NOW - dt.timedelta(hours=1)
    await _status_poll(fresh_persistence, repeater.public_key, earlier, batt_milli_volts=3660)
    await _failed_poll(fresh_persistence, repeater.public_key, NOW)

    async with _collect_client(state) as client:
        body = (await client.get("/contacts")).text
        ticked = await client.post(
            f"/contacts/{repeater.public_key.hex()}/collect", data={"collect": "true"}
        )

    assert "login unanswered" in body
    mark = body[body.index("low battery") :][:300]
    assert "3.66 V" in mark and "2026-09-06 11:00 UTC" in mark
    assert "low battery" in ticked.text, "the htmx re-render keeps it"


async def test_a_companion_row_carries_no_battery_mark(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    companion = dataclasses.replace(_contact("friend"), node_type=NodeType.CHAT)
    state.contacts.restore([companion])
    await _status_poll(fresh_persistence, companion.public_key, NOW, batt_milli_volts=3500)

    async with _collect_client(state) as client:
        body = (await client.get("/contacts")).text

    assert "low battery" not in body


async def test_the_metrics_page_shows_the_battery_estimate(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("solar")
    mains = _repeater("mains")
    state.contacts.restore([repeater, mains])
    await _status_poll(fresh_persistence, repeater.public_key, NOW, batt_milli_volts=3950)
    await _status_poll(fresh_persistence, mains.public_key, NOW, batt_milli_volts=0)

    async with _collect_client(state) as client:
        body = (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics")).text
        unsensed = (await client.get(f"/contacts/{mains.public_key.hex()}/metrics")).text

    row = body[body.index("<td>battery</td>") :][:120]
    assert "3.950 V" in row and re.search(r"~\d+%", row)
    assert "0.000 V · no battery sensed" in unsensed


# --- repeater-metrics-history 3.2: one poll ---------------------------------


async def test_an_earlier_poll_shows_its_own_status_and_neighbours(fresh_persistence) -> None:
    from sighop.db.repositories import PollOutcome, PollRecord
    from sighop.protocol.payloads import NeighbourEntry

    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    valley = _repeater("valley")
    ridge = _repeater("ridge")
    state.contacts.restore([repeater, valley, ridge])
    stored = await fresh_persistence.entities.store(
        name="collector-id",
        identity=generate_identity(),
        secret=b"\x01" * 32,
        node_type=NodeType.CHAT,
    )
    old = await fresh_persistence.repeater_polls.record(
        PollRecord(
            public_key=repeater.public_key,
            entity_id=stored.value.id,
            started_at=NOW - dt.timedelta(days=3),
            outcome=PollOutcome.SUCCEEDED,
            route="DIRECT h1",
            stats=_stats(n_packets_recv=111),
            neighbours_total=1,
            neighbours=(NeighbourEntry(prefix=valley.public_key[:6], heard_seconds_ago=9, snr=8),),
        )
    )
    await fresh_persistence.repeater_polls.record(
        PollRecord(
            public_key=repeater.public_key,
            started_at=NOW,
            outcome=PollOutcome.SUCCEEDED,
            route="FLOOD",
            stats=_stats(n_packets_recv=999),
            neighbours_total=1,
            neighbours=(NeighbourEntry(prefix=ridge.public_key[:6], heard_seconds_ago=9, snr=8),),
        )
    )
    key = repeater.public_key.hex()

    async with _collect_client(state) as client:
        metrics = (await client.get(f"/contacts/{key}/metrics")).text
        response = await client.get(f"/contacts/{key}/metrics/polls/{old.value}")

    assert f'href="/contacts/{key}/metrics/polls/{old.value}"' in metrics
    body = response.text
    assert response.status_code == 200
    assert "2026-09-03 12:00 UTC" in body and "DIRECT h1" in body and "collector-id" in body
    received = body[body.index("<td>packets received</td>") :][:120]
    assert ">111<" in received
    assert "valley" in body and "ridge" not in body


async def test_a_failed_poll_page_states_it_returned_no_status(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    poll_id = await _failed_poll(fresh_persistence, repeater.public_key, NOW)

    async with _collect_client(state) as client:
        response = await client.get(
            f"/contacts/{repeater.public_key.hex()}/metrics/polls/{poll_id}"
        )

    assert response.status_code == 200
    assert "login unanswered" in response.text and "FLOOD" in response.text
    assert "returned no status" in response.text


async def test_a_poll_under_another_key_or_unknown_is_not_found(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    other = _repeater("valley")
    state.contacts.restore([repeater, other])
    poll_id = await _status_poll(fresh_persistence, repeater.public_key, NOW)

    async with _collect_client(state) as client:
        wrong = await client.get(f"/contacts/{other.public_key.hex()}/metrics/polls/{poll_id}")
        missing = await client.get(
            f"/contacts/{repeater.public_key.hex()}/metrics/polls/{poll_id + 1000}"
        )
        junk = await client.get(f"/contacts/{repeater.public_key.hex()}/metrics/polls/abc")

    assert (wrong.status_code, missing.status_code, junk.status_code) == (404, 404, 404)
    assert "poll not found" in wrong.text


# --- repeater-metrics-history 4.4 / 4.5: charts -----------------------------


def _trends(body: str) -> str:
    return body[body.index('id="trends"') : body.index("<h2>poll history</h2>")]


async def _two_weeks(persistence, key: bytes, now: dt.datetime) -> dt.datetime:
    """Six-hourly polls for 14 days: 4.100 V before the last week, 3.900 V in it."""
    oldest = now - dt.timedelta(days=14)
    for i in range(14 * 4):
        at = oldest + dt.timedelta(hours=6 * i)
        recent = at > now - dt.timedelta(days=7)
        await _status_poll(
            persistence,
            key,
            at,
            batt_milli_volts=3900 if recent else 4100,
            n_packets_recv=1000 + 60 * i,
            total_up_time_secs=100_000 + 21_600 * i,
        )
    return oldest


async def test_the_charts_default_to_the_last_seven_days(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    now = dt.datetime.now(dt.UTC)
    await _two_weeks(fresh_persistence, repeater.public_key, now)
    await _failed_poll(fresh_persistence, repeater.public_key, now - dt.timedelta(hours=1))
    key = repeater.public_key.hex()

    async with _collect_client(state) as client:
        default = _trends((await client.get(f"/contacts/{key}/metrics")).text)
        junk = _trends((await client.get(f"/contacts/{key}/metrics?range=fortnight")).text)

    assert '<strong aria-current="page">last 7 days</strong>' in default
    assert "max 3.900 V" in default and "4.100 V" not in default
    assert "latest 10.0 /h" in default, "60 packets per 6 hours"
    assert "chart-failure-login_unanswered" in default and "1 login unanswered" in default
    assert default.count("<svg") == 8
    assert "<script" not in default
    assert "http://" not in default and "https://" not in default and "//" not in default
    assert '<strong aria-current="page">last 7 days</strong>' in junk


async def test_everything_kept_spans_from_the_oldest_poll(fresh_persistence) -> None:
    from sighop.web.render import utc_short

    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    now = dt.datetime.now(dt.UTC)
    oldest = await _two_weeks(fresh_persistence, repeater.public_key, now)

    async with _collect_client(state) as client:
        body = _trends(
            (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics?range=all")).text
        )

    assert '<strong aria-current="page">everything kept</strong>' in body
    assert "max 4.100 V" in body and "min 3.900 V" in body
    first_label = body[body.index('class="chart-x mono"') :][:300]
    assert utc_short(oldest) in first_label


async def test_a_range_with_no_status_says_so_and_offers_wider_ones(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    now = dt.datetime.now(dt.UTC)
    await _status_poll(fresh_persistence, repeater.public_key, now - dt.timedelta(days=3))
    await _failed_poll(fresh_persistence, repeater.public_key, now - dt.timedelta(hours=2))

    async with _collect_client(state) as client:
        body = _trends(
            (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics?range=24h")).text
        )

    assert "No status was collected in the last 24 hours" in body
    assert "<svg" not in body
    assert 'href="?range=7d#trends"' in body and 'href="?range=all#trends"' in body


async def test_the_retention_note_reads_kept_forever(fresh_persistence) -> None:
    state = stub_state(persistence=fresh_persistence)
    repeater = _repeater("hilltop")
    state.contacts.restore([repeater])
    await _status_poll(fresh_persistence, repeater.public_key, NOW)

    async with _collect_client(state) as client:
        days = (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics")).text
        await fresh_persistence.repeater_collection.save(
            enabled=False, entity_id=None, interval_minutes=60, recent_days=3, retention_days=None
        )
        forever = (await client.get(f"/contacts/{repeater.public_key.hex()}/metrics")).text

    assert "(30 days)" in days
    assert "(kept forever)" in forever and "days)" not in forever


def test_a_zero_hop_route_shows_as_one_hop_through_the_preferred_repeater() -> None:
    """preferred-first-hop 5.3: the table shows the route a send will use."""
    state = stub_state()
    contact = _contact("neighbour")
    state.contacts.restore([contact])
    _learn(state, PathKey.for_public_key(contact.public_key), b"", 0)

    unset = contact_rows(state.contacts, state.pipeline.paths)[0].route
    assert unset is not None
    assert (unset.zero_hop, unset.rewritten) == (True, False)

    state.pipeline.paths.preferred_first_hop = b"\xab" * 32
    route = contact_rows(state.contacts, state.pipeline.paths)[0].route
    assert route is not None
    assert (route.hop_count, route.path, route.rewritten) == (1, "ab", True)
    assert route.text == "1 hop via ab, through the preferred first hop"

    with _client(_app(state)) as client:
        body = client.get("/contacts").text
    assert "via preferred" in body
    assert "direct, zero hops" not in body
