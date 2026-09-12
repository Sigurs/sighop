## Context

See proposal.md — Why. What shapes the approach is where the state the UI needs actually lives.

Three-quarters of it is not in the database. `TxScheduler.status()` holds the queue depths and the
budget; `AirtimeBudget.remaining_pct` is computed against a sliding window in memory;
`DedupCache.stats`, `PathStore.destination_count`, `ContactStore` and `ProbeResult` are all
process state; the packet feed is `NetworkBus`'s fan-out, which exists for exactly one instant per
frame. `Persistence` holds the rest — entities, rooms, members, messages, bots, bot state, the
packet log — behind repositories the CLI already calls.

Four existing rules constrain the design more than any preference:

- **`net/` never imports the renderer.** Since milestone 2, `net/` emits typed events and
  `monitor/render.py` turns one into a line. `web/` is the second renderer and inherits the rule.
- **Memory stays the authority** (§6). The UI must read the in-memory stores, not re-query
  Postgres for a contact the process already holds.
- **Nothing on the packet path may await anything that can be slow.** The contact, path and packet
  log sinks all take a non-awaiting, non-raising `offer`. A browser is the slowest thing yet
  proposed to attach to that path.
- **`protocol/` imports nothing.** `tests/protocol/test_import_boundary.py` names the packages;
  `fastapi` joins the list it must not reach.

And one operator decision sits above all of it: **authentication is milestone 9**, and this
milestone ships a transmit-capable, key-revealing interface without it, loopback by default and
non-loopback when configured. That single fact drives decisions D9 and D10 and is the reason this
design spends more space on request provenance than a v1 UI normally would.

## Goals / Non-Goals

**Goals:**

- One process, one modem, one event loop: the UI sees live state directly, and adds no second
  reader of the radio or the mesh.
- The reception and transmit paths are byte-for-byte unaffected by whether anybody is watching.
  The corpus replay must produce identical counts with the UI wired in, as it did for milestones 6
  and 7.
- Every rule has one implementation. A room password rotated in the browser goes through the same
  repository call `sighop room passwd` uses.
- No JavaScript toolchain, no CDN, no asset that is not in the wheel.
- The absence of authentication is loud, bounded, and closable without redesign.

**Non-Goals:**

- Authentication, sessions, users, roles (milestone 9). No session table, no cookie, no login form
  is built here — including "just a stub", which is how a stub becomes the auth model.
- Channels: no key store, no `GRP_TXT` decrypt, no channel UI. Named absent, per milestone 7's
  precedent for `on_channel_message`.
- Being a client of somebody else's room server, and with it the 5-byte keep-alive acknowledgement
  milestone 6 recorded as owed. It belongs to a room *client*, which is not one of §8's four areas.
- A mobile layout. §8 asks for density on a screen beside other radio tooling.
- Replacing the CLI. Every noun the UI touches keeps its command.

## Decisions

### D1. The UI is a task inside `sighop run`, not a second process

`sighop run --web` starts the server in the runtime's own loop. Rejected: a separate `sighop serve`
process reading Postgres. It cannot show queue depth, remaining budget, dedup occupancy, the probe
readback or the live feed — none of which is in the database — and it cannot transmit, because the
modem is held by one process. The chat client would have had to hand messages to the running
process through a table used as a queue, which is a second scheduler with worse properties than
the one that exists.

Consequence: `uvicorn.Server(config).serve()` as a task, with `install_signal_handlers=False`.
Never `uvicorn.run()`, which creates its own loop and installs its own handlers over
`Runtime.install_signal_handlers`. Plain `uvicorn` — **not** `uvicorn[standard]`, which pulls
`uvloop` and would swap the event loop the radio's serial transport runs on for one nothing in
this project has tested. The dependency comment says so, because `[standard]` is what everyone
types.

### D2. `Runtime` gains a generic service slot; it never imports `web/`

`Runtime` takes `services: tuple[Callable[[], Awaitable[None]], ...]`, starts each alongside its
own tasks and cancels them on stop. `cli.py` builds the web service from `sighop.web` and passes
it in. So `runtime.py` imports nothing from `web/`, and `web/` imports nothing from `runtime.py`:
the state the UI reads is described by `Protocol`s in `web/state.py` — the structural-typing seam
the room server's storage and the bot host's state already use — and `Runtime` satisfies them
structurally. `cli.py` is the only module that knows both.

Rejected: `Runtime` holding an optional `WebServer` field. It reads better and creates the import
cycle the two `Protocol`s exist to avoid, and it would put the bind-failure path inside the
runtime, where milestone 5 established that a startup failure must be reported *before* a pipeline
exists.

### D3. Jinja + HTMX + vendored `htmx.min.js`, no build step

§11 says `web/ FastAPI app, routes, templates`, and the operator confirmed it. Server-rendered
pages with HTMX partial refresh, plus about a hundred lines of hand-written JS for the WebSocket
feed — the one genuinely streaming element on the page. `htmx.min.js` is vendored into
`web/static/` and served by the app: §10's image is read-only with no network egress assumed, and
a CDN reference is a page that breaks in the deployment it was built for.

