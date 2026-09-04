"""The network layer: the seam between the radio and the protocol codec.

Milestone 2 populates only `rx.py`, the stateless decode stage. The bus, the
dedup cache, path learning and the TX scheduler are milestone 3's, and are
deliberately absent rather than stubbed.

The dependency runs one way — `net/` uses `protocol/`, never the reverse
(DESIGN.md §11, `tests/protocol/test_import_boundary.py`).
"""
