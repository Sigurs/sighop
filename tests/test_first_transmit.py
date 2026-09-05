"""`sighop keys`, the entity-carrying `run`, and the exchange (milestone 4).

The loopback at the bottom is the whole milestone wired together: two persistent
identities, the scheduler, a sender that hands every transmitted packet straight
back as a reception, and a message that composes, decrypts, is acknowledged and
resolves. It proves the pieces compose — and, as `tests/test_dm.py` says, it
proves nothing about the wire format, because both ends are sighop.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
from collections.abc import AsyncIterator

import pytest

from sighop.cli import build_parser, main
from sighop.keystore import NodeHashCollisionError, create_keyfile
from sighop.net.paths import LearnedPath, PathKey
from sighop.protocol.identity import generate_identity
from sighop.radio.modem import EU868_NARROW, ModemEvent, RxEvent, RxMeta, TransmitDone
from sighop.runtime import Runtime, RuntimeConfig
from tests.protocol.corpus import CAPTURES_DIR
from tests.test_contacts import verified_advert
from tests.test_runtime import _events, _never_ends, _startup
from tests.test_tx import ManualClock, RecordingLogger

CAPTURE = CAPTURES_DIR / "2026-09-04-03.jsonl"


# --- `sighop keys` (task 5.1) ----------------------------------------------


def test_keys_new_writes_a_keyfile_and_prints_the_public_key(tmp_path) -> None:
    out = io.StringIO()
    path = tmp_path / "entity.json"

    code = main(["keys", "new", "--name", "skogen", "--out", str(path)], out=out)

    text = out.getvalue()
    document = json.loads(path.read_text())
    assert code == 0
    assert document["public_key_hex"] in text
    assert "skogen" in text
    assert "CHAT" in text


def test_keys_new_refuses_to_overwrite(tmp_path, capsys) -> None:
    path = tmp_path / "entity.json"
    main(["keys", "new", "--name", "one", "--out", str(path)], out=io.StringIO())

    code = main(["keys", "new", "--name", "two", "--out", str(path)], out=io.StringIO())

    assert code == 2
    assert "will not be overwritten" in capsys.readouterr().err


def test_keys_show_prints_the_identity_and_never_the_seed(tmp_path) -> None:
    path = tmp_path / "entity.json"
    keyfile = create_keyfile(path, "skogen")
    out = io.StringIO()

    code = main(["keys", "show", str(path)], out=out)

    text = out.getvalue()
    assert code == 0
    assert keyfile.public_key.hex() in text
    assert f"0x{keyfile.node_hash:02x}" in text
    assert keyfile.identity.seed.hex() not in text
    assert "seed" not in text.lower()


def test_there_is_no_flag_that_prints_a_seed() -> None:
    """Exporting private material must be a distinct, explicit act."""
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["keys", "show", "x.json", "--seed"])


# --- The `run` surface (tasks 5.2, 5.5) ------------------------------------


def test_run_accepts_repeated_entities_a_peer_and_a_message() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--replay",
            str(CAPTURE),
            "--entity",
            "a.json",
            "--entity",
            "b.json",
            "--peer",
            "skogen",
            "--send",
            "hej",
        ]
    )

    assert [str(path) for path in args.entities] == ["a.json", "b.json"]
    assert args.peer == "skogen"
    assert args.send_text == "hej"


def test_flooding_is_off_unless_asked_for() -> None:
    parser = build_parser()

    assert parser.parse_args(["run", "--replay", str(CAPTURE)]).allow_flood is False
    assert (
        parser.parse_args(["run", "--replay", str(CAPTURE), "--allow-flood"]).allow_flood
        is True
    )


def runtime(
    source: AsyncIterator[ModemEvent],
    *,
    config: RuntimeConfig,
    sender=None,
    clock: ManualClock | None = None,
    out: io.StringIO | None = None,
) -> Runtime:
    return Runtime(
        source=source,
        startup=_startup,
        config=config,
        sender=sender,
        radio=EU868_NARROW,
        clock=clock or ManualClock(),
        out=out or io.StringIO(),
        logger=RecordingLogger(),
    )


async def test_two_colliding_keyfiles_fail_startup_naming_both(tmp_path) -> None:
    first = generate_identity()
    while True:
        second = generate_identity()
        if second.node_hash == first.node_hash:
            break
    one = create_keyfile(tmp_path / "one.json", "one", identity=first)
    two = create_keyfile(tmp_path / "two.json", "two", identity=second)

    with pytest.raises(NodeHashCollisionError) as excinfo:
        runtime(
            _events(CAPTURE),
            config=RuntimeConfig(
                status_interval=3600, advert_tick=3600, entity_keyfiles=(one.path, two.path)
            ),
        )

    message = str(excinfo.value)
    assert str(one.path) in message
    assert str(two.path) in message


async def test_a_loaded_identity_is_listed_as_persistent(tmp_path) -> None:
    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")
    out = io.StringIO()
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, entity_keyfiles=(keyfile.path,)
        ),
        out=out,
    )

    await run.run()

    text = out.getvalue()
    assert "skogen" in text
    assert "persistent" in text
    assert "contacts: in memory only" in text
    assert "do not survive the process" in text


async def test_persistent_and_ephemeral_entities_are_distinguished(tmp_path) -> None:
    keyfile = create_keyfile(tmp_path / "entity.json", "persistent-one")
    out = io.StringIO()
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(keyfile.path,),
            stub_names=("ephemeral-one",),
        ),
        out=out,
    )

    await run.run()

    line = next(
        line for line in out.getvalue().splitlines() if line.startswith("stubs: ")
    )
    assert "persistent-one" in line and "persistent" in line
    assert "ephemeral-one" in line and "ephemeral" in line


async def test_an_unresolvable_peer_keeps_the_run_receiving(tmp_path) -> None:
    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")
    out = io.StringIO()
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(keyfile.path,),
            peer="nobody",
            send_text="hej",
            peer_wait_seconds=0.0,
        ),
        out=out,
    )

    await run.run()

    text = out.getvalue()
    assert "peer 'nobody' is unknown" in text
    assert "the run continues receiving" in text
    assert run.pipeline.delivered > 0, "the run stopped receiving"
    assert run.scheduler.stats.submitted == 0, "something was queued for an unknown peer"


# --- The startup banner with the gate open (task 5.7) ----------------------


async def test_the_banner_names_every_originating_identity_when_the_gate_is_open(
    tmp_path,
) -> None:
    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")
    out = io.StringIO()
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            transmit_enabled=True,
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(keyfile.path,),
        ),
        out=out,
    )

    await run.run()

    text = out.getvalue()
    assert "packets WILL be transmitted on air" in text
    assert "duty-cycle ceiling 10.0% (360 s/hour)" in text
    assert keyfile.public_key.hex() in text
    assert f"node_hash=0x{keyfile.node_hash:02x}" in text


# --- The exchange (tasks 5.6, 5.9, 5.10) -----------------------------------


def seed_zero_hop(run: Runtime, public_key: bytes, at: dt.datetime) -> None:
    """State the route the peer's advert would have taught us."""
    run.pipeline.paths._insert(
        PathKey.for_public_key(public_key),
        LearnedPath(b"", 1, 0, 9.0, at, "seed"),
    )


