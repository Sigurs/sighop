## Why

Everything sighop can do, it does through a terminal. Eight milestones have built a platform
whose state is real — durable identities, contacts, learned paths, room history, bots with
durable greeting records, a duty-cycle budget that is a legal limit on EU 868 — and the only way
to see or change any of it is `sighop run`'s status line and eleven subcommands. DESIGN.md §12's
milestone 8 is the WebUI, and §8 states the four areas and their order: admin and config,
observability, room browsing, chat client. All four are in scope here.

The fourth is the one that changes what sighop *is*. §7's companion — "an addressable identity
driven by a human in the WebUI" — has been named since the beginning and has never existed;
`sighop run --send` is a one-shot that dies with the process. A chat client makes sighop usable
without a handheld, which §8 calls the strongest argument for the project, and it is the first
time a human drives a transmission from outside the process that owns the radio.

That is a new risk posture again, and it is the one this milestone has to be careful about.
Milestone 4 made the first transmission a watched operator act. Milestone 7 made a bot's
unsolicited greeting a matter of durable records and rate limits. A browser tab that can transmit
is neither: it is a general-purpose transmit surface with a private-key store behind it, and §8's
answer — real authentication — does not arrive until milestone 9. What this change owes is that
the gap is **explicit, defaulted shut, and loud** rather than incidental.

## What Changes

- **A web server inside `sighop run`, not beside it.** The dashboard needs the scheduler's queue
  depths, the budget's remaining fraction and the bus's live receptions, and the chat client needs
  the messenger that owns the acknowledgement registry. None of those is in the database, and a
  second process would need a second modem. So the UI is `sighop run --web`, an opt-in task in the
  runtime's own event loop — never `uvicorn.run()`, which builds its own loop and installs its own
  signal handlers over the ones the runtime already owns.
- **Unauthenticated, loopback by default, and non-loopback only when asked.** `--web-host`
  defaults to `127.0.0.1`. A non-loopback bind is permitted — the operator's call — and is
  reported at startup as its own line and its own wide event, naming that this build has no
  authentication and that anything reaching the port can transmit and read key material.
  Authentication is milestone 9. **This relaxes DESIGN.md §8's "no unauthenticated mode, at any
  milestone", which is corrected there rather than quietly contradicted here.**
- **An instrument panel, server-rendered.** Jinja templates, HTMX for partial refresh, a
  hand-written WebSocket client for the live packet feed, and a vendored `htmx.min.js` — no Node,
  no bundler, no CDN, nothing added to §10's minimal image but three Python packages.
- **The live feed is a bus subscriber that drops rather than blocks.** `NetworkBus.subscribe`
  already gives a bounded queue whose overflow is counted, which is exactly the contract a slow
  browser needs: a WebSocket that cannot keep up loses frames and says how many. Nothing in the
  reception path may await a socket.
- **Rendering lives in `web/` and `net/` does not import it**, the rule `monitor/` has followed
  since milestone 2. The feed serialises the same typed records `monitor/render.py` formats.
- **Admin and config over the repositories that already exist.** Entities, rooms, members,
  retention, passwords, bots, their mode and their driver configuration — every write goes through
  the same repository call the CLI uses, so there is one implementation of each rule. Revealing a
  private key, enabling transmit and raising the duty-cycle ceiling are §8's audited actions: each
  is a distinct wide event naming the action, and each is confirmed in the UI rather than being a
  link.
- **Room browsing reads `message` and `room_member`.** History with its `post_timestamp` cursor,
  member lists with their permissions and sync positions — the two things an operator currently
  reads with `sighop room history` and `sighop room members`.
- **A chat client for direct messages, and a `direct_message` table to give it a history.**
  Sending is `DirectMessenger.send` — the code that has existed since milestone 4 — from a
  companion entity the operator picks. Receiving is the messenger's existing `MessageReceived`
  report. Neither direction is durable today: §6's `message` table is room-scoped, so a page
  refresh would lose a conversation the radio actually carried. Migration `0004` adds the tenth
  table, and the messenger gains a report sink of the shape `ContactStore` and `PathStore` already
  take.
