"""The storage boundary's one rule about time (design D7).

Every timestamp column is `TIMESTAMPTZ`, every value crossing the boundary is
timezone-aware UTC, and a naive `datetime` offered for storage raises rather
than being assumed to mean anything.

The assumption is what makes this worth a module. The development server's
`TimeZone` is `Europe/Helsinki`, so a naive value quietly adopted by the driver
would record a different instant here than the same code would record against a
UTC server — a bug that appears in one deployment and looks like a clock problem.
The corpus, the capture headers and the dedup TTL all reason about instants
across sessions recorded on different days, so there is no zone to fall back to
that would be right more often than it was wrong.
"""

from __future__ import annotations

import datetime as dt


class NaiveDatetimeError(ValueError):
    """A datetime with no time zone was offered for storage. Names the field."""


def ensure_utc(value: dt.datetime, *, field: str) -> dt.datetime:
    """A timezone-aware UTC datetime, or an error naming the field that was naive."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            f"{field} is a naive datetime ({value.isoformat()}); sighop stores instants "
            "and will not assume a time zone for one — attach dt.UTC, or the zone the "
            "value was actually recorded in"
        )
    return value.astimezone(dt.UTC)


def optional_utc(value: dt.datetime | None, *, field: str) -> dt.datetime | None:
    return None if value is None else ensure_utc(value, field=field)