Rejected: a Vite/React SPA. It buys the least on the densest screen (a table of contacts is a
table), and costs a Node toolchain in `build.sh`, a second lint/typecheck chain, a bundle stage in
a multi-stage image that currently contains no build tools, and a JSON API surface that would be
the only untested public contract in the project.

### D4. One bus subscription for the whole UI, fanning out to per-connection queues

`web/feed.py` holds a hub that takes exactly one `NetworkBus.subscribe("web-feed")` for the
process, plus the runtime's existing TX resolution callback, and fans both out to a bounded
`deque` per WebSocket connection. A connection that cannot keep up loses its oldest records, which
are counted per connection and reported into that connection so the browser can say "feed
incomplete, 412 dropped".

Rejected: one bus subscription per connection. The bus's subscriber list is a property of the
platform — `subscriber_stats()` is in the status line and in the room server's `ServerStats` — and
making it grow with the number of open browser tabs turns an operator's second tab into a change
in what the platform reports about itself.

The hub's `offer` never awaits and never raises, and the socket write happens in the connection's
own task. This is the contact-sink contract applied to a socket; it is the one place where getting
it wrong would let a browser slow the radio.

### D5. Serialisation lives in `web/`, and reuses the event types, not the rendered lines

The feed sends JSON built from `RxRecord`, `Submission` and `TxOutcome` — the same typed values
`monitor/render.py` formats into lines. `web/` gets its own serialiser rather than shipping
`render_frame_line`'s string, because a table wants fields and a terminal wants a line, and a UI
built on scraped text is a UI that breaks when the line changes. Several of these types already
have `as_json()` for the wide events; the feed uses them where they fit and adds nothing to `net/`.

### D6. The companion is an entity, not a module

§7's companion — "an addressable identity driven by a human in the WebUI" — needs no code of its
own: the send path is `DirectMessenger.send` (milestone 4), the receive path is `MessageReceived`
(milestone 4), and the identity is an `entity` row of chat node type. The chat surface picks one
and sends as it.

So **`entities/` still does not exist**, and §11's paragraph explaining its absence stays true.
The companion turned out to be a *user interface* over existing behaviour rather than a new entity
behaviour, which is the opposite of what §7 implied and is worth stating in DESIGN.md.

### D7. Direct messages get the tenth table, and it is written once and updated in place

Migration `0004` adds `direct_message`: id, `entity_public_key`, `peer_public_key`, `direction`,
`text` as bytes, `wire_timestamp BIGINT` (epoch seconds as they appear on the wire, per §6's rule
on the three cursor columns), `handled_at TIMESTAMPTZ`, `ref`, `packet_ids`, `attempts`,
`route_flood`, `route_path`, `outcome`, `ack_latency_ms`.

`UNIQUE (entity_public_key, ref)`, where `ref` is the send's `message_id` outbound and the
reception's `packet_id` inbound. That is what makes "written at submission, updated when it
resolves" one row: the update is `ON CONFLICT (entity_public_key, ref) DO UPDATE`. Milestone 5's
finding applies directly — a batch proposing one key twice is rejected outright — so the batch is
collapsed to one row per conflict key, keeping the latest, exactly as the contact and path
batches are.

Ordering in the UI is by `handled_at`, never by `wire_timestamp`: the wire value is the peer's
clock, and a peer with a wrong clock must not be able to reorder a conversation.

### D8. The DM record is write-behind on the refusing lane, and the acknowledgement never waits for it

`WriteBehind(drop_oldest=False)` — the contact lane, which refuses rather than displacing and
leaves the caller holding the item — because a lost conversation entry is not a re-learnable
route. But **not** straight-through like `bot_state`, and **not** the room server's
"acknowledge only once the row has landed".

Those two look like the precedents and are not. The room server acknowledges a post because it
promises to hold it, so the promise must be true first. `bot_state` is straight-through because a
lost greeting record buys a stranger a second unsolicited message. A direct message
acknowledgement is neither: it is the protocol's own receipt, computed on decrypt, and the sender's
retry window is 4–5 seconds. Delaying it on a database write would make sighop's acknowledgements
late against a firmware peer that has already retried — trading a visible, counted gap in our own
history for a real protocol failure. So: acknowledge, then offer the record.

### D9. Request provenance is enforced even though there is no authentication — because there is none

Two attacks work against an unauthenticated loopback service with no further work:

- **CSRF.** Any page in the operator's browser can `POST http://127.0.0.1:8080/transmit/enable`.
- **DNS rebinding.** A hostname the attacker controls resolves to `127.0.0.1`, making their
  JavaScript same-origin with the UI and able to read responses — key material included.

