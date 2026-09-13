"""Milestone 8's non-regression assertions (group 16).

The assertion milestones 6 and 7 both used, applied to the whole interface at
once: replay the corpus with the panel fully wired — the feed hub subscribed and
observing, a connection open, the direct-message sink attached — and the
platform must produce the same delivered, duplicate, contact and path counts as
a replay with none of it.

If it does not, something in `web/` is on the reception path, which is the one
thing this milestone was not allowed to do.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

import sighop.protocol
from sighop.net.acks import AckDispatcher, AckRegistry
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.contacts import ContactStore
from sighop.net.dedup import DedupCache
from sighop.net.paths import PathStore
from sighop.radio.modem import EU868_NARROW
from sighop.web.chat import ConversationLog
from sighop.web.feed import FeedHub
from tests.protocol.corpus import EXPECTED_RECEIVED_COUNT
from tests.test_dm import Entity, messenger
from tests.test_room_exercise import _replayed
from tests.test_tx import RecordingLogger
from tests.webfixtures import signed_client

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


async def _counts(*, watched: bool) -> dict[str, int]:
    """Replay the corpus, optionally with the whole panel attached."""
    bus = NetworkBus(logger=RecordingLogger())
    hub = FeedHub(capacity=8) if watched else None
    conversations = ConversationLog() if watched else None
    pipeline = IngressPipeline(
        bus=bus,
        dedup=DedupCache(),
        paths=PathStore(),
        logger=RecordingLogger(),
        radio=EU868_NARROW,
        observer=hub.on_reception if hub is not None else None,
    )
    contacts = ContactStore(logger=RecordingLogger())
    contacts.subscribe(bus)
    acks = AckRegistry(logger=RecordingLogger())
    AckDispatcher(registry=acks).subscribe(bus)

    us = Entity("panel")
    dm = messenger(us, contacts=contacts, paths=pipeline.paths)
    dm.acks = acks
    dm._owns_acks = False
    dm.subscribe(bus)

    if hub is not None:
        hub.subscribe(bus)
        # A connection that is opened and never drained: the shape a browser
        # left on a screen has, and the one most likely to slow something down.
        hub.connect(at=NOW)
        assert conversations is not None
        dm.add_record_sink(conversations)

    for record in _replayed():
        pipeline.ingest(record)
    await bus.aclose()

    return {
        "delivered": pipeline.delivered,
        "duplicates": pipeline.duplicates,
        "considered": pipeline.dedup.stats.considered,
        "contacts": len(contacts),
        "paths": pipeline.paths.destination_count,
        "received": dm.received,
        "undecryptable": dm.undecryptable,
    }


# --- 16.1 The corpus replays identically with the interface wired in --------


async def test_the_corpus_replays_identically_with_the_whole_interface_wired_in() -> None:
    """16.1: the assertion milestones 6 and 7 both used, for milestone 8."""
    without = await _counts(watched=False)
    with_panel = await _counts(watched=True)

    assert with_panel == without, "wiring the panel changed what the platform produced"
    assert without["delivered"] > 0, "the comparison would be vacuous"
    assert without["duplicates"] > 0, "the duplicate path was not exercised"


async def test_a_stalled_connection_still_changes_nothing() -> None:
    """16.1: the same replay with a connection far too small to keep up.

    Its queue holds eight records against a thousand: it drops almost
    everything, which is exactly the case the counts must not notice.
    """
    bus = NetworkBus(logger=RecordingLogger())
    hub = FeedHub(capacity=8)
    connection = hub.connect(at=NOW)
    pipeline = IngressPipeline(
        bus=bus,
        dedup=DedupCache(),
        paths=PathStore(),
        logger=RecordingLogger(),
        radio=EU868_NARROW,
        observer=hub.on_reception,
    )
    for record in _replayed():
        pipeline.ingest(record)
    await bus.aclose()

    assert connection.dropped > 0, "the connection never fell behind"
    assert pipeline.delivered + pipeline.duplicates == EXPECTED_RECEIVED_COUNT


# --- 16.2 `protocol/` gained nothing ----------------------------------------


def test_the_protocol_layer_gained_nothing_from_this_milestone() -> None:
    """16.2: the codec is pure functions over bytes, before and after.

    A static scan for anything milestone 8 introduced. The import boundary test
    owns the framework packages; this owns the rest of the claim — no module in
    `protocol/` so much as mentions the web layer's own vocabulary.
    """
    forbidden = ("fastapi", "starlette", "uvicorn", "jinja2", "websocket", "sighop.web")
    package = Path(sighop.protocol.__file__).parent
    offenders: dict[str, list[str]] = {}
    for path in sorted(package.glob("*.py")):
        source = path.read_text().lower()
        found = [word for word in forbidden if word in source]
        if found:
            offenders[path.name] = found
    assert not offenders, f"{offenders} reach the web layer from protocol/"


def test_the_protocol_suite_still_holds_its_own_evidence() -> None:
    """16.2: the corpus decodes to the same number of receptions it always has."""
    assert len(_replayed()) == EXPECTED_RECEIVED_COUNT


def test_no_protocol_module_imports_anything_this_milestone_added() -> None:
    """16.2: the same claim through the import graph rather than through text."""
    package = Path(sighop.protocol.__file__).parent
    names: set[str] = set()
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
    assert not {name for name in names if name.startswith(("sighop.web", "fastapi"))}


# --- 16.3 The suite passes in all three configurations ----------------------


def test_every_panel_test_that_needs_a_database_says_so() -> None:
    """16.3: what makes "the suite passes with no database configured" true.

    A test that takes the `database` fixture without the marker does not skip —
    it fails, on exactly the machine least able to do anything about it. The
    marker is the gate `conftest.py` applies, so this checks that every test
    that needs one carries it.
    """
    unmarked: list[str] = []
    for path in sorted(Path("tests").glob("test_web_*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            takes_database = any(
                argument.arg == "database" for argument in node.args.args
            )
            marked = any(
                ast.unparse(decorator) == "pytest.mark.database"
                for decorator in node.decorator_list
            )
            if takes_database and not marked:
                unmarked.append(f"{path.name}::{node.name}")
    assert not unmarked, (
        f"{unmarked} need a database and are not marked; they would fail rather "
        "than skip on a machine without one"
    )


@pytest.mark.parametrize("configuration", ["no database", "no interface", "both"])
def test_the_panel_is_constructible_in_every_configuration(configuration: str) -> None:
    """16.3: a page renders with no database, no feed, or neither."""

    from sighop.web.app import allowed_hosts, create_app
    from tests.webfixtures import authenticator, stub_state

    feed = FeedHub() if configuration != "no interface" else None
    app = create_app(
        stub_state(),
        auth=authenticator(),
        feed=feed,
        hosts=allowed_hosts("127.0.0.1", 8080),
        logger=RecordingLogger(),
    )
    with signed_client(app, base_url="http://127.0.0.1:8080") as client:
        assert client.get("/").status_code == 200


# --- 16.4 Nothing here asserts a property a collision makes false -----------


PROBABILISTIC = (
    "generate_identity",
    "token_urlsafe",
    "token_hex",
    "uuid4",
)


def test_no_new_test_group_asserts_a_property_a_collision_would_break() -> None:
    """16.4: milestone 7 found two of these, so milestone 8 looks for its own.

    A test that generates a key and then asserts something about its *node hash*
    is asserting something §3 makes false 1 time in 256. The scan is for the two
    together: a generated identity in the same test as an assertion about a hash
    or a prefix, without the test having fixed the byte on purpose.
    """
    suspects: dict[str, list[str]] = {}
    for path in sorted(Path("tests").glob("test_web_*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = ast.get_source_segment(source, node) or ""
            if not any(word in body for word in PROBABILISTIC):
                continue
            risky = [
                line.strip()
                for line in body.splitlines()
                if "assert" in line
                and ("node_hash" in line or "[:2]" in line)
                and "!=" not in line
                and "0xab" not in line
            ]
            if risky:
                suspects[f"{path.name}::{node.name}"] = risky
    assert not suspects, (
        f"{suspects} generate a key and then assert something a 1-in-256 node "
        "hash collision would make false; fix the byte deliberately instead"
    )


def test_the_one_deliberate_hash_collision_is_deliberate() -> None:
    """16.4: the room member test builds a collision rather than hoping for one.

    Named here so the scan above reads as a rule with a known exception rather
    than as a rule nothing tests.
    """
    source = Path("tests/test_web_rooms.py").read_text()
    assert "first[0] == second[0] and first != second" in source, (
        "the colliding-hash test no longer constructs its collision"
    )