class LoopbackSender:
    """Hands every transmitted packet straight back as a reception.

    Both ends are sighop, so this confirms the pieces compose and nothing about
    the wire format — section 8 of the change is what does that.
    """

    def __init__(self, queue: asyncio.Queue, clock: ManualClock) -> None:
        self.queue = queue
        self.clock = clock
        self.sent: list[bytes] = []

    async def send_packet(self, packet: bytes, *, timeout: float) -> TransmitDone:
        self.sent.append(packet)
        self.queue.put_nowait(
            RxEvent(
                packet=packet,
                rx_meta=RxMeta(snr_db=9.0, rssi_dbm=-40),
                received_at=self.clock.now(),
            )
        )
        return TransmitDone(success=True)


async def test_two_entities_exchange_a_message_end_to_end(tmp_path) -> None:
    alice = create_keyfile(tmp_path / "alice.json", "alice")
    bob = create_keyfile(
        tmp_path / "bob.json", "bob", avoid_node_hashes=frozenset({alice.node_hash})
    )
    clock = ManualClock()
    queue: asyncio.Queue = asyncio.Queue()
    sender = LoopbackSender(queue, clock)
    out = io.StringIO()

    async def source() -> AsyncIterator[ModemEvent]:
        while True:
            yield await queue.get()

    run = runtime(
        source(),
        config=RuntimeConfig(
            transmit_enabled=True,
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(alice.path, bob.path),
            peer="bob",
            send_text="hej fran alice",
            peer_wait_seconds=10.0,
        ),
        sender=sender,
        clock=clock,
        out=out,
    )
    # The contacts and route the peer's advert would have supplied.
    run.contacts.observe_advert(
        verified_advert(bob.identity, "bob"), at=clock.now()
    )
    run.contacts.observe_advert(
        verified_advert(alice.identity, "alice"), at=clock.now()
    )
    seed_zero_hop(run, bob.public_key, clock.now())
    seed_zero_hop(run, alice.public_key, clock.now())

    task = asyncio.create_task(run.run())
    for _ in range(4000):
        if "dm delivered" in out.getvalue():
            break
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 5)

    text = out.getvalue()
    assert "-> dm sent" in text, "the message never reached the air"
    assert "dm from claimed 'bob'" not in text, "the sender was rendered as the recipient"
    assert "dm from claimed" in text, "the decrypted message named no claimed sender"
    assert "to bob:" in text
    assert "'hej fran alice'" in text
    assert "matched attempt 0" in text
    assert "dm delivered to 'bob' after 1 attempt(s)" in text
    assert run.messenger.received == 1
    assert run.messenger.undecryptable == 0
    # One TXT_MSG and one ACK: the exchange is two packets, not four.
    assert len(sender.sent) == 2


