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
)

# The milestone 2 files carry their provenance in-band, as a `capture_meta`
# first line; the two milestone 0 files predate that record and carry theirs in
# a paired `.meta.json` sidecar (DESIGN.md §12).
SIDECAR_PROVENANCE_FILES = ("2026-09-02.jsonl", "2026-09-03.jsonl")

# Recorded expectations. Asserted, never regenerated from a failing run.
EXPECTED_FRAME_COUNT = 442
EXPECTED_FRAMES_PER_FILE = {
    "2026-09-02.jsonl": 152,
    "2026-09-03.jsonl": 199,
    "2026-09-04.jsonl": 56,
    "2026-09-04-02.jsonl": 2,
    "2026-09-04-03.jsonl": 33,
}


@dataclass(frozen=True, slots=True)
class CorpusFrame:
    """One `rx_frame` record, identified well enough to name it in a failure."""

    capture_file: str
    index: int
    timestamp: str
    raw: bytes
    snr_db: float | None
    rssi_dbm: int | None

    @property
    def location(self) -> str:
        return f"{self.capture_file}[{self.index}] {self.timestamp}"

    def describe(self) -> str:
        return f"{self.location}: {self.raw.hex()}"


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
            if record.get("kind") != "rx_frame":
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
                )
            )
    if not frames:
        raise CorpusError(f"capture file {path} contained no rx_frame records")
    return frames


@cache
def load_corpus() -> tuple[CorpusFrame, ...]:
    """Every `rx_frame` record across every capture file, in capture order."""
    frames: list[CorpusFrame] = []
    for name in CAPTURE_FILES:
        loaded = _load_file(name)
        expected = EXPECTED_FRAMES_PER_FILE[name]
        if len(loaded) != expected:
            raise CorpusError(
                f"{name} holds {len(loaded)} rx_frame records, expected {expected}"
            )
        frames.extend(loaded)
    if len(frames) != EXPECTED_FRAME_COUNT:
        raise CorpusError(
            f"corpus holds {len(frames)} frames, expected {EXPECTED_FRAME_COUNT}"
        )
    return tuple(frames)
