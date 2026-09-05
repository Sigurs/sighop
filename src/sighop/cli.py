"""sighop command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import IO

from sighop.keystore import KeyfileError, create_keyfile, load_keyfile
from sighop.logging import configure_logging, get_logger
from sighop.monitor.render import render_replay_startup, render_startup
from sighop.monitor.run import MonitorRun
from sighop.net.airtime import cross_check_airtime
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.protocol.payloads import NodeType
from sighop.radio.capture import CaptureRun, CaptureWriter
from sighop.radio.kiss import KissTransport, serial_connector
from sighop.radio.modem import EU868_NARROW, Modem, RadioParams
from sighop.radio.probe import ProbeResult
from sighop.radio.replay import CaptureReplay
from sighop.runtime import (
    DEFAULT_PEER_WAIT_SECONDS,
    DEFAULT_STATUS_INTERVAL_SECONDS,
    Runtime,
    RuntimeConfig,
)

RADIO_PRESETS = {"eu868-narrow": EU868_NARROW}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sighop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture", help="dump raw modem frames with RxMeta and timestamps to a file"
    )
    capture.add_argument(
        "--device", required=True, help="stable serial device path (/dev/serial/by-id/...)"
    )
    capture.add_argument("--out", required=True, type=Path, help="capture output file (JSONL)")
    capture.add_argument(
        "--radio-preset",
        default="eu868-narrow",
        choices=sorted(RADIO_PRESETS),
        help="radio parameters to apply via SetRadio (default: %(default)s)",
    )
    capture.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="also append wide-event logs here (default: <out stem>.log next to --out)",
    )

    monitor = subparsers.add_parser(
        "monitor", help="decode and print received packets, live or from a capture file"
    )
    source = monitor.add_mutually_exclusive_group(required=True)
    source.add_argument("--device", help="stable serial device path (/dev/serial/by-id/...)")
    source.add_argument(
        "--replay", type=Path, help="capture file to replay through the same decode path"
    )
    monitor.add_argument(
        "--radio-preset",
        default="eu868-narrow",
        choices=sorted(RADIO_PRESETS),
        help="radio parameters to apply via SetRadio, live only (default: %(default)s)",
    )
    monitor.add_argument(
        "--capture",
        type=Path,
        default=None,
        help="also write the monitored frames to this capture file (JSONL)",
    )
    monitor.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="append wide-event logs here instead of standard output",
    )

    run = subparsers.add_parser(
        "run",
        help="run the platform: decode, dedup, learn paths, and schedule transmissions",
    )
    run_source = run.add_mutually_exclusive_group(required=True)
    run_source.add_argument("--device", help="stable serial device path (/dev/serial/by-id/...)")
    run_source.add_argument(
        "--replay", type=Path, help="capture file to replay through the same pipeline"
    )
    run.add_argument(
        "--radio-preset",
        default="eu868-narrow",
        choices=sorted(RADIO_PRESETS),
        help="radio parameters to apply via SetRadio, live only (default: %(default)s)",
    )
    run.add_argument(
        "--enable-transmit",
        action="store_true",
        help=(
            "open the receive-only gate. WITHOUT THIS FLAG NOTHING IS TRANSMITTED: "
            "packets are scheduled, charged against the duty-cycle budget and "
            "logged, then dropped at the hand-off to the modem"
        ),
    )
    run.add_argument(
        "--duty-cycle-ceiling",
        type=float,
        default=DEFAULT_CEILING_FRACTION,
        help=(
            "fraction of an hour sighop may transmit for (default: %(default)s). "
            "On EU 868 the 10%% default is a regulatory limit, not a tuning knob"
        ),
    )
    run.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_INTERVAL_SECONDS,
        help="seconds between status lines (default: %(default)s)",
    )
    run.add_argument(
        "--stub",
        action="append",
        default=[],
        metavar="NAME",
        dest="stubs",
        help=(
            "add an in-memory advert stub with this name; repeatable. Keys are "
            "generated per process and are never persisted"
        ),
    )
    run.add_argument(
        "--entity",
        action="append",
        default=[],
        type=Path,
        metavar="KEYFILE",
        dest="entities",
        help=(
            "load a persistent entity identity from this keyfile; repeatable. "
            "Two keyfiles whose public keys share a first byte fail startup"
        ),
    )
    run.add_argument(
        "--peer",
        default=None,
        metavar="NAME|HEX-PREFIX",
        help="the contact to send --send to, by exact name or hex public key prefix",
    )
    run.add_argument(
        "--send",
        default=None,
        metavar="TEXT",
        dest="send_text",
        help=(
            "send this text to --peer once, then keep running. The outcome — "
            "acknowledged, unacknowledged, or dropped — is always reported"
        ),
    )
    run.add_argument(
        "--allow-flood",
        action="store_true",
        help=(
            "permit sending to a peer no route is known to. WITHOUT THIS FLAG a "
            "send with no known route is refused: a flood is rebroadcast by every "
            "repeater in the mesh, and must not be reachable by mistyping a name"
        ),
    )
    run.add_argument(
        "--advert-zero-hop",
        default=None,
        metavar="NAME",
        dest="zero_hop_advert",
        help=(
            "emit exactly one zero-hop advert for this entity at startup. It "
            "reaches direct neighbours and stops there, and creates no recurring "
            "zero-hop schedule"
        ),
    )
    run.add_argument(
        "--peer-wait",
        type=float,
        default=DEFAULT_PEER_WAIT_SECONDS,
        help=(
            "seconds to wait for --peer's advert before reporting it unknown "
            "(default: %(default)s). The run continues receiving either way"
        ),
    )
    run.add_argument(
        "--advert-override-seconds",
        type=float,
        default=None,
        help=(
            "advert faster than the 24 h floor, for a dry run. Requires "
            "--advert-override-expires-in, which is capped at 24 h"
        ),
    )
    run.add_argument(
        "--advert-override-expires-in",
        type=float,
        default=None,
        help="seconds until the advert override auto-reverts to the 24 h floor",
    )
    run.add_argument(
        "--dedup-ttl",
        type=float,
        default=DEFAULT_TTL_SECONDS,
        help="duplicate cache time-to-live in seconds (default: %(default)s)",
    )
    run.add_argument(
        "--dedup-max-entries",
        type=int,
        default=DEFAULT_MAX_ENTRIES,
        help="duplicate cache entry cap (default: %(default)s)",
    )
    run.add_argument(
        "--capture",
        type=Path,
        default=None,
        help=(
            "also write the received frames to this capture file (JSONL), with the "
            "same provenance header `sighop capture` writes. Live sources only"
        ),
    )
    run.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="append wide-event logs here instead of standard output",
    )

    keys = subparsers.add_parser(
        "keys", help="create and inspect entity identity keyfiles"
    )
    key_actions = keys.add_subparsers(dest="keys_command", required=True)
    keys_new = key_actions.add_parser(
        "new", help="generate an entity keyfile and print its public key"
    )
    keys_new.add_argument("--name", required=True, help="the entity's advertised name")
    keys_new.add_argument(
        "--out", required=True, type=Path, help="keyfile to create (never overwritten)"
    )
    keys_new.add_argument(
        "--node-type",
        default=NodeType.CHAT.name,
        choices=[node_type.name for node_type in NodeType],
        help="the node type the entity adverts as (default: %(default)s)",
    )
    keys_new.add_argument(
        "--burned",
        action="store_true",
        help=(
            "mark the keyfile as a published test vector that must never be used "
            "on air again. For an identity committed to the repository as a "
            "fixture; a real entity generates its own"
        ),
    )
    keys_show = key_actions.add_parser(
        "show", help="print a keyfile's name, node type, public key and node hash"
    )
    keys_show.add_argument("keyfile", type=Path, help="the keyfile to inspect")

    return parser


async def _run_capture(device: str, out: Path, radio_preset: str, log_file: Path) -> int:
    with log_file.open("a", encoding="utf-8") as log_stream:
        configure_logging(log_stream)
        logger = get_logger(component="cli")
        transport = KissTransport(serial_connector(device))
        modem = Modem(transport, radio_params=RADIO_PRESETS[radio_preset])
        run = CaptureRun(modem, out)
        run.install_signal_handlers()
        logger.info(
            "capture_starting",
            device=device,
            out=str(out),
            radio_preset=radio_preset,
            log_file=str(log_file),
        )
        try:
            await run.run()
        except Exception:
            logger.error("capture_failed", device=device, out=str(out))
            raise
        logger.info("capture_stopped")
    return 0


async def _run_monitor_live(
    device: str,
    radio_preset: str,
    capture: Path | None,
    out: IO[str] | None = None,
) -> int:
    """Live monitor: open the link, probe it, then render every frame."""
    transport = KissTransport(serial_connector(device))
    modem = Modem(transport, radio_params=RADIO_PRESETS[radio_preset])

    async def startup() -> str:
        await modem.probe_ready.wait()
        return render_startup(modem.probe_result)

    async def probe_result() -> ProbeResult | None:
        await modem.probe_ready.wait()
        return modem.probe_result

    writer = CaptureWriter(capture) if capture is not None else None
    if writer is not None:
        writer.open()
    try:
        run = MonitorRun(
            modem.events(),
            startup=startup,
            out=out,
            capture_writer=writer,
            capture_probe=probe_result,
            reconnects=lambda: modem.reconnect_count,
            reboots=lambda: modem.reboot_count,
        )
        run.install_signal_handlers()
        await run.run()
    finally:
        if writer is not None:
            writer.close()
    return 0


async def _run_monitor_replay(path: Path, out: IO[str] | None = None) -> int:
    """Replay: no device is opened, and the file's own provenance is the
    startup line — or its absence is, for the headerless milestone 0 captures.
    """
    replay = CaptureReplay.open(path)

    async def startup() -> str:
        return render_replay_startup(replay.provenance, str(path))

    run = MonitorRun(replay.events(), startup=startup, out=out)
    run.install_signal_handlers()
    await run.run()
    if replay.unreadable:
        for line in replay.unreadable:
            print(f"unreadable {line}", file=sys.stderr)
    return 0


def _run_config(args: argparse.Namespace) -> RuntimeConfig:
    return RuntimeConfig(
        transmit_enabled=args.enable_transmit,
        status_interval=args.status_interval,
        dedup_ttl_seconds=args.dedup_ttl,
        dedup_max_entries=args.dedup_max_entries,
        ceiling_fraction=args.duty_cycle_ceiling,
        stub_names=tuple(args.stubs),
        advert_override_seconds=args.advert_override_seconds,
        advert_override_expires_in=args.advert_override_expires_in,
        entity_keyfiles=tuple(args.entities),
        peer=args.peer,
        send_text=args.send_text,
        allow_flood=args.allow_flood,
        zero_hop_advert=args.zero_hop_advert,
        peer_wait_seconds=args.peer_wait,
    )


# --- Key management (design D12) --------------------------------------------


def _keys_new(args: argparse.Namespace, out: IO[str]) -> int:
    """Create a keyfile and print the public key another node's contact list needs."""
    try:
        keyfile = create_keyfile(
            args.out,
            args.name,
            node_type=NodeType[args.node_type],
            burned=args.burned,
        )
    except KeyfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"created {keyfile.path}", file=out)
    print(f"name       {keyfile.name}", file=out)
    print(f"node_type  {NodeType(keyfile.node_type).name}", file=out)
    print(f"public_key {keyfile.public_key.hex()}", file=out)
    print(f"node_hash  0x{keyfile.node_hash:02x}", file=out)
    for warning in keyfile.warnings:
        print(f"!! {warning}", file=out)
    return 0


