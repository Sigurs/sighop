"""Persistence: a durable backing for state memory stays the authority over.

`db/` is a peer of `net/`, never a layer beneath `protocol/` (DESIGN.md §11,
design D10, asserted by `tests/protocol/test_import_boundary.py`). Nothing here
is on the reception path: `net/contacts.py` and `net/paths.py` keep their own
in-memory structures, answer every lookup from them, and hand this package
writes that happen in their own time (design D2).
"""

from __future__ import annotations

from sighop.db.engine import (
    Database,
    DatabaseError,
    DatabaseUnavailableError,
    SchemaVersionError,
)

__all__ = [
    "Database",
    "DatabaseError",
    "DatabaseUnavailableError",
    "SchemaVersionError",
]
