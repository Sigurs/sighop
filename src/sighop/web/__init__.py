"""The web interface (DESIGN.md §8, §11, milestone 8).

The second renderer. `monitor/render.py` turns a typed event into a line for a
terminal; this package turns the same typed values into a page and a JSON record
for a browser. It inherits `monitor/`'s rule unchanged: `net/` never imports it,
and nothing here is on the reception path.

Five modules and two asset trees:

* `state.py`     — the read seam. `Protocol`s describing what the panel needs,
                   which `Runtime` satisfies structurally, so `web/` imports
                   nothing from `runtime.py` and `runtime.py` imports nothing
                   from `web/` (design D2). `cli.py` is the only module that
                   knows both.
* `serialize.py` — `RxRecord`, `Submission` and `TxOutcome` as JSON, built from
                   the typed values rather than from rendered lines (design D5).
* `feed.py`      — one `NetworkBus` subscription for the whole process, fanned
                   out to a bounded queue per connection, offered without
                   awaiting and never raising (design D4).
* `app.py`       — the FastAPI application factory and the service that runs it
                   inside the runtime's own event loop (design D1).
* `routes/`      — the request handlers, one module per area of §8.

`templates/` and `static/` are served by the application itself: no CDN, no
bundler, no request from the browser to any external origin (design D3).

Importing this package starts nothing and binds nothing. The submodules are not
imported here for that reason: `import sighop.web` must remain something a test
of the import boundary can do without pulling in FastAPI, a template
environment or an event loop.
"""