- **Channels are not in this change, and the module says why.** §8 area 4 says "DMs and channel
  messages"; nothing in `net/` decrypts or sends `GRP_TXT` and there is no channel key store —
  the same absence that kept `on_channel_message` off milestone 7's `Bot` protocol. A channel tab
  that could never carry a message is the promise that milestone declined to make.
- **Being a *client* of someone else's room server is also not in this change.** Milestone 6
  recorded the 5-byte keep-alive acknowledgement as "the one thing milestone 8 must add before it
  can keep-alive"; that belongs to logging into a foreign room, which is not one of §8's four
  areas. Named here so the note is not lost.
- **`packet_log` becomes readable.** It was built to power the live feed and has only ever been
  written, counted and pruned. The feed's first paint needs the recent rows, so the repository
  gains a bounded, ordered read.

## Capabilities

### New Capabilities

- `web-server`: the app's existence and shape — where it runs, what it binds, how it is opted
  into, what it reports at startup, the per-request wide event §9 calls a unit of work, and the
  behaviour of every page when the database is degraded or absent.
- `web-dashboard`: the observability panel — the live packet feed over a WebSocket, the
  duty-cycle meter against the ceiling, queue depths, per-entity counters, the contact table with
  learned paths and SNR, modem health, and §8's hard rule that unverified content is never
  presented as verified.
- `web-admin`: configuration and CRUD over entities, keys, radio, bots and rooms, and the
  re-confirmed, individually audited actions that reveal a key, enable transmit or raise the
  ceiling.
- `web-rooms`: browsing stored room history and per-room member lists.
- `web-chat`: the companion chat client — conversations with contacts, sending as a chosen local
  entity, delivery state from the acknowledgement the messenger already waits for.
- `dm-history`: the durable record of direct messages in both directions, its table, and what a
  lost or degraded write costs.

### Modified Capabilities

- `packet-log`: gains a read — recent rows, newest first, bounded — so the feed can paint what
  happened before the browser connected. Unchanged in every other respect: still a feed, still
  pruned, still consulted by nothing on the reception path.
- `direct-messaging`: outbound sends and inbound receptions are reported to an optional sink so
  they can be made durable, on the same never-awaits/never-raises contract the contact and path
  sinks have; and a send may be initiated by a caller other than the one-shot, concurrently, with
  per-entity ordering preserved.
- `runtime-cli`: `sighop run` gains `--web`, `--web-host` and `--web-port`; startup reports where
  the UI is listening and, when the bind is not loopback, that it is unauthenticated.

## Impact

- **New**: `src/sighop/web/` — `app.py`, `routes/`, `templates/`, `static/`, and the feed's bus
  subscriber. `alembic/versions/0004_*` with the `direct_message` table and a `DirectMessageRepository`
  beside the others in `db/repositories.py`.
- **Modified**: `runtime.py` (the web task, the DM sink wiring, the state the panel reads),
  `cli.py` (three flags), `net/dm.py` (the report sink), `db/repositories.py` and `db/persistence.py`
  (the new repository and its writer lane), `db/models.py`, DESIGN.md §8 (the authentication
  correction) and §11 (the `web/` line, which describes a package that did not exist).
- **Dependencies**: `fastapi`, `uvicorn` (plain — **not** `[standard]`, which pulls `uvloop` and
  would replace the event loop the radio runs on), `jinja2`, `websockets`, `python-multipart`.
  Vendored: `htmx.min.js`. No Node toolchain, so `build.sh` and the Dockerfile gain no stage.
- **Unchanged**: `protocol/` gains nothing — the boundary test keeps passing, and the corpus
  replay must produce byte-identical counts with the UI wired in, as it did for milestones 6 and 7.
- **Security posture**: this build ships a transmit-capable, key-revealing surface with no
  authentication. That is bounded by the loopback default and by the startup warning, and it is
  closed by milestone 9.
