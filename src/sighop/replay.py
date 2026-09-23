"""The build's parity harness: `python -m sighop.replay <capture>`.

The test suite runs on the build host's C library and the image ships another
(`build-script`). This module is the one place the platform's own behaviour —
decoding, signature verification, deduplication and path learning — is run on
the library the image ships, by rendering a committed capture inside the image
and comparing it byte for byte with the same capture rendered on the host.

It is not a command line. It takes exactly one positional argument, opens no
modem and no database, and is not on `PATH`. It cannot use `Runtime` for the
same reason it needs no database: a node requires one, and this is not a node —
it drives the decode path the radio drives, directly, and nothing else.

Its output is deterministic by construction. No periodic status line is ever
printed (one depends on the wall clock), no packet id is rendered (one is minted
per reception), and the summary is counts only. Two runs over one capture, on
one platform or two, print the same bytes or the gate is right to fail.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from sighop.logging import configure_logging
from sighop.monitor.render import (
    Summary,
    render_detail_line,
    render_frame_line,
    render_replay_startup,
    render_summary,
)
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.dedup import DedupCache
from sighop.net.paths import PathStore
from sighop.net.rx import AdvertOutcome, RxRecord, decode_event
from sighop.protocol.crypto import VerifiedAdvert
from sighop.radio.modem import RadioParams
from sighop.radio.replay import CaptureReplay

USAGE = "usage: python -m sighop.replay <capture.jsonl> — exactly one capture path"


def main(argv: Sequence[str] | None = None, out: IO[str] | None = None) -> int:
    """Render one capture. 0 when every line was read, 1 when any was not, 2 on usage."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if len(arguments) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    path = Path(arguments[0])
    if not path.is_file():
        print(f"no capture file at {path}", file=sys.stderr)
        return 2
    # Wide events carry an instance id and the wall-clock time, so they go to
    # standard error: standard output is the rendering, and it must be the same
    # bytes on every run.
    configure_logging(stream=sys.stderr)
    return asyncio.run(render_capture(path, out if out is not None else sys.stdout))


async def render_capture(path: Path, out: IO[str]) -> int:
    """Every reception in the capture, through the decode path the radio drives."""
    replay = CaptureReplay.open(path)
    radio = replay_radio(replay.provenance)
    bus = NetworkBus()
    pipeline = IngressPipeline(bus=bus, dedup=DedupCache(), paths=PathStore(), radio=radio)
    counts = _Counts()

    _write(out, render_replay_startup(replay.provenance, str(path)))
    async for event in replay.events():
        record = decode_event(event)
        pipeline.ingest(record)
        counts.add(record)
        _write(out, render_frame_line(record))
        detail = render_detail_line(record)
        if detail:
            _write(out, detail)
    await bus.aclose()

    _write(
        out,
        render_summary(
            counts.summary(
                duplicates=pipeline.duplicates, paths_learned=pipeline.paths.destination_count
            )
        ),
    )
    # Reported, never skipped: a line dropped quietly is a frame that stops
    # existing (`capture-replay`). Standard error, so the rendered output the
    # gate compares stays the rendering of what *was* read.
    for unreadable in replay.unreadable:
        print(f"unreadable capture {unreadable}", file=sys.stderr)
    return 1 if replay.unreadable else 0


def replay_radio(provenance: dict | None) -> RadioParams | None:
    """The radio the capture was recorded on, when its header says.

    Airtime is computed against the parameters the frames were actually
    received under, not against today's configuration — and a header that does
    not say is `None`, never a guess.
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


class _Counts:
    """What the summary line reports: raw receptions, not deduplicated ones."""

    def __init__(self) -> None:
        self.frames = 0
        self.decode_failures = 0
        self.adverts_verified = 0
        self.adverts_failed = 0
        self.node_hashes: set[int] = set()

    def add(self, record: RxRecord) -> None:
        self.frames += 1
        if record.failed:
            self.decode_failures += 1
        if isinstance(record.outcome, AdvertOutcome):
            if isinstance(record.outcome.verification, VerifiedAdvert):
                self.adverts_verified += 1
            else:
                self.adverts_failed += 1
        if record.src_hash is not None:
            self.node_hashes.add(record.src_hash)

    def summary(self, *, duplicates: int, paths_learned: int) -> Summary:
        return Summary(
            frames=self.frames,
            decode_failures=self.decode_failures,
            adverts_verified=self.adverts_verified,
            adverts_failed=self.adverts_failed,
            node_hashes=len(self.node_hashes),
            duplicates=duplicates,
            paths_learned=paths_learned,
        )


def _write(out: IO[str], text: str) -> None:
    out.write(text + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
