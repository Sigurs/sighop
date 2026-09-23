"""The node's own rendered output: pure functions from a decoded record to text.

The `monitor` command these once served is gone with the command line; the
runtime renders its startup lines, receptions, status and activity through
them. Line-oriented, greppable, and tested by string comparison with no device,
no file and no event loop (design D8).
"""
