"""Typed network records as JSON for the feed (design D5).

Built from `RxRecord`, `Submission` and `TxOutcome` — the same typed values
`monitor/render.py` formats into lines — and never from the lines themselves. A
table wants fields and a terminal wants a line, and a display built on scraped
text is a display that breaks the next time the line changes.

`as_json()` is reused where one already exists (`TxOutcome`, `PacketLogRow`'s
own values) and nothing is added to `net/`: the wide-event shape is what those
methods are for, and a feed row needs a few fields those do not carry — the
duplicate flag, the direction, the reason a frame could not be decoded.

Every value here is JSON-safe by construction: bytes as hex, times as ISO 8601,
enums as their names. Nothing on this path may raise, because the thing that
would be raising is a display and the thing it would be interrupting is a
reception.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sighop.db.repositories import PacketLogRow
from sighop.net.bus import Submission, TxOutcome
from sighop.net.rx import RxRecord, outcome_fields

RX = "rx"
TX = "tx"

HISTORY = "history"
LIVE = "live"
"""Which side of the boundary a row came from. The browser marks it, because
"this happened before you connected" and "this is happening" are different
claims and a feed that blurred them would be lying about the present."""


def rx_record(record: RxRecord, *, duplicate: bool = False, source: str = LIVE) -> dict[str, Any]:
    """One reception, as a feed row.

    The undecodable case is the one that matters: §4.1's rule is that a frame
    nobody could decode must not become invisible, so it is serialised *with*
    its raw bytes and the reason, exactly as it is written to the packet log.
    """
    fields = outcome_fields(record)
    return {
        "source": source,
        "direction": RX,
        "packet_id": record.packet_id,
        "at": _iso(record.received_at),
        "route_type": _name(record.route_type),
        "payload_type": _name(record.payload_type),
        "size_bytes": record.size_bytes,
        "path": record.path.hex(),
        "path_len": record.hop_count,
        "hash_size": record.hash_size,
        "snr_db": record.snr_db,
        "rssi_dbm": record.rssi_dbm,
        "src_hash": record.src_hash,
        "duplicate": duplicate,
        "outcome": fields.get("outcome"),
        "reason": _reason(fields),
        # Present only for what could not be decoded, which is the only case
        # where the bytes themselves are the evidence.
        "raw": record.raw.hex() if record.failed else None,
    }


def tx_record(
    submission: Submission, outcome: TxOutcome, *, at: dt.datetime, source: str = LIVE
) -> dict[str, Any]:
    """One resolved transmission, as a feed row.

    Suppressed transmissions included, and that is the point: a gated run
    resolves everything as `suppressed`, and a feed that showed only what
    reached the air would render a receive-only session as silence.
    """
    resolved = outcome.as_json()
    return {
        "source": source,
        "direction": TX,
        "packet_id": outcome.packet_id,
        "at": _iso(at),
        "route_type": None,
        "payload_type": None,
        "size_bytes": len(submission.packet),
        "path": "",
        "path_len": None,
        "hash_size": None,
        "snr_db": None,
        "rssi_dbm": None,
        "src_hash": None,
        "duplicate": False,
        "outcome": resolved["tx_result"],
        "reason": outcome.reason or None,
        "raw": None,
        # Transmission-only fields. A reception has no priority class and no
        # entity that originated it.
        "priority": int(submission.priority),
        "entity_name": submission.entity_name,
        "origin": submission.origin,
        "airtime_ms": resolved["airtime_ms"],
        "queue_wait_ms": resolved["queue_wait_ms"],
        "attempts": outcome.attempts,
        "transmitted": outcome.sent,
    }


def logged_packet(row: PacketLogRow) -> dict[str, Any]:
    """One recorded packet, as the feed's painted history.

    The same shape a live row has, so the browser renders one list rather than
    two — with `source` saying which side of the boundary it came from, and the
    duplicate flag as it was recorded rather than as the bus would have had it.
    """
    return {
        "source": HISTORY,
        "direction": row.direction,
        "packet_id": row.packet_id,
        "at": _iso(row.at),
        "route_type": row.route_type,
        "payload_type": row.payload_type,
        "size_bytes": row.size_bytes,
        "path": "" if row.path_bytes is None else row.path_bytes.hex(),
        "path_len": row.hop_count,
        "hash_size": None,
        "snr_db": row.snr_db,
        "rssi_dbm": row.rssi_dbm,
        "src_hash": None,
        "duplicate": row.outcome == "duplicate",
        "outcome": row.outcome,
        "reason": row.reason,
        "raw": None if row.raw is None else row.raw.hex(),
        "priority": row.priority_class,
        "airtime_ms": row.airtime_ms,
    }


def _iso(when: dt.datetime | None) -> str | None:
    return None if when is None else when.isoformat()


def _name(value: object) -> str | None:
    """An enum by its name, so a browser shows `ADVERT` rather than `3`."""
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _reason(fields: dict[str, object]) -> str | None:
    """Why an outcome was what it was, where the decode stage said.

    Joined from the two fields `outcome_fields` splits it into, because a
    display shows one sentence and a wide event indexes two values.
    """
    reason = fields.get("failure_reason")
    if reason is None:
        return None
    detail = fields.get("failure_detail")
    return f"{reason}: {detail}" if detail else str(reason)
