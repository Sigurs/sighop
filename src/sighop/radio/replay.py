"""Replay a capture file as the event stream the modem produces.

The inverse of `capture.py`, and it lives beside it because the two share one
record format and have to change together. Design D1: replay is a *source*
for the same pipeline the live link drives, not a second decoder — a replay
path that is not the live path proves nothing about the live path, and the
value here is that a bug reproduced from a capture file is a bug in the code
that runs on air.

Events carry the timestamp recorded in the file, and replay runs as fast as it
can read: reproducing an overnight capture's inter-frame gaps in real time is
nobody's workflow, and nothing needs the pacing until the scheduler exists
(design D10).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sighop.logging import Logger, get_logger
from sighop.radio.capture import CAPTURE_META_KIND, TX_FRAME_KIND
from sighop.radio.modem import ModemEvent, RxEvent, RxMeta, UnparsedEvent


@dataclass(frozen=True, slots=True)
class UnreadableLine:
    """A capture line that could not be read back, and where it was.

    Reported rather than skipped: a capture file is evidence, and a line
    quietly dropped from a replay is a frame that silently stops existing.
    """

    line_number: int
    reason: str
    raw: str

    def __str__(self) -> str:
        return f"line {self.line_number}: {self.reason}"


@dataclass(slots=True)
class CaptureReplay:
    """A capture file, read back as modem events.

    `provenance` is the file's `capture_meta` header, or None for a file
    written before headers existed — which the two milestone 0 captures are,
    and which any partially written file will also look like.
    """

    path: Path
    provenance: dict | None = None
    unreadable: list[UnreadableLine] = field(default_factory=list)
    transmitted_skipped: int = 0
    """`tx_frame` records passed over: frames sighop sent, not receptions."""

    _header_line: int | None = None
    _logger: Logger | None = None

    @classmethod
    def open(
        cls, path: Path, *, logger: Logger | None = None
    ) -> CaptureReplay:
        """Read the file's provenance, so a caller can report it before the
        first frame is replayed.
        """
        replay = cls(path=path, _logger=logger or get_logger(component="replay"))
        replay._read_provenance()
        return replay

    def _read_provenance(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return  # reported as unreadable when the events are read
                if isinstance(record, dict) and record.get("kind") == CAPTURE_META_KIND:
                    self.provenance = record
                    self._header_line = line_number
                return

    async def events(self) -> AsyncIterator[ModemEvent]:
        """Yield the file's frames as modem events, in file order.

        Async to match `Modem.events()`, not because the reading is: the file
        is local and unpaced, so there is nothing here to await.
        """
        for event in self.read():
            yield event

    def read(self) -> Iterator[ModemEvent]:
        """The synchronous form, for callers with no event loop."""
        self.unreadable = []
        self.transmitted_skipped = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if line_number == self._header_line:
                    continue  # provenance, not a frame
                event = self._event_for(line_number, line)
                if event is not None:
                    yield event

    def _event_for(self, line_number: int, line: str) -> ModemEvent | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # A truncated final line looks exactly like this: the capturing
            # process was killed mid-write.
            return self._report_unreadable(line_number, f"invalid JSON: {exc}", line)
        if not isinstance(record, dict):
            return self._report_unreadable(line_number, "record is not a JSON object", line)

        kind = record.get("kind")
        received_at = self._timestamp(record)
        try:
            if kind == "rx_frame":
                return RxEvent(
                    packet=bytes.fromhex(record["raw_hex"]),
                    rx_meta=_rx_meta(record.get("rx_meta")),
                    received_at=received_at,
                )
            if kind == "unparsed":
                return UnparsedEvent(
                    raw=bytes.fromhex(record["raw_hex"]),
                    reason=record["reason"],
                    received_at=received_at,
                )
            if kind == TX_FRAME_KIND:
                # A frame sighop transmitted, in a capture from a transmitting
                # run. Skipped rather than yielded: replaying our own packets as
                # receptions would invent traffic the radio never heard, and
                # milestone 2's property is that a replay reproduces every
                # *reception* exactly. Counted so a caller can say how many were
                # passed over, and never reported as an unreadable line — it is a
                # deliberate record kind, not a corrupt one.
                self.transmitted_skipped += 1
                return None
            if kind == CAPTURE_META_KIND:
                return self._report_unreadable(
                    line_number,
                    "capture_meta record after the first line; a capture file "
                    "carries at most one header and it comes first",
                    line,
                )
            return self._report_unreadable(line_number, f"unknown record kind {kind!r}", line)
        except (KeyError, ValueError) as exc:
            return self._report_unreadable(line_number, f"malformed {kind} record: {exc}", line)

    def _timestamp(self, record: dict) -> dt.datetime | None:
        raw = record.get("ts")
        if not isinstance(raw, str):
            return None
        try:
            return dt.datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _report_unreadable(
        self, line_number: int, reason: str, line: str
    ) -> ModemEvent | None:
        """Record and log an unreadable line. Returns None: there is no event
        for a line we could not read, and the callers `return` that None.
        """
        entry = UnreadableLine(line_number=line_number, reason=reason, raw=line.rstrip("\n"))
        self.unreadable.append(entry)
        if self._logger is not None:
            self._logger.error(
                "capture_line_unreadable",
                capture_file=str(self.path),
                line_number=line_number,
                reason=reason,
            )
        return None


def _rx_meta(value: object) -> RxMeta | None:
    """A null RxMeta stays null: a frame that arrived without correlated
    metadata has no SNR, which is not the same as an SNR of zero.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"rx_meta must be an object or null, got {type(value).__name__}")
    return RxMeta(snr_db=float(value["snr_db"]), rssi_dbm=int(value["rssi_dbm"]))
