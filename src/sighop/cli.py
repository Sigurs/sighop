"""sighop command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import IO

from sighop.logging import configure_logging, get_logger
from sighop.monitor.render import render_replay_startup, render_startup
from sighop.monitor.run import MonitorRun
from sighop.radio.capture import CaptureRun, CaptureWriter
from sighop.radio.kiss import KissTransport, serial_connector
from sighop.radio.modem import EU868_NARROW, Modem
from sighop.radio.probe import ProbeResult
from sighop.radio.replay import CaptureReplay

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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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

    return 0


async def _monitor(args: argparse.Namespace) -> int:
    if args.replay is not None:
        return await _run_monitor_replay(args.replay)
    return await _run_monitor_live(args.device, args.radio_preset, args.capture)


if __name__ == "__main__":
    sys.exit(main())