So every state-changing request requires a token generated at process start and present only in
pages this process served, and every request's `Host` header must match an address the interface
was configured to serve (rebinding's defence, and the cheap half). `Sec-Fetch-Site` is checked
where the browser sends it. GET/HEAD change nothing and reveal no key material, ever.

This is not authentication and is not presented as such. It is the difference between "reachable
by anything that can route to the port" and "reachable by anything that can render a page in the
operator's browser", and the second is a much larger set.

### D10. Guarded actions are a confirmation page and their own wide event

Revealing a private key, enabling transmit, and raising the ceiling each get a distinct
confirmation view stating what the action does — for the ceiling, that the default is a regulatory
limit — and a POST carrying the provenance token plus a nonce minted by that view. Each emits its
own structured event naming the action, target, actor context and outcome, separate from the
request event, so §8's "logged as their own wide events" survives the arrival of real users in
milestone 9: the actor field is filled in then, and nothing else about these events changes.

Key material appears in exactly one response body and is never in a page reachable by navigation.

### D11. Composition refuses; it never truncates

`net/dm.py`'s `MAX_TEXT_LEN` is 160 and the composer enforces it before sending, showing the limit
and the overage with the author's text preserved. This is milestone 6's rule applied on the right
side of it: **refuse rather than corrupt is right where a refusal can be heard**, and a browser is
the one place in this protocol where it can. The room server truncates an inbound post because
there is no way to say no to a radio; there is every way to say no to a form.

### D12. Every page carries the meter, and the panel is one template inheritance

A base template holds the duty-cycle meter, the gate indicator and the persistence state; every
page extends it. §8 calls the meter the single most important element on the screen, and the way
to make that structurally true rather than aspirationally true is to make it impossible to render
a page without it.

### D13. Verification status is a rendering primitive, not a per-view decision

One macro renders an identity — verified, unverified, or key-only — and every view uses it:
contacts, feed, room authors, chat senders. §8's rule is a hard rule, and a per-view `if` is how a
hard rule becomes 90% true. The marking is a glyph plus text, never colour alone, so it survives a
screenshot, a colourblind operator and a monochrome terminal-adjacent theme.

### D14. `packet_log` gains a bounded read and nothing else

`PacketLogRepository.recent(limit)` — newest first, capped, ordered by `received_at` — for the
feed's first paint. It stays a feed: nothing on the packet path consults it, and the read is under
the engine's existing statement bound like every other query.

## Risks / Trade-offs

- **An unauthenticated transmit surface is the largest new risk in the project.** → Loopback
  default; non-loopback permitted but announced in output and in a logged event with no way to
  silence it; D9's provenance checks; guarded actions confirmed and audited individually; milestone
  9 closes it. Named in the proposal's Impact so it is not discovered from the code.
- **A browser attached to the packet path could slow the radio.** → D4: one subscription, bounded
  per-connection queues, non-awaiting offer, drops counted and shown. The regression test is the
  one milestones 6 and 7 both used: a corpus replay with the UI wired in produces byte-identical
  delivered, duplicate, contact and path counts, and a stalled connection changes none of them.
- **Stored DM text is plaintext in the database.** A dump exposes conversations, where §6 was
  careful that a dump must not expose *identities*. → Stated in the migration's docstring and in
  the `dm-history` spec rather than left to be inferred from the column type. Encrypting it would
  need a key the UI holds anyway, so it would buy less than it appears to.
- **Two surfaces for every write invites divergence.** → Every write goes through the repository
  call the CLI uses; no validation lives in a route handler. Tested by asserting the stored result
  of a UI write is indistinguishable from the CLI's.
- **FastAPI/Starlette is a large dependency for eleven pages.** → It brings the WebSocket handling,
  the ASGI server integration and the dependency wiring this would otherwise hand-roll; the
  alternative was `aiohttp` plus hand-rolled templating, which is less code we depend on and more
  code we own in the layer where being wrong is least dangerous. Accepted deliberately.
- **`web/` is where an accidental blocking call would hurt most.** Argon2id for a room password is
  the concrete case, and `passwords.py` already runs it off the loop, bounded, from milestone 6. →
  Route handlers use it; `ruff`'s `ASYNC` rules already cover the class of mistake.
- **A raised ceiling is a legal question, not a UI one.** → The confirmation states that the
  default is regulatory, the meter says the ceiling in force is above it for as long as it is, and
  the change is audited.

## Migration Plan

1. `alembic upgrade head` applies `0004`, creating `direct_message`. Nothing existing is altered,
   so a run of the previous code against the new schema still works — the revision check refuses
   it only in the other direction, which is the design's rule (`sighop run` refuses a database not
   at the revision the code expects).
2. A downgrade of `0004` drops the table and **loses every recorded conversation**, stated in the
   migration's docstring the way `0003`'s greeting-record loss is.
3. The UI is opt-in, so deployment is unchanged until `--web` is passed. Compose gains a published
   port only when an operator wants one; §10's Postgres stays on the internal network.
4. Rollback is dropping `--web`. Nothing else in the run depends on the interface having existed.

## Open Questions

- **What does a busy mesh's feed cost a browser?** The per-connection queue bound and the initial
  paint size are guesses until a session runs against real advert volume — 555 receptions in 2 h 54
  min was the busiest measured. Answerable from the live exercise; changes a default, not the
  design.
- **Does direct-message volume need retention at all?** `dm-history` keeps everything, matching
  room retention's "keep everything" default and §13's unknown #1. A conversation is content, and
  the answer needs observed volume — which is exactly why milestone 5 refused to pick a default
  that would have deleted history before the question was asked.
