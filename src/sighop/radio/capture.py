"""Persist modem RX/unparsed events to a JSONL capture file.

One JSON object per line, appended and flushed immediately so a process
interruption loses at most the in-flight record. See design.md's "Capture
file format" decision for the record shape.

A capture file begins with a `capture_meta` provenance record
built from the startup probe (DESIGN.md §12, design D6): the corpus outlives
the session that recorded it, and a fixture whose recording conditions live
only in someone's memory decays into an untrustworthy one. Values are what the
board reported; a value it did not report is written as an explicit null with
its reason, never as the configured value.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import IO

from sighop.logging import commit_hash, package_version
from sighop.radio.modem import ModemEvent, RxEvent
from sighop.radio.probe import ProbeResult

CAPTURE_META_KIND = "capture_meta"

TX_FRAME_KIND = "tx_frame"
"""Frames sighop transmitted, kept distinct from the `rx_frame` records the mesh
sent us (milestone 4, `protocol-corpus`). They decode through the same codecs and
belong in the corpus; they must stay out of any measurement whose subject is what
the mesh sent — a duplicate rate computed over our own transmissions would be
measuring the wrong thing."""


def _sighop_provenance() -> dict[str, str]:
    return {"version": package_version(), "commit_hash": commit_hash()}


def capture_meta_record(probe_result: ProbeResult | None) -> dict:
    """The header record: what the board said it is, plus what recorded it.

    `probe_result` is None only when the link never became ready — recorded as
    such rather than papered over, for the same reason an unanswered
    sub-command is.
    """
    record: dict[str, object] = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "kind": CAPTURE_META_KIND,
        "sighop": _sighop_provenance(),
    }
    if probe_result is None:
        record["probe"] = None
        record["probe_absent_reason"] = "the link never reached the ready state"
        return record
    record.update(probe_result.as_json())
    return record


def _record_for(event: ModemEvent) -> dict:
    ts = (event.received_at or dt.datetime.now(dt.UTC)).isoformat()
    if isinstance(event, RxEvent):
        rx_meta = None
        if event.rx_meta is not None:
            rx_meta = {"snr_db": event.rx_meta.snr_db, "rssi_dbm": event.rx_meta.rssi_dbm}
        return {"ts": ts, "kind": "rx_frame", "raw_hex": event.packet.hex(), "rx_meta": rx_meta}
    return {"ts": ts, "kind": "unparsed", "raw_hex": event.raw.hex(), "reason": event.reason}


class CaptureWriter:
    """Owns a capture file: one provenance header, then one record per event.

    An event written before the header is available is *held*, not dropped and
    not written ahead of it — a capture file's header is its first line, and a
    frame can easily arrive while the board is still being probed. Holding
    rather than blocking matters: the frame loop must keep running for the
    probe's own responses to be routed at all.

    The runtime writes its `SIGHOP_CAPTURE_FILE` through this same writer, so
    every capture sighop produces is one dialect of one format.
    """

    def __init__(self, out_path: Path) -> None:
        self._out_path = out_path
        self._file: IO[str] | None = None
        self._append_only = False
        self._started = False
        self._held: list[ModemEvent | dict] = []
        """Receptions and transmissions in one list, so the order they happened
        in is the order the file records — a capture whose own frames arrived
        interleaved is what makes an exchange readable afterwards."""

        self.rx_count = 0
        self.unparsed_count = 0
        self.tx_count = 0

    @property
    def started(self) -> bool:
        return self._started

    def open(self) -> None:
        # A file that already holds records keeps the header it has, if any:
        # a capture file carries at most one, and it is the first line.
        self._append_only = self._out_path.is_file() and self._out_path.stat().st_size > 0
        self._file = self._out_path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def start(self, probe_result: ProbeResult | None) -> None:
        """Write the provenance header and release anything held behind it."""
        if self._started:
            return
        self._started = True
        if not self._append_only:
            self._write(capture_meta_record(probe_result))
        held, self._held = self._held, []
        for item in held:
            self._write_item(item)

    def write(self, event: ModemEvent) -> None:
        if not self._started:
            self._held.append(event)
            return
        self._write_item(event)

    def write_transmitted(
        self,
        packet: bytes,
        *,
        at: dt.datetime | None = None,
        packet_id: str | None = None,
        airtime_ms: float | None = None,
    ) -> None:
        """Record a frame sighop put on the air.

        A run with transmission enabled produces a capture holding both
        directions, which is the first time the corpus can contain a packet
        whose plaintext we know (milestone 4, `protocol-corpus`).
        """
        record: dict = {
            "ts": (at or dt.datetime.now(dt.UTC)).isoformat(),
            "kind": TX_FRAME_KIND,
            "raw_hex": packet.hex(),
            "packet_id": packet_id,
            "airtime_ms": None if airtime_ms is None else round(airtime_ms, 3),
        }
        if not self._started:
            self._held.append(record)
            return
        self._write_item(record)

    def _write_item(self, item: ModemEvent | dict) -> None:
        if isinstance(item, dict):
            self._write(item)
            self.tx_count += 1
            return
        self._write(_record_for(item))
        if isinstance(item, RxEvent):
            self.rx_count += 1
        else:
            self.unparsed_count += 1

    def _write(self, record: dict) -> None:
        assert self._file is not None, "call open() before writing"
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
