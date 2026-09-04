"""`sighop monitor`: the smallest consumer that makes decoded traffic visible.

Line-oriented rather than a full-screen panel — that is the WebUI's job at
milestone 8. Here, greppable, pipe-friendly, dependency-free output that can be
tested by string comparison is worth more than a layout.

`render.py` is pure functions from a decoded record to text; `run.py` owns the
event loop, the counters and the source. The split is what makes the format
testable with no device, no file and no event loop (design D8).
"""
