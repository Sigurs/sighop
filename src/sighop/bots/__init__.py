"""The plugin host for automated entities (DESIGN.md §7, milestone 7).

A peer of `net/` that imports from it, exactly as `db/` is. §11 sketches an
`entities/` package holding the room server, the companion and the bot runtime;
the room server ended up in `net/room.py` and there is no companion yet, so
creating `entities/` now would produce a package that describes the layout less
accurately than the sketch it came from (design D13). DESIGN §11 is corrected in
this change to say so.

Three modules, and the split is the seam:

* `base.py`    — what a driver is, what it is given, and what it may do. No
                 driver, no dispatch, no database.
* `runtime.py` — the host: loading, the bounded dispatch queue, the rate limit,
                 the observe/active mode and the durable state.
* `greeter.py` — one driver.

`drivers.py` is the registry that maps a stored `bot.driver` name to a class
(design D14). Nothing here imports `monitor/`: every event this package emits is
a typed value and `monitor/render.py` is what turns one into a line.
"""
