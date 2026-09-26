"""Loader for the synthetic regression corpus.

The corpus is three generated capture files under `tests/corpus/`, written by
`tests.protocol.synthetic` from a fixed seed and committed. They are read-only
generated artefacts (`protocol-corpus`): this module opens them for reading and
never writes to them, and only `python -m tests.protocol.generate_corpus` does.
`test_synthetic_corpus.py` regenerates the corpus in memory and fails if the
committed files differ.

The corpus replaced a recording of live mesh traffic. A generated corpus proves
the decoders agree with sighop's own encoders and with the expectations below; it
does not prove interoperability with another MeshCore implementation. See
`CORPUS.md` for what it does and does not cover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from sighop.protocol.packet import PayloadType, decode
from sighop.protocol.result import DecodeFailure

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

AMBIENT = "synthetic-ambient.jsonl"
"""Receive-only mesh: adverts of every form, routing, discovery, third-party mail.
What a test takes when it wants "a realistic stream of receptions"."""
CHANNELS = "synthetic-channels.jsonl"
"""Group text on the Public channel and a second channel, and group data."""
EXCHANGE = "synthetic-exchange.jsonl"
"""sighop and a synthetic peer: both directions, both acknowledgement forms, a room."""

CORPUS_FILES = (AMBIENT, CHANNELS, EXCHANGE)

RX_FRAME_KIND = "rx_frame"
TX_FRAME_KIND = "tx_frame"
"""Frames sighop transmitted. They decode through the same codecs as receptions
and belong in the corpus, but they must stay out of any measurement whose
subject is what the mesh sent us — a duplicate rate computed over our own
transmissions would be measuring the wrong thing (milestone 4)."""

# Recorded expectations. Typed in from a reviewed run of the generator's manifest
# (`python -m tests.protocol.generate_corpus`), asserted, never regenerated.
EXPECTED_FRAME_COUNT = 282
"""Every `rx_frame`/`tx_frame` record: 264 received, 18 transmitted."""

EXPECTED_RECEIVED_COUNT = 265
"""Receptions as the *live pipeline* (`CaptureReplay`/`decode_event`) counts
them — one more than the 264 `rx_frame` records, because the exchange file carries
one `unparsed` line (a stray `RxMeta` at modem startup) that the pipeline turns
into its own `ModemUnparsed` record. `CorpusFrame`-based counting skips that kind
entirely, so the two totals no longer sum to the same thing."""
EXPECTED_TRANSMITTED_COUNT = 18
"""The frames sighop sends in the exchange file: its own adverts, direct messages,
acknowledgements, and the room's login answers, responses and pushes."""

EXPECTED_FRAMES_PER_FILE = {
    AMBIENT: 180,
    CHANNELS: 63,
    EXCHANGE: 39,
}


@dataclass(frozen=True, slots=True)
class CorpusFrame:
    """One frame record, identified well enough to name it in a failure."""

    capture_file: str
    index: int
    timestamp: str
    raw: bytes
    snr_db: float | None
    rssi_dbm: int | None
    transmitted: bool = False
    """True for a frame sighop sent. Excluded from reception-derived measures."""

    @property
    def location(self) -> str:
        return f"{self.capture_file}[{self.index}] {self.timestamp}"

    def describe(self) -> str:
        direction = "tx" if self.transmitted else "rx"
        return f"{self.location} ({direction}): {self.raw.hex()}"


class CorpusError(RuntimeError):
    """The corpus is missing or malformed — never a silent pass on no frames."""


def _load_file(name: str) -> list[CorpusFrame]:
    path = CORPUS_DIR / name
    if not path.is_file():
        raise CorpusError(
            f"corpus file {path} is missing; the corpus is the regression "
            "evidence for the protocol layer and the suite must not pass without it "
            "(regenerate it with `uv run python -m tests.protocol.generate_corpus`)"
        )
    frames: list[CorpusFrame] = []
    with path.open("r", encoding="utf-8") as handle:  # read-only, never "a" or "w"
        for index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            kind = record.get("kind")
            if kind not in (RX_FRAME_KIND, TX_FRAME_KIND):
                continue
            meta = record.get("rx_meta") or {}
            frames.append(
                CorpusFrame(
                    capture_file=name,
                    index=index,
                    timestamp=record["ts"],
                    raw=bytes.fromhex(record["raw_hex"]),
                    snr_db=meta.get("snr_db"),
                    rssi_dbm=meta.get("rssi_dbm"),
                    transmitted=kind == TX_FRAME_KIND,
                )
            )
    if not frames:
        raise CorpusError(f"corpus file {path} contained no frame records")
    return frames


@cache
def load_corpus() -> tuple[CorpusFrame, ...]:
    """Every frame record across every corpus file, in file order.

    Both directions: receptions, and the frames sighop transmitted in the
    exchange file. `received_frames()` is what any measurement of what the mesh
    sent us must use.
    """
    frames: list[CorpusFrame] = []
    for name in CORPUS_FILES:
        loaded = _load_file(name)
        expected = EXPECTED_FRAMES_PER_FILE[name]
        if len(loaded) != expected:
            raise CorpusError(f"{name} holds {len(loaded)} frame records, expected {expected}")
        frames.extend(loaded)
    if len(frames) != EXPECTED_FRAME_COUNT:
        raise CorpusError(f"corpus holds {len(frames)} frames, expected {EXPECTED_FRAME_COUNT}")
    transmitted = sum(1 for frame in frames if frame.transmitted)
    if transmitted != EXPECTED_TRANSMITTED_COUNT:
        raise CorpusError(
            f"corpus holds {transmitted} transmitted frames, expected {EXPECTED_TRANSMITTED_COUNT}"
        )
    return tuple(frames)


def received_frames() -> tuple[CorpusFrame, ...]:
    """Only the frames the mesh sent us — what a duplicate rate is measured over."""
    return tuple(frame for frame in load_corpus() if not frame.transmitted)


def transmitted_frames() -> tuple[CorpusFrame, ...]:
    """Only the frames sighop put on the air."""
    return tuple(frame for frame in load_corpus() if frame.transmitted)


def first_received_frame(payload_type: PayloadType, *, hops: int | None = None) -> bytes:
    """The raw bytes of the first reception of a payload type, for a test that
    needs one frame that decodes rather than a hand-built shape.

    `hops`, when given, picks the first with that hop count.
    """
    for frame in received_frames():
        packet = decode(frame.raw)
        if isinstance(packet, DecodeFailure) or packet.payload_type is not payload_type:
            continue
        if hops is None or packet.hop_count == hops:
            return frame.raw
    raise CorpusError(f"the corpus holds no received {payload_type.name} frame (hops={hops})")
