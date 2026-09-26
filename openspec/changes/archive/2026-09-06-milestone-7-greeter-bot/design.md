## Context

See proposal.md — Why. What shapes the approach is what already exists, and one property of this
milestone that no earlier one had: **the trigger is a packet anyone can send, and the response is
addressed to a stranger.**

**What exists.** `net/dm.py` already does everything a greeting needs done: `DirectMessenger.send`
composes, routes through `net/paths.py`, retries to `MAX_ATTEMPT` with the attempt counter in the
flags byte, waits on the shared `AckRegistry`, and returns a `SendOutcome` — at
`PriorityClass.MESSAGE` (2), which §4.3 already names *"originated traffic: room broadcasts, bot
DMs"*. Its inbound half decrypts and acknowledges before it reports. `net/contacts.py` already
distinguishes a created contact from an updated one (`ContactObservation.created`) and is already
restored from Postgres before any traffic is processed. `net/rx.py`'s `RxRecord` already carries
`hop_count` (the packet's path length) and `snr_db`. `net/room.py` has the shape for everything
else this milestone needs: a token-bucket throttle counting refusals by reason, an entity claimed
at wiring time, typed events rendered by `monitor/render.py`, a `Protocol`-typed storage seam that
`tests/roomfixtures.py` satisfies in memory, and a `room` table whose CLI, loader and startup
reporting can be followed line for line. Migration `0002` names `bot_state` as deliberately absent.

**What does not exist.** No channel key store and nothing in `net/` that decrypts `GRP_TXT` — so
§7's third handler has nothing to fire on. No `entities/` package: the room server lives in
`net/room.py`, which is the precedent this milestone follows rather than the §11 sketch.

**Constraints that are not negotiable here.** The decode stage stays pure (design D6 of milestone
2). `protocol/` gains nothing at all — a greeting is a `TXT_MSG` and the codec has existed since
milestone 4. No test may require Postgres. `--enable-transmit` and the duty-cycle ceiling are
untouched. Exactly one component decrypts a packet and at most one acknowledgement leaves for it
(design D10 of milestone 6).

## Goals / Non-Goals

**Goals:**

- A plugin seam narrow enough that a driver's only capability is what the context grants, so the
  rate limit, the mode and the isolation cannot be bypassed by a driver that forgets to check.
- A greeter whose four gates are each independently observable, so an operator can tell which one
  is silencing it.
- Restart safety as a property of the data (contact table restored first, greeting record written
  before transmit), not of the driver's care.
- The same operational shape as rooms: a table, a repository, a CLI noun, startup lines, status
  counters, typed events.

**Non-Goals:**

- Channel (`GRP_TXT`) reception, a channel key store, or `on_channel_message`. Nothing here
  pretends they are coming in this milestone.
- The companion entity (§7) and anything WebUI. Milestone 8.
- Out-of-process or third-party plugins: drivers are in-tree modules in a registry dict. A loader
  for foreign code is a security surface this milestone has no reason to open.
- Greeting real strangers on the live mesh as a deliverable. The exercise is dry-run then test peer;
  live greeting is an operator's decision afterwards.

## Decisions

**D1 — A bot is a consumer of the direct messenger's reports, not a second bus subscriber.**
The messenger already decrypts inbound `TXT_MSG` for every local entity that is not a room
server's, and acknowledges it. A bot subscribing to the bus in parallel would decrypt the same
packet a second time and, if it also acknowledged, put two acknowledgements on the air for one
message — exactly the failure design D10 was written for. So the bot runtime registers as a
consumer of `MessageReceived` and the messenger keeps ownership of decryption and acknowledgement.
*Alternative rejected:* claiming the bot's entity the way `claim_for_room` does. That would move
decryption and acknowledgement into the bot runtime for no gain — a bot has no reason to answer
differently from any other companion, and every duplicated line is one that can drift.

**D2 — Advert observations reach a bot through a listener on the contact store, carrying the
reception.** `ContactStore.handle` already produces the one fact that matters (`created`) and holds
the `RxRecord` that produced it. A bot subscribing to the bus for `AdvertOutcome` records would
have to ask the store whether the contact was new — and the store's own subscriber may or may not
have processed that record yet, because subscribers have independent queues. The answer would be a
race whose wrong branch greets a peer twice or not at all. So the store gains an optional
`on_observation(observation, record)` callback, invoked synchronously after the store is updated.
Cost: a listener that raises must not cost a contact, which the spec states and a test pins.

**D3 — `bot` and `bot_state`, migration `0003`, mirroring `room`.** `bot` carries id, unique
`entity_id`, `driver`, `enabled`, `mode`, `config` (JSONB), `created_at`. `bot_state` is
`(bot_id, key)` primary key with a JSONB `value` and `updated_at`. Two tables rather than §6's one,
because §6's sketch predates a bot having a driver name, a mode and configuration to store, and
those are per bot rather than per key. The `room` shape is copied deliberately: `RoomRepository`,
`_room()`, the CLI's `_with_rooms` helper and `_load_rooms`/`_serve_room` in the runtime all have a
direct analogue, so this milestone writes little new structure.
*Alternative rejected:* driver name and config inside `entity.advert_config`. That column describes
how an identity adverts; putting a greeting template in it makes one column mean two things and
makes "list the bots" a scan of every entity.

**D4 — Observe is the default mode, and it is a column, not a flag on the command line.** A bot is
created in `observe`; it runs its whole decision path and records what it would have done.
Transmission requires `sighop bot mode <name> active`, and still requires `--enable-transmit` on
the run. Two independent gates for the same reason milestone 4 kept `--enable-transmit` after
adding the airtime ceiling: the mode says *this bot is meant to act*, the flag says *this run is
allowed to transmit*, and they answer to different people. Storing the mode rather than passing it
per run means a bot cannot become active by an operator forgetting which flags the last run had.

**D5 — No database, no bots.** §7 rule 1 is a statement about the `contact` table. Without
Postgres, `ContactStore` starts empty every run and *every* contact looks new, so a DB-less greeter
would greet the entire neighbourhood on every restart — the exact behaviour the rule forbids. Rooms
already refuse to exist without a database and say so at startup (design D5 of milestone 6); bots
take the same rule and the same startup line. Consequence, stated rather than hidden: `sighop run`
against a capture replay or a bare radio runs no bots at all.

**D6 — The greeting record is written before the greeting is transmitted, and it records an
*attempt* rather than a delivery.** The greeter writes `greeted:<public key>` into `bot_state` and
only then calls `send`. A crash between the two costs one un-sent greeting; the reverse order costs
a duplicate greeting to a stranger after every crash — and a duplicate is the failure mode §7 rule 1
exists to prevent. This is milestone 6's "acknowledge only once the row has landed" pointed the
other way: there, the row was the promise; here, the record is the guard. Consequence: while the
database is degraded the greeter greets nobody, and says why.

*Revised after the live exercise.* The original decision also said the record was written "whatever
happened, and never retried", on the reasoning that an unacknowledged greeting may well have
arrived and a duplicate is the worse failure. **The exercise showed that reasoning resting on a
false premise.** The one greeting transmitted went unacknowledged, and it went unacknowledged
because the peer could not read it at all (see D18) — not because an acknowledgement was lost. The
record settled the contact permanently, so the peer could never be greeted again, and the only
symptom was silence. Trading a possible duplicate against a *certain* permanent failure is the
wrong way round.

So the record now carries `acknowledged` and an attempt count, and only an outcome in a settled set
— acknowledged, seeded, operator-set, would-have-greeted — closes the matter. An unacknowledged or
crashed-`pending` record is retried on a later advert from the same contact, once
`retry_after_minutes` (15) has passed, up to `greeting_attempts` (3). Both bounds are what keep a
retry from becoming the spam the original decision was worried about: a node adverting every few
minutes cannot turn one silence into a burst, and a node that can never acknowledge costs three
greetings rather than an endless stream. A crash between the `pending` write and the send costs one
attempt, which is the right side of the trade — an attempt over-counted is a greeting not sent, and
an attempt under-counted is a stranger messaged twice.

**D7 — The greeting record is the only gate, and a new greeter is seeded with the contacts it
already knows.** *(Revised during implementation. The original decision required
`ContactObservation.created` **and** the absence of a greeting record; the first half is gone.)*

Requiring `created` conflated two different facts. "We have never heard this key" is a statement
about the platform's memory of the mesh; "we have never said anything to this node" is a statement
about this bot. A peer heard for the first time in milestone 2, or added by an operator as a hex
public key, has had nothing said to it — it is exactly the peer a welcome is for, and `created`
refuses it forever because the one advert that would have qualified arrived before the greeter
existed. Worse, it is a gate that silently expires: a contact heard once and never greeted (because
the rate limit was exhausted, or the database was degraded, or the bot was disabled that afternoon)
can never be greeted afterwards, because it will never be created again.

So the greeting record decides on its own: no record, and the other gates pass, means greet.

That inverts the risk the original decision was managing, and it needs an answer rather than a
disclaimer. Without `created`, a greeter created on an established node owes a greeting to **every
contact in the table**, delivered over the following hours as they advert. The answer is to make
the debt explicit at creation instead of implicit in a gate: **`sighop bot create` writes a greeting
record for every contact that already exists**, marked `seeded` rather than sent, and reports the
count. A new greeter therefore starts owing nothing, exactly as the `created` gate arranged — but
the state is *data an operator can see and change* rather than a rule that also refuses the cases
the rule was never meant to refuse.

`sighop bot greeted` is the other half: inspect one contact's record, clear it so the contact is
greeted when it next adverts, or set it so the contact never is. Releasing a seeded contact is one
command and one contact, which is the granularity a decision to message a stranger deserves.

*Alternative rejected:* keeping `created` as a configurable option defaulting to on. That leaves two
mechanisms for one policy — a gate and a record — and an operator debugging a greeter that greets
nobody would have to work out which of them is responsible. One mechanism, visible in
`sighop bot greeted`, answers that question by being readable.

Consequences, stated: the seeding is a bulk write at creation time and is reported rather than
silent; a greeter created while the database is degraded cannot seed, so creation fails rather than
producing a greeter that owes the whole table; and clearing a bot's whole state (`sighop bot state
--clear`) now discards the seed as well as the sends, which the command already says makes
previously greeted nodes eligible again.

**D8 — The hop gate reads the reception's own path length, and its limitation is stated.**
`RxRecord.hop_count` is the number of repeaters the advert crossed, so `max_hops` bounds *network*
distance: a default of 1 greets nodes we hear directly and nodes one repeater away, which is the
neighbourhood a greeting is for. What it does **not** bound is radio distance — a tropospheric or
ducted path delivers a zero-hop advert from a node hundreds of kilometres away, and no hop count
will ever separate that from a neighbour across the street. A `min_snr_db` option is offered
alongside for the operator who wants to try, and is null by default, because a strong ducted signal
defeats it too. The honest position: `max_hops` bounds how much of the mesh can trigger us, the
per-contact once-ever rule bounds the damage when it lets one through, and neither claims to
identify a duct. *Alternative rejected:* gating on the learned route's hop count instead. That is
the route *out*, may be learned later or from another packet, and would make the gate depend on
path-store state rather than on the packet that triggered the decision.

**D9 — `node_types` defaults to chat nodes only.** Greeting a repeater or another room server puts
a DM on the air that no human will read. The set is configurable because someone may want to greet
room servers, and default-restrictive because the airtime cost of the alternative is paid by
everyone on the channel.

**D18 — A peer is introduced to before it is messaged, and the two distances cost differently.**
*(Added after the live exercise.)* The exercise's greeting was transmitted four times to a peer at
zero hops with a strong signal, and every attempt went unanswered. The reason is not in this
milestone's code at all: a direct message is encrypted under a secret derived from the **sender's**
public key, so a peer decrypts one by trying the contacts it holds. the dev greeter bot had been
created minutes earlier and its flood interval is a day, so it had never adverted — the peer did
not hold its key, could not derive the secret, could not read a byte, and had nothing to
acknowledge. **From the sender's side this is indistinguishable from a peer that is not
listening**, which is exactly why it needs to be handled rather than diagnosed.

So `BotContext` gains `announce(hops)`: the driver says how far the peer is, and the runtime picks
the mechanism. The mechanism is the runtime's because the two choices differ enormously in cost:

* **Zero hops** — a `DIRECT` advert with an empty path, which stops at direct neighbours. Local,
  cheap, and spent up front on every attempt to a direct neighbour.
* **Further** — a flood advert, which every repeater in the mesh repeats, and whose interval floor
  is a day for that reason. Far too expensive to spend on a guess. So a first greeting to a peer
  heard over a repeater goes out **bare**, on the chance it already holds our key from an earlier
  advert of ours, and the flood is paid for only once silence has proved it necessary.

**That proof is acted on immediately, not at the next cooldown.** *(Revised after the second live
exercise, which reproduced the problem exactly: a peer at one hop, four unanswered attempts,
`announced=0`.)* When a bare greeting to a distant peer goes unacknowledged, the silence has
already identified its own cause — the peer cannot decrypt us — and waiting fifteen minutes buys
only the same silence for the same reason, while the peer is adverting *now* and is therefore
demonstrably awake. So the flood advert and the second greeting follow within the same reaction.
The cooldown has not been weakened: this escalation happens **at most once** per contact, and every
attempt after it is spaced by `retry_after_minutes` as before. A direct neighbour never escalates —
it was introduced to before its first greeting, so silence from it means something else.

**Silence is believed only after a grace period.** The message path spends four attempts inside
about forty seconds and then reports failure, but an acknowledgement returning over a longer path
than the one we sent on is *late*, not absent — and here the action taken on that difference is a
flood advert, which the whole mesh pays for. So `BotContext.send` takes `ack_grace_seconds`
(greeter default 30), which `DirectMessenger.send` honours by keeping the already-registered
expectations armed past the last attempt. It extends listening and never transmitting: the packet
count is unchanged, and a late acknowledgement is matched rather than counted `ack_unmatched`. It
is opt-in because an interactive `--send` that returned half a minute after it had already failed
would read as a hang.

`announce` is **awaited**. An advert is `PriorityClass.ADVERT` (3) and a message is `MESSAGE` (2),
so a send issued without waiting would be transmitted *first* and arrive at a peer that still could
not read it — the scheduler would have faithfully undone the fix.

A requested flood advances the entity's own flood schedule rather than arriving on top of it: from
the mesh's point of view it *is* the entity's advert. What bounds how often one can be asked for is
the bot's rate limit and nothing else, which is stated rather than hidden — at the default of six
greetings an hour, a greeter retrying to distant peers can flood far more often than the daily
convention, and the `announced` counter is what makes that visible.

*Alternative rejected:* a `DIRECT` advert routed along the learned reverse path to the peer. The
packet format permits it and the codec would emit one, but no evidence exists that any firmware
processes an advert arriving that way, and an untested packet shape on the air is not something to
build a delivery guarantee on.

**D10 — A greeting is never flooded.** `choose_route` already refuses to flood unless asked, and
this milestone never asks. A greeting to a peer with no known route is suppressed with
`no_route` — an unsolicited DM is the last packet that should be shouted across the whole mesh.
In practice the triggering advert usually supplies the route (a flood advert teaches its reverse
path; a zero-hop advert teaches a zero-hop route), so this suppression should be rare, and the
counter is what will show whether that is true.

This is about the **greeting**, and D18's flood advert does not contradict it. A greeting is
addressed to one peer and is content nobody asked for, so shouting it at the whole mesh spends
everybody's airtime on a message meant for one node. An advert is addressed to nobody and is the
one packet the protocol expects to be repeated — it is how a mesh learns who exists. The rule is
that the *unsolicited private message* is never flooded, not that this entity never floods.

**D11 — One token bucket per bot, no per-source bucket.** `net/room.py`'s `ReplyThrottle` is the
model, but a room server needed a per-source bucket because one peer can replay a login endlessly.
A bot's per-contact gate is absolute — once ever — so the only bucket that can be exhausted is the
global one, which is exactly the *"burst of adverts after an outage"* §7 warns about. The bucket is
in memory and refills from a rate in the bot's config; it is not persisted, because a restart is
already rate-limited by the once-ever record.

**D12 — Dispatch is one bounded queue and one worker task per bot.** The handler runs in the
worker, never on the bus subscriber and never before an acknowledgement. Per bot rather than per
runtime so one slow driver cannot starve another. Overflow drops the *oldest* pending dispatch and
counts it, matching `net/bus.py`'s subscription semantics rather than inventing a second policy;
for a greeter, an advert that has been queued long enough to be dropped has almost certainly been
superseded anyway.

**D13 — `src/sighop/bots/`, not `src/sighop/entities/bots/`.** §11 sketches an `entities/` package
holding the room server, the companion and the bot runtime; the room server ended up in
`net/room.py` and there is no companion yet, so creating `entities/` now would produce a package
that describes the layout less accurately than the sketch it came from. `bots/` is a peer of `net/`
that imports from it, exactly as `db/` is. DESIGN §11 is corrected in this change to say so.

**D14 — Drivers are a registry dict of in-tree classes, and each validates its own config.**
`sighop bot create --driver greeter` looks the name up and refuses an unknown one by listing what
exists. `sighop bot set` hands the value to the driver's validator, so "greeting text too long for
one direct message" (`MAX_TEXT_LEN` is 160 bytes) is refused at configuration time rather than
discovered at 3am when the first new node adverts. No entry-point discovery, no import by string:
loading foreign code into the process that holds the entity seeds needs a better reason than
convenience.

**D15 — `MessageReceived` gains the receiving entity, not just its name.** A driver replying must
send *as* the entity that was addressed, and `entity_name` cannot be resolved back to an identity
when two entities share a display name. The report carries the entity itself (the `LocalEntity`
already in hand at the call site). One field, no behaviour change, and `monitor/render.py` keeps
using the name.

**D16 — Bot state is written straight through, not through the write-behind queue.** Contacts and
paths use `db/writer.py` because losing one costs a re-learn; losing a greeting record costs a
duplicate greeting to a stranger, and D6 needs the write to have *landed* before the send. The
write is a bounded repository call from the worker task, under the connect and statement timeouts
`db/engine.py` already sets, and a failure suppresses the greeting.

## Risks / Trade-offs

- **A driver is in-process and can still do harm no seam prevents** (busy-loop, memory) → The seam
  bounds capability, not resources: drivers are in-tree, reviewed, and isolated only to the extent
  that a raising handler and a slow handler are contained (D12). Stated rather than implied.
- **The hop gate does not stop a ducted long-haul advert** (D8) → Accepted and documented; the
  once-ever rule bounds each such node to a single greeting, and the counters make it visible.
- **Observe mode can be mistaken for a broken greeter** → Every observed decision is rendered and
  counted as a would-have-sent, distinct from a suppression, and the startup line names the mode.
- **A greeting record written before a send that then fails means a node is never greeted** (D6) →
  The right side of the trade: a missing greeting is invisible to the recipient, a duplicate is not.
  The outcome is recorded, so the operator can see the unacknowledged ones.
- **Greeting strangers is socially loaded on a shared mesh** → Not a code risk and not solvable by
  code: the default is observe, the default hop limit is small, and the exercise never greets the
  live mesh. Whether to enable it on a public channel is the operator's call, made deliberately.
- **A second table where §6 named one** (D3) → The migration's docstring records why, as `0002`'s
  did for the three shapes that differed from the sketch.
- **Contact-store listener adds a synchronous call to a hot path** → It is one dict lookup and a
  queue offer in the common case; the offer never awaits and never raises, and a raising listener is
  caught. The alternative is a race (D2).

## Migration Plan

1. `alembic/versions/0003` creates `bot` and `bot_state`. No data migration: neither table exists
   anywhere, and the seven built by `0001` and `0002` are untouched. Downgrade drops both.
2. A deployment that does not create a bot is unaffected — `sighop run` reports no bots and behaves
   exactly as it does today.
3. Rollback is `alembic downgrade 0002`, which loses only bot configuration and greeting records.
   The consequence of losing greeting records — previously greeted nodes could be greeted again if
   their contacts were also lost — is stated in the migration docstring, not discovered.
4. Order of operational rollout: create the bot (observe by default), run against live adverts,
   read the decision log, then `sighop bot mode <name> active` with the test peer in range.