def _keys_show(args: argparse.Namespace, out: IO[str]) -> int:
    """Inspect an identity. The seed is not printed, and there is no flag for it."""
    try:
        keyfile = load_keyfile(args.keyfile)
    except KeyfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    node_type = (
        keyfile.node_type.name
        if isinstance(keyfile.node_type, NodeType)
        else f"type_{keyfile.node_type}"
    )
    print(f"keyfile    {keyfile.path}", file=out)
    print(f"name       {keyfile.name}", file=out)
    print(f"node_type  {node_type}", file=out)
    print(f"public_key {keyfile.public_key.hex()}", file=out)
    print(f"node_hash  0x{keyfile.node_hash:02x}", file=out)
    for warning in keyfile.warnings:
        print(f"!! {warning}", file=out)
    return 0


async def _run_live(args: argparse.Namespace, out: IO[str] | None = None) -> int:
    """The platform against a live link. The gate is closed unless asked for."""
    transport = KissTransport(serial_connector(args.device))
    modem = Modem(transport, radio_params=RADIO_PRESETS[args.radio_preset])
    events = modem.events()

    async def probe_result() -> ProbeResult | None:
        await modem.probe_ready.wait()
        return modem.probe_result

    writer = CaptureWriter(args.capture) if args.capture is not None else None
    if writer is not None:
        writer.open()
    try:
        runtime = Runtime(
            source=events,
            startup=lambda: _live_startup(modem, runtime),
            config=_run_config(args),
            sender=modem,
            out=out,
            capture_writer=writer,
            capture_probe=probe_result,
        )
        runtime.install_signal_handlers()
        await runtime.run()
    finally:
        if writer is not None:
            writer.close()
    return 0


