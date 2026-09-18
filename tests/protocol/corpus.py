"""Loader for the captured-frame regression corpus.

The capture files and their provenance — a `capture_meta` header line from
milestone 2 onward, a paired `.meta.json` sidecar for the two milestone 0
files — are read-only evidence (DESIGN.md §12): this module opens them for
reading and never writes to them, and nothing in the test suite regenerates
them.

See `CORPUS.md` for what the corpus does and does not cover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

CAPTURES_DIR = Path(__file__).resolve().parents[2] / "captures"

CAPTURE_FILES = (
    "2026-09-02.jsonl",
    "2026-09-03.jsonl",
    "2026-09-04.jsonl",
    "2026-09-04-02.jsonl",
    "2026-09-04-03.jsonl",
    "2026-09-05.jsonl",
    "2026-09-04-first-transmit.jsonl",
    "2026-09-06-room-server.jsonl",
)

# Files from milestone 2 onward carry their provenance in-band, as a
# `capture_meta` first line; the two milestone 0 files predate that record and
# carry theirs in a paired `.meta.json` sidecar (DESIGN.md §12).
SIDECAR_PROVENANCE_FILES = ("2026-09-02.jsonl", "2026-09-03.jsonl")

RX_FRAME_KIND = "rx_frame"
TX_FRAME_KIND = "tx_frame"
"""Frames sighop transmitted. They decode through the same codecs as receptions
and belong in the corpus, but they must stay out of any measurement whose
subject is what the mesh sent us — a duplicate rate computed over our own
transmissions would be measuring the wrong thing (milestone 4)."""

# Recorded expectations. Asserted, never regenerated from a failing run.
EXPECTED_FRAME_COUNT = 1093
"""Every `rx_frame`/`tx_frame` record: 1058 received, 35 transmitted."""

EXPECTED_RECEIVED_COUNT = 1059
"""Receptions as the *live pipeline* (`CaptureReplay`/`decode_event`) counts
them — one more than `EXPECTED_FRAME_COUNT`'s 1058 `rx_frame` records, because
the room-server session's capture carries one `unparsed` line (a stray
`RxMeta` at modem startup) that the pipeline turns into its own `ModemUnparsed`
record. `CorpusFrame`-based counting (`EXPECTED_FRAME_COUNT`) skips that kind
entirely, so the two totals no longer sum to the same thing; that's the
capture, not a bug in either counter."""
EXPECTED_TRANSMITTED_COUNT = 35
"""3 frames from the first-transmit exercise (the DM, its acknowledgement of the
peer's DM, and one zero-hop advert), plus 32 from milestone 6's live room-server
exercise: adverts, room logins, post acknowledgements and pushes."""

EXPECTED_FRAMES_PER_FILE = {
    "2026-09-02.jsonl": 152,
    "2026-09-03.jsonl": 199,
    "2026-09-04.jsonl": 56,
    "2026-09-04-02.jsonl": 2,
    "2026-09-04-03.jsonl": 33,
    "2026-09-05.jsonl": 555,
    "2026-09-04-first-transmit.jsonl": 6,
    "2026-09-06-room-server.jsonl": 90,
}

FIRST_TRANSMIT_FILE = "2026-09-04-first-transmit.jsonl"
"""The one session in the corpus whose ciphertext sighop holds a key for.

Its record indices, which the known-answer tests name so the vector and its
provenance cannot drift apart:

* 1 — the peer's zero-hop advert
* 2 — sighop's first transmission (a `TXT_MSG` to the peer)
* 3 — the peer's `TXT_MSG` to sighop, produced by stock `companion_radio`
  v1.17.1-d929643 and decryptable with `tests/fixtures/burned-first-transmit.json`
* 4 — the peer's 6-byte acknowledgement of record 2
* 5 — sighop's 4-byte acknowledgement of record 3
* 6 — sighop's zero-hop advert
"""

PEER_DM_INDEX = 3
SIGHOP_DM_INDEX = 2
PEER_ACK_INDEX = 4
SIGHOP_ACK_INDEX = 5


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
    path = CAPTURES_DIR / name
    if not path.is_file():
        raise CorpusError(
            f"capture file {path} is missing; the corpus is the regression "
            "evidence for the protocol layer and the suite must not pass without it"
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
        raise CorpusError(f"capture file {path} contained no frame records")
    return frames


@cache
def load_corpus() -> tuple[CorpusFrame, ...]:
    """Every frame record across every capture file, in capture order.

    Both directions: receptions, and the frames sighop transmitted in the
    first-transmit exercise. `received_frames()` is what any measurement of what
    the mesh sent us must use.
    """
    frames: list[CorpusFrame] = []
    for name in CAPTURE_FILES:
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


def first_transmit_frame(index: int) -> CorpusFrame:
    """One record of the first-transmit session, by its index in the file.

    Named rather than searched for, so a known-answer test and the capture it
    reads cannot drift apart (`protocol-corpus`, milestone 4).
    """
    for frame in load_corpus():
        if frame.capture_file == FIRST_TRANSMIT_FILE and frame.index == index:
            return frame
    raise CorpusError(f"{FIRST_TRANSMIT_FILE} has no record at index {index}")