async def test_a_transmitting_run_captures_the_frames_it_sent(tmp_path) -> None:
    """A capture from a transmitting run holds both directions (task 5.9)."""
    from sighop.radio.capture import TX_FRAME_KIND, CaptureWriter
    from sighop.radio.probe import FirmwareVersion, ProbeResult

    alice = create_keyfile(tmp_path / "alice.json", "alice")
    bob = create_keyfile(
        tmp_path / "bob.json", "bob", avoid_node_hashes=frozenset({alice.node_hash})
    )
    capture_path = tmp_path / "session.jsonl"
    writer = CaptureWriter(capture_path)
    writer.open()
    clock = ManualClock()
    queue: asyncio.Queue = asyncio.Queue()
    sender = LoopbackSender(queue, clock)
    out = io.StringIO()

    async def source() -> AsyncIterator[ModemEvent]:
        while True:
            yield await queue.get()

    async def probe() -> ProbeResult:
        return ProbeResult(
            configured_radio=EU868_NARROW,
            device_name="Heltec V4 OLED",
            radio=EU868_NARROW,
            tx_power_dbm=0,
            firmware_version=FirmwareVersion(version=1, reserved=0),
            battery_mv=4279,
            mcu_temp_tenths_c=320,
            sensors_raw=b"",
        )

    run = Runtime(
        source=source(),
        startup=_startup,
        config=RuntimeConfig(
            transmit_enabled=True,
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(alice.path, bob.path),
            peer="bob",
            send_text="hej",
            peer_wait_seconds=10.0,
        ),
        sender=sender,
        radio=EU868_NARROW,
        clock=clock,
        out=out,
        logger=RecordingLogger(),
        capture_writer=writer,
        capture_probe=probe,
    )
    run.contacts.observe_advert(
        verified_advert(bob.identity, "bob"), at=clock.now()
    )
    run.contacts.observe_advert(
        verified_advert(alice.identity, "alice"), at=clock.now()
    )
    seed_zero_hop(run, bob.public_key, clock.now())
    seed_zero_hop(run, alice.public_key, clock.now())

    task = asyncio.create_task(run.run())
    for _ in range(4000):
        if "dm delivered" in out.getvalue():
            break
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 5)
    writer.close()

    records = [json.loads(line) for line in capture_path.read_text().splitlines()]
    assert records[0]["kind"] == "capture_meta"
    transmitted = [r for r in records if r["kind"] == TX_FRAME_KIND]
    received = [r for r in records if r["kind"] == "rx_frame"]
    assert len(transmitted) == 2, "the run's own frames are missing from the capture"
    assert len(received) == 2, "the loopback receptions are missing"
    assert all(r["airtime_ms"] is not None for r in transmitted)
    # The same bytes both ways: a transmitted record decodes through the same
    # codecs as a received one, which is what lets the corpus hold both.
    assert {r["raw_hex"] for r in transmitted} == {r["raw_hex"] for r in received}


