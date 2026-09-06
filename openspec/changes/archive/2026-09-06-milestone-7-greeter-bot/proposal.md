## Why

Milestone 6 gave one entity a role it plays *for other people*: a room server answers logins,
stores posts and pushes history. Every other entity sighop runs is still reactive — it answers what
is addressed to it and originates nothing. DESIGN.md §12's milestone 7 is the first one where
sighop **acts on its own initiative**: a bot is a companion with a driver plugin, and the greeter
sends a welcome DM to a node it has never seen before, unprompted, because an advert arrived.

That is a different risk posture again. A room login is a stranger asking sighop to transmit, and
milestone 6 answered for it with silence on failure and a throttle on success. A greeting is
sighop transmitting *at* a stranger who asked for nothing, on a shared public mesh, triggered by
the one packet type anyone can forge cheaply. §7 states the three rules that make that acceptable —
never re-greet across a restart, rate-limit globally, greet only after signature verification — and
this milestone's job is to make all three properties of the code rather than of the driver's good
intentions. A fourth is added here: **greet only what is nearby**, so a flood advert that reached
us across half a mesh does not earn a DM back down the same long path.

§6's eighth table, `bot_state`, has been named-but-absent since migration `0001` for exactly this
milestone.

## What Changes

- **A bot plugin interface.** §7's `Bot` protocol — `on_advert`, `on_direct_message` — with a
  `BotContext` that exposes sending, contact lookup and durable state, and nothing else. A driver
  cannot reach the transmit scheduler, the bus or the database directly; every airtime-consuming
  action it takes goes through the context, which is where the rate limit and the observe/active
  mode live. `on_channel_message` is **not** in the interface this milestone: no channel key store
  exists and nothing in `net/` decrypts `GRP_TXT`, so a hook that could never fire would be a
  promise the runtime cannot keep. It arrives with channels.
- **A bot runtime that dispatches, not a second receiver.** Bots ride on the existing
  `DirectMessenger` and `ContactStore` rather than subscribing to the bus themselves: the messenger
  already decrypts and acknowledges an inbound DM, and doing that twice for one packet is the
  mistake design D10 was written to prevent. Driver work runs on the runtime's own bounded queue,
  never on the bus handler and never in front of an acknowledgement.
- **`bot` and `bot_state` tables, in migration `0003`.** `bot` binds a driver to exactly one
  entity and carries its enablement, its mode and its driver configuration — the shape `room`
  established in milestone 6, so the loader, the CLI and the startup reporting follow code that
  already exists. `bot_state` is the durable per-bot key/value store `BotContext` exposes, and is
  the only place a driver may persist anything.
- **Bots require a database, and say so when there is none.** §7 rule 1 — *"never re-greet across a
  restart"* — is true only because the greeting records are durable and are read before any traffic
  is processed. Without persistence there is no such table, so no bot is served and the run states
  that at startup exactly as it states an unserved room (design D5). A greeter that re-greets the
  neighbourhood after every restart is worse than no greeter.
- **A greeter driver with four gates, all of which must pass.** A contact is greeted only when it
  is (1) **not yet greeted** — carrying no greeting record of its own, which is a different question
  from whether the platform has heard it before: a node known for weeks and never greeted has an
  unsent welcome, and the record is the only thing that decides; (2) **within a configured hop
  limit** of us, so an advert that flooded in from a distant part of the mesh is heard and recorded
  but not answered; (3) of a node type worth greeting — a person's chat node, not a repeater or
  another room server; and (4) within the global rate limit. Every suppression is counted by reason
  and reported, because a greeter that silently greets nobody looks exactly like a mesh that went
  quiet.
- **A new greeter owes nothing to the contacts it already knows.** Creating one records every
  existing contact as already greeted — marked as seeded rather than sent — so a greeter created on
  an established node does not work its way through the whole table. `sighop bot greeted` is how an
  operator sees that record and changes it: release one contact so it is greeted the next time it
  adverts, or mark one so it never is. The debt is data an operator can read and edit, rather than a
  gate that also refuses the cases it was never meant to refuse.
- **Observe mode is the default, and transmitting is the opt-in.** A newly created bot is in
  `observe` mode: it runs the whole decision path, records and renders every greeting it *would*
  have sent, and puts nothing on the air. Moving it to `active` is a deliberate
  `sighop bot mode` invocation, and even then the existing `--enable-transmit` gate and duty-cycle
  ceiling apply unchanged. Nothing about the radio's safety posture changes.
- **Greetings are sent over a known route only.** A greeting is a DM to a peer that has never
  contacted us, so flooding one is the antisocial case of an already antisocial packet. When no
  route to the contact is known the greeting is suppressed with that reason rather than flooded.