async def _live_startup(modem: Modem, runtime: Runtime) -> str:
    """Wait for the probe, then adopt the board's own radio readback.

    The readback rather than the configured preset: the budget is enforced
    against what the radio is actually doing, and refuses to guess when the
    board answered nothing.
    """
    await modem.probe_ready.wait()
    probe_result = modem.probe_result
    runtime.set_radio(probe_result.observed_radio if probe_result is not None else None)
    if probe_result is not None:
        await cross_check_airtime(modem, runtime.radio or modem.radio_params)
    return render_startup(probe_result)


async def _run_replay(args: argparse.Namespace, out: IO[str] | None = None) -> int:
    """The same pipeline over recorded frames — no device, no transmission."""
    replay = CaptureReplay.open(args.replay)
    config = _run_config(args)
    radio = _replay_radio(replay.provenance) or RADIO_PRESETS[args.radio_preset]

    runtime = Runtime(
        source=replay.events(),
        startup=lambda: _replay_startup(replay, args.replay),
        config=config,
        radio=radio,
        out=out,
    )
    runtime.install_signal_handlers()
    await runtime.run()
    return 0


async def _replay_startup(replay: CaptureReplay, path: Path) -> str:
    return render_replay_startup(replay.provenance, str(path))


def _replay_radio(provenance: dict | None) -> RadioParams | None:
    """The radio the capture was recorded on, when its header says.

    A replayed run computes airtime against the parameters the frames were
    actually received under, not against today's configuration.
    """
    if not provenance:
        return None
    radio = provenance.get("radio")
    value = radio.get("value") if isinstance(radio, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        return RadioParams(
            freq_hz=int(value["freq_hz"]),
            bw_hz=int(value["bw_hz"]),
            sf=int(value["sf"]),
            cr=int(value["cr"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def main(argv: list[str] | None = None, out: IO[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stream = out if out is not None else sys.stdout

    if args.command == "keys":
        configure_logging(stream=sys.stderr)
        if args.keys_command == "new":
            return _keys_new(args, stream)
        return _keys_show(args, stream)

    if args.command == "capture":
        log_file = args.log_file or args.out.with_name(args.out.stem + ".log")
        return asyncio.run(_run_capture(args.device, args.out, args.radio_preset, log_file))

    if args.command == "monitor":
        # Rendered lines own standard output; the wide events go to stderr and
        # to the log file when one is given. Both records of the session
        # exist, and neither is parsed out of the other.
        if args.log_file is not None:
            with args.log_file.open("a", encoding="utf-8") as log_stream:
                configure_logging(log_stream, stream=sys.stderr)
                return asyncio.run(_monitor(args))
        configure_logging(stream=sys.stderr)
        return asyncio.run(_monitor(args))

    if args.command == "run":
        if args.log_file is not None:
            with args.log_file.open("a", encoding="utf-8") as log_stream:
                configure_logging(log_stream, stream=sys.stderr)
                return asyncio.run(_run(args))
        configure_logging(stream=sys.stderr)
        return asyncio.run(_run(args))

    return 0


async def _monitor(args: argparse.Namespace) -> int:
    if args.replay is not None:
        return await _run_monitor_replay(args.replay)
    return await _run_monitor_live(args.device, args.radio_preset, args.capture)


async def _run(args: argparse.Namespace) -> int:
    if args.replay is not None:
        if args.capture is not None:
            # Re-recording a replay would produce a capture whose header
            # describes a probe that never happened. A capture is evidence of a
            # session on the air (DESIGN.md §12), not a copy of a file.
            print(
                "--capture records a live session; it cannot be combined with --replay",
                file=sys.stderr,
            )
            return 2
        return await _run_replay(args)
    return await _run_live(args)


if __name__ == "__main__":
    sys.exit(main())