# --- The one-shot zero-hop advert (task 5.4) -------------------------------


async def test_a_one_shot_zero_hop_advert_is_requested_once(tmp_path) -> None:
    from sighop.net.bus import PriorityClass
    from sighop.protocol.packet import RouteType, decode

    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")
    clock = ManualClock()
    submitted: list = []

    run = runtime(
        _never_ends(),
        config=RuntimeConfig(
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(keyfile.path,),
            zero_hop_advert="skogen",
        ),
        clock=clock,
    )
    original_submit = run.bus.submit

    def recording(submission):
        submitted.append(submission)
        return original_submit(submission)

    run.adverts.submit = recording
    stub = run.adverts.stubs[0]
    next_flood_before = stub.next_flood_at

    task = asyncio.create_task(run.run())
    for _ in range(50):
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 5)

    assert len(submitted) == 1, "a one-shot advert created a recurring schedule"
    submission = submitted[0]
    assert submission.priority is PriorityClass.ADVERT
    assert submission.origin == "advert_zero_hop"
    assert decode(submission.packet).route_type is RouteType.DIRECT
    assert stub.next_flood_at == next_flood_before, "the flood schedule moved"
    assert stub.zero_hop_interval_seconds == 0.0, "a recurring zero-hop was created"
    assert run.adverts.zero_hop_requests == 1


async def test_a_gated_run_composes_charges_and_suppresses_a_message(tmp_path) -> None:
    """The dry run of task 6.3: everything happens except the transmission.

    With the gate closed a message is composed, charged against the duty-cycle
    budget and dropped at the hand-off, exactly as milestone 3's adverts are —
    which is what makes a gated run's log comparable with a transmitting one.
    """
    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")
    peer = generate_identity()
    clock = ManualClock()
    out = io.StringIO()

    run = runtime(
        _never_ends(),
        config=RuntimeConfig(
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(keyfile.path,),
            peer="peer",
            send_text="hej",
            peer_wait_seconds=10.0,
        ),
        clock=clock,
        out=out,
    )
    run.contacts.observe_advert(verified_advert(peer, "peer"), at=clock.now())
    seed_zero_hop(run, peer.public_key, clock.now())

    task = asyncio.create_task(run.run())
    for _ in range(4000):
        if "not sent (gate closed)" in out.getvalue():
            break
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 5)

    text = out.getvalue()
    assert "-> dm not sent (gate closed)" in text
    assert "DIRECT h0" in text, "the learned zero-hop route was not used"
    assert run.scheduler.stats.suppressed >= 1
    assert run.scheduler.stats.transmitted == 0
    assert run.scheduler.budget.used_ms(clock.now()) > 0, "the message was not charged"


async def test_a_one_shot_advert_waits_for_the_radio_readback(tmp_path) -> None:
    """Regression: the live exercise dropped its first advert to this.

    The one-shot fired before the probe had delivered a readback, so the
    scheduler — correctly — refused to compute airtime and dropped the packet.
    The refusal was right; the ordering was not. Nothing may be queued before
    startup has adopted the board's own parameters.
    """
    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")
    clock = ManualClock()
    probed = asyncio.Event()
    out = io.StringIO()

    async def slow_startup() -> str:
        # Stands in for a probe: the radio is unknown until this resolves.
        await probed.wait()
        run.set_radio(EU868_NARROW)
        return "probed"

    run = Runtime(
        source=_never_ends(),
        startup=slow_startup,
        config=RuntimeConfig(
            transmit_enabled=True,
            status_interval=3600,
            advert_tick=3600,
            entity_keyfiles=(keyfile.path,),
            zero_hop_advert="skogen",
        ),
        sender=LoopbackSender(asyncio.Queue(), clock),
        radio=None,
        clock=clock,
        out=out,
        logger=RecordingLogger(),
    )

    task = asyncio.create_task(run.run())
    for _ in range(50):
        await asyncio.sleep(0)
    assert run.scheduler.stats.submitted == 0, "an advert was queued before the readback"

    probed.set()
    for _ in range(200):
        if run.scheduler.stats.transmitted:
            break
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 5)

    assert run.scheduler.stats.transmitted == 1
    assert run.scheduler.stats.dropped == 0, "the advert was dropped for want of a readback"