- **`sighop bot` command surface.** Create a bot on an entity, list and show them with their
  configuration and counters, enable/disable, switch mode, set driver configuration, and inspect
  or clear durable state. Plus `sighop run` reporting which bots are running, in which mode, and
  with what limits — and saying plainly that without a database there are none.
- **Contact observations become reportable.** The contact store already knows whether an advert
  created a contact; it just never told anyone. It gains a listener that receives that observation
  together with the reception that produced it, which is what makes the hop gate and the
  first-sighting decision a single ordered fact rather than a race between two bus subscribers.
- **A staged exercise, dry-run first.** The exit criterion is: an observe-mode greeter running
  against live adverts, producing a decision log with the gates visible; then a real greeting
  transmitted to the second board — the test peer — and acknowledged; then a restart proving the
  peer is not greeted twice. Greeting real strangers on the live mesh stays an explicit operator
  choice, not a milestone deliverable.

## Capabilities

### New Capabilities
- `bot-runtime`: the plugin host — the `Bot` protocol and the `BotContext` it is given, how a bot
  is bound to an entity and loaded at startup, the dispatch path from advert observations and
  received direct messages onto a bounded queue that never blocks reception or an acknowledgement,
  durable per-bot state, the global rate limit every outbound driver action spends from, the
  observe/active mode that decides whether the radio is touched at all, the requirement of a
  database, and what a run reports about its bots.
- `greeter-bot`: the greeter driver's policy — what "not yet greeted" means and why it survives a
  restart, how a new greeter is seeded with the contacts it already knows and how an operator
  changes that per contact, the hop and node-type gates, one greeting per contact, the route
  requirement, the greeting text and its length bound, what is recorded when a greeting is sent,
  suppressed or would have been sent, and the counters that make an idle greeter distinguishable
  from a broken one.

### Modified Capabilities
- `contacts`: the store reports each verified-advert observation — whether it created the contact,
  whether anything changed, and the reception it came from — to an optional listener, synchronously
  and before the next observation is processed. Nothing about keying, candidate sets, durability or
  the write-behind path changes; a listener that raises must not lose the contact.
- `direct-messaging`: a received-message report names the local entity that received it, not only
  its display name, so a driver can reply as that entity without resolving a name back to an
  identity. Decryption, candidate trials, acknowledgement and the claimed-sender rule are
  unchanged, and the acknowledgement is still sent before any driver sees the message.
- `entity-store`: an entity may be created as a bot — stored type `bot`, MeshCore's chat node type
  on the wire, because a bot *is* a companion to every other node — and a bot binds to exactly one
  such entity, which serves no room.
- `runtime-cli`: `sighop bot` is added, and `sighop run` reports the bots it is running, their
  mode, their limits and their counters, including saying plainly that without a database there
  are none.

## Impact

- **No new dependencies.** The rate limit is the token bucket `net/room.py` already implements, the
  state store is JSONB through the existing SQLAlchemy stack, and the driver registry is a dict.
- **New code:** `src/sighop/bots/` — `base.py` (the `Bot` protocol, `BotContext`, the event and
  decision types), `runtime.py` (loading, dispatch queue, rate limit, mode, state), `greeter.py`
  (the driver) — plus `bot` and `bot_state` models and repositories in `src/sighop/db/`,
  `alembic/versions/0003`, and `sighop bot` in `src/sighop/cli.py`.
- **Modified code:** `src/sighop/net/contacts.py` (observation listener),
  `src/sighop/net/dm.py` (the received-message report names its entity),
  `src/sighop/runtime.py` (load, wire, start and stop bots; report them),
  `src/sighop/db/persistence.py` (bot repositories beside the room ones),
  `src/sighop/monitor/render.py` (bot startup, decision and status lines).
- **`protocol/` is untouched.** This milestone puts no new bytes on the wire: a greeting is an
  ordinary `TXT_MSG` composed by code that has existed since milestone 4.
  `tests/protocol/test_import_boundary.py` keeps passing unchanged.
- **No test may require Postgres to pass.** Bot tests use in-memory storage satisfying the same
  read-only-property protocols `tests/roomfixtures.py` established; the ones that exercise the real
  schema isolate in a throwaway schema and skip without a database URL, as milestone 5's do.
- **The safety posture is unchanged, and one property is added.** `--enable-transmit` stays off by
  default, the ceiling and priority classes are untouched, and a bot in observe mode — the default
  for every bot at creation — produces no transmission at all whatever its driver decides.
- **DESIGN.md is updated in this change**, per its own rule: §6's table list moves `bot_state` from
  "not built yet" to built and records that a `bot` table joined it and why; §7's Bots section
  records the interface as shipped (no channel hook yet), the fourth greeting rule (hop distance)
  and the observe-mode default; §11's layout records that the bot runtime lives in
  `src/sighop/bots/` rather than the sketched `entities/bots/`, for the same reason the room server
  lives in `net/room.py`; and §12's milestone 7 entry records the exercise and what it showed.
