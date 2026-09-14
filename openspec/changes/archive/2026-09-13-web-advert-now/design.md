## Context

The pieces already exist, and nothing reaches them from a running process except the greeter:

- `AdvertScheduler.request_zero_hop(stub)` builds a DIRECT/zero-hop advert and submits it at class
  3 with a 300 s deadline. It leaves `next_flood_at` and `last_global_flood_at` alone.
- `AdvertScheduler.request_flood(stub)` submits a flood advert and sets `last_flood_at`,
  `last_global_flood_at` and `next_flood_at = now + jittered(effective_interval)`. Its docstring
  says the caller is responsible for how often it is called. The greeter calls it through
  `Runtime._announcer_for`.
- `AdvertScheduler._gap_remaining(now)` is what `tick()` uses to defer scheduled floods for the
  inter-entity gap (default 600 s).
- `TxScheduler.transmit_enabled` is the gate. With the gate closed, a submitted packet is charged at
  hand-off and counted as suppressed. `LiveState.radio` is `None` until the board's readback
  arrives, and until then the scheduler drops packets with `no_radio_readback`.
- A stub's `entity_id` is its name for keyfile and stored identities (`add_identity(entity_id=None)`).
  `/admin/reveal/{entity_id}` already addresses loaded identities by it, from the "loaded by this
  run" table on `admin/identities.html`.
- Guarded actions (`web/guarded.py`) come in two levels. Some re-authenticate with a password
  (reveal, export, transmit, ceiling). A room post is confirmation-only: a nonce bound to action and
  target, a provenance token, `page.unverified` refusal and `audit()`. `admin/guarded.html` always
  renders a password field.
- `RoomServer.entity` and bot workers' `entity` are `LocalEntity` values. A stub is matched to them
  by public key, as `routes/rooms.py` and `routes/keys.py` already do.

## Goals / Non-Goals

**Goals:**
- One route pair per advert kind, following the reveal and room-post shape, so the guarded-action
  sweeps cover them with no new mechanism.
- Every refusal decided from live state at POST time, never from what the confirmation view showed.

**Non-Goals:**
- Changing the greeter's use of `request_flood`. Bots still flood without the gap check, as they
  did before this change.
- A runtime override control, or advert changes from the command line.
- Showing whether the advert actually reached the air. The packet feed and the TX counters already
  do that.

## Decisions

### D1. Refuse, never defer or queue

Every refusal condition is refused at POST time and nothing is submitted.

- **Closed gate.** Refused because submitting would charge airtime and suppress the packet. For a
  flood it would also move `next_flood_at` a full interval out, so one click would silently cost
  the identity its next real advert. This intentionally differs from room posts, which are
  accepted with the gate closed (DESIGN §8). A post is a stored row that is delivered once the gate
  opens. An advert is a packet: once suppressed it is gone.
- **Inside the gap.** A flood is refused rather than deferred by setting `next_flood_at = now + gap`
  and letting `tick()` send it. Deferring would give no result to report and could land after the
  operator had closed the gate. A refusal that names the seconds remaining is honest and needs no
  new state.

*Alternative:* defer through the schedule, which matches how `tick()` treats scheduled floods.
Rejected because an operator pressing "flood now" should know whether a flood was sent.

### D2. The gap applies to operator floods, measured against every flood this run

The check is `adverts.flood_gap_remaining(now) > 0`: a public wrapper around `_gap_remaining`,
counting scheduled, greeter and operator floods alike. The spec's inter-entity gap rule exists so
that N identities never burst together. An operator clicking flood on three identities in a row is
exactly that burst.

A consequence, accepted: a second flood for the *same* identity within 10 minutes is also refused.
The operator chose no per-identity cooldown, and this is not one. It is the existing gap, which is
also the only thing stopping a repeated click from flooding the mesh twice.

*Alternative:* check only floods from other identities. Rejected because the scheduler tracks a
single `last_global_flood_at`, and splitting it adds state for a case that is itself a flood burst.

### D3. Confirmation-only guarded actions, `advert_zero_hop` and `advert_flood`

The two constants are added to `guarded.py` with `ACTION_DESCRIPTIONS`, and are not in
`REAUTHENTICATED_ACTIONS`. The operator chose this. It also matches the room-post reasoning: an
advert is an ordinary transmission by an identity this operator already runs, not a change of what
the station may do. It is still a guarded action with a nonce, not a plain form, because a flood's
cost lands on other people's airtime and must never be triggered by a prefetch or a reload.

Each action has its own nonce target (`entity_id`), so a zero-hop confirmation cannot be spent as a
flood, and one identity's confirmation cannot be spent on another.

### D4. Routes and view

- `GET /admin/advert/{entity_id}/{kind}` renders `admin/advert.html`: a new template, because
  `guarded.html` hard-codes the password field. It shows the description, the identity (name, node
  hash, public key), `next_flood_at`, `last_flood_at`, and the current gate and gap state. The gate
  and gap are shown so an operator can see a refusal coming, but the POST re-checks both.
- `POST /admin/advert/{entity_id}/{kind}`, where `kind` is `zero-hop` or `flood` (any other value
  gets 404). The order of checks is fixed so every refusal is audited once:
  1. `page.unverified`
  2. identity lookup, then nonce `spend` (refused as "no confirmation")
  3. gate closed
  4. `state.radio is None`
  5. for a flood, the gap

  After those it calls `request_zero_hop` or `request_flood`, then `audit(... outcome="success",
  entity_name, node_hash, next_flood_at)` and `page.say(render_advert_request(...))`, and redirects
  303 to `/admin/identities`.
- Refusals render `admin/refused.html` with the specific reason, 403 for a confirmation failure and
  409 for a state refusal (gate, radio, gap), with 404 when the identity is not loaded. This matches
  how room post splits 403 and 409.

The identity is looked up against `page.state.adverts.stubs` by `entity_id`, as `_loaded()` in
`admin.py` already does. Stubs are keyed by name, so an ephemeral stub (`--stub`) is addressable
too.

### D5. Schedule display lives on the identities page

`admin/identities.html` "loaded by this run" gains the columns next flood, last flood, adverts, and
override (interval and expiry, or a dash), plus the two action links. Times are rendered as ISO UTC
in monospace, like every other timestamp in the panel. No relative "in 5 h" text is added: the
panel has no such helper and the instrument-panel direction prefers exact values.

### D6. Room and bot links by public key

`admin.rooms` and `admin.bots` build a map from public key to stub `entity_id` from
`page.state.adverts.stubs`, and pass each served room or running bot's `entity_id` to the template.
A room or bot that is not served or running gets no link, which is the same condition the lists
already show in their served and running columns.

## Risks / Trade-offs

- **Flood cost is one click after confirmation.** No password and no cooldown, by operator choice.
  Bounded by the 10-minute gap (D2), the class-3 priority, and the duty-cycle ceiling, which
  together make a runaway impossible. The confirmation text states the mesh-wide cost.
- **Budget exhausted at submission.** The advert waits up to its 300 s deadline and may be dropped.
  In that case a flood has already moved `next_flood_at`, which is the documented `request_flood`
  behaviour (a dropped advert is not retried outside its schedule). The success page says the advert
  was *submitted*, not transmitted.
- **Names as ids.** `entity_id` is the stub name and the name is in the URL. This is the existing
  reveal precedent; names are not secret and are advertised in clear.
- **A greeter flood and an operator flood interact through the gap.** A greeter flood can make an
  operator's flood be refused for up to 10 minutes. The refusal states the seconds remaining, so
  this is visible rather than mysterious.
