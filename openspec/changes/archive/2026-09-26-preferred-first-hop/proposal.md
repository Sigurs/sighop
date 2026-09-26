# Proposal

## Why

The node's own antenna is weak, but a repeater next to it has a strong one. Routes are learned
from what we *hear*, and hearing a peer does not mean the peer hears us: a zero-hop route learned
from a strong neighbour, or a route whose first hop is a distant repeater that reached our
receiver, sends our weak transmission straight at a node that may never decode it. Every DIRECT
send along such a route burns airtime and a retry budget on a link that only works one way. An
operator who knows a nearby repeater carries our traffic well needs a way to say "go out through
that one first".

## What Changes

- A new station-wide setting: one **preferred first hop**, a repeater chosen by public key, or none.
  Stored in the database, set on the system page, applied to the running process without a restart.
- When set, every DIRECT send along a learned route leaves through the preferred repeater:
  - among the learned candidates for a destination, one whose first hop is already the preferred
    repeater wins over newer candidates that do not start with it;
  - otherwise the most recently confirmed candidate is used and the preferred repeater's hash is
    **prepended** at that route's hash width — a zero-hop route becomes a one-hop route through it;
  - a route that already passes through the preferred repeater further along is shortened to start
    at it, rather than visiting it twice;
  - a destination that *is* the preferred repeater is sent to unchanged;
  - a route that would exceed the protocol's path limit once prepended is sent unchanged, and that is
    counted.
- The acknowledgement timeout follows the rewritten hop count, because it is computed from the route
  actually sent.
- Floods are unchanged: the preferred repeater already repeats our floods if it hears them, and a
  flood carries no chosen path to prefix.
- The contacts page and send output show the route that will actually be sent and mark it as
  routed through the preferred first hop, so the table never shows a route the sender will not use.
- The system page shows whether the preferred repeater has been heard zero-hop (the premise of the
  setting) and warns when it has not.
- With no preferred first hop set, behaviour is byte-identical to today.

## Capabilities

### New Capabilities
- `route-preference`: the preferred-first-hop setting and how it rewrites the route chosen for a
  DIRECT send — candidate preference, prepending, shortening, exemptions and limits.

### Modified Capabilities
- `path-learning`: "the most recently confirmed path wins" gains the preferred-first-hop exception
  for route selection; learning and storage are unchanged.
- `direct-messaging`: routing along a known route applies the route preference, and the output
  says when a route was rewritten.
- `web-admin`: the system page gains the preferred-first-hop form.
- `web-dashboard`: the contact table shows the route as it will be sent, marked when rewritten.

## Impact

- `src/sighop/net/paths.py`: preference held on the shared `PathStore`; one resolution function used
  by every route lookup.
- `src/sighop/net/dm.py` (`choose_route`, `Route`), `src/sighop/net/room.py` (`_known_route_to`),
  `src/sighop/net/collect.py` (via `choose_route`), `src/sighop/web/render.py` (`_route_for`).
- `src/sighop/db/models.py`, `src/sighop/db/repositories.py`, new migration
  `alembic/versions/0014_route_preference.py` (one-row table).
- `src/sighop/runtime.py`: load at startup, apply on save.
- `src/sighop/web/routes/system.py`, `src/sighop/web/templates/system.html`, contacts template.
- Tests: path store, DM routing, room routing, collection routing, schema, system page.
- No new dependencies. No wire-format change.
