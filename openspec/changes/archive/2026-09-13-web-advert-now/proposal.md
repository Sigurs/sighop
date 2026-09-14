## Why

An identity this run holds — a room, a bot, a plain companion — adverts only on its schedule:
first at a random point in a 24 h interval after startup, then every 18–30 h. An operator who has
just created a room, brought a bot up, or wants a nearby board to learn a key has no way to make
it advert now short of restarting with `--advert-zero-hop`, which only works at startup and only
for zero-hop. The scheduler already has both one-shot mechanisms (`request_zero_hop`,
`request_flood` — the greeter uses them); the panel does not reach them.

## What Changes

- Each identity in the identities page's "loaded by this run" table gets two actions: **advert
  zero-hop…** and **advert flood…**. Each link opens its own confirmation view. A POST from that
  view, carrying the provenance token and a nonce minted for that action and identity, submits
  one advert through the transmit scheduler as class 3 traffic.
- The two confirmation views state what each costs. A zero-hop advert reaches direct neighbours
  only and leaves the flood schedule alone. A flood advert is repeated by every repeater in the
  mesh on everyone's airtime, and it counts as the identity's scheduled flood: the next one moves
  a full jittered interval out.
- Both are **confirmation-only** guarded actions, like a room post: a nonce and no password,
  recorded as their own `web_guarded_action` event whether they succeed or are refused. There is
  no per-identity cooldown.
- A request is **refused, and nothing is submitted**, when:
  - the transmit gate is closed (a suppressed flood would still be charged and would push the
    real advert a day out with nothing on air)
  - the run has no radio readback yet (the scheduler would drop it)
  - the identity is not loaded by this run
  - (flood only) another flood from this run went out within the inter-entity gap (default
    10 min). The refusal gives the seconds remaining.
- The identities page shows each loaded identity's advert schedule: next scheduled flood, last
  flood, adverts sent, and any active override with its expiry.
- The admin rooms and bots lists link a served room or running bot to its identity's advert
  actions, so "advert this room" is one click from where the room is managed.
- The run's own output states each advert the panel requested, naming the operator, as it does
  for a transmit-gate change.
- `DESIGN.md` §8 records the actions and why they refuse rather than defer.

Not changing: scheduled advert timing, the 24 h floor, overrides (still startup-only through the
command line), the greeter's own advert behaviour, and the command line.

## Capabilities

### New Capabilities

None. Adverts are `advert-policy`'s; browser controls for the station are `web-admin`'s.

### Modified Capabilities

- `advert-policy`: new requirement for a single flood advert on explicit request. It goes
  through the scheduler as class 3, advances the identity's flood schedule, and counts toward the
  inter-entity gap. The existing one-shot zero-hop requirement is unchanged.
- `web-admin`: new requirement that loaded identities can be told to advert zero-hop or flood
  now, as confirmation-only guarded actions with the stated refusals. New requirement that the
  identities page shows each loaded identity's advert schedule.

## Impact

- `src/sighop/net/adverts.py`: public `flood_gap_remaining(now)`, a read of the existing
  `_gap_remaining`. The request methods are unchanged.
- `src/sighop/web/guarded.py`: `ADVERT_ZERO_HOP`, `ADVERT_FLOOD` actions and descriptions, not in
  `REAUTHENTICATED_ACTIONS`.
- `src/sighop/web/routes/admin.py`: `GET`/`POST /admin/advert/{entity_id}/zero-hop` and
  `/admin/advert/{entity_id}/flood`; rooms/bots list context gains the served identity's
  `entity_id`.
- `src/sighop/web/render.py`: advert-requested run-output line.
- Templates: `admin/identities.html` (schedule columns, action links), a new
  `admin/advert.html` confirmation view without a password field, and `admin/rooms.html` and
  `admin/bots.html` (links).
- Tests: `tests/test_web_admin.py` (or a new `tests/test_web_adverts.py`), `tests/test_adverts.py`,
  and the route sweep tests that enumerate guarded POSTs.
- `DESIGN.md` §8. No migration, no new dependency, no new configuration.
