# Proposal

## Why

When a room pushes a new post to a member, the acknowledgement window is measured from the moment
the push is *composed*, not from when it finished transmitting (`RoomServer.deliver`,
`src/sighop/net/room.py`). Queue wait and the push's own airtime come out of that window, so a push
that sits behind other traffic or is long on a slow modulation can expire almost as soon as it
leaves the radio — and the next round, 1.2 s later, retries it. Each retry draws a new random
`attempt`, so it expects a *different* acknowledgement, and the previous attempt's expectation has
already been released. The member's acknowledgement then arrives while the retry is being sent and
is reported as `ack_unmatched`: the member got the post, the cursor does not move, the member gets
it again, and repeated misses back the member off. The reference firmware carries the same gap as a
TODO (`MyMesh.cpp:1004`, "keep prev expected_ack's in a list, in case they arrive LATER").

Beyond the bug, the firmware's fixed windows (12 s flooded, 4 s + 2 s per hop direct) are short
for a busy or slow mesh, and every push to a member who has not been around for weeks spends
airtime on three attempts that cannot succeed. An operator needs to tune both per room.

## What Changes

- A push's acknowledgement window starts when the push has finished transmitting, not when it was
  composed. A push that is queued or on air cannot time out.
- An acknowledgement for an earlier attempt of the post currently being delivered to a member still
  counts as delivery of that post, so a late acknowledgement is no longer lost to the retry that
  replaced it. Earlier attempts' expectations are kept only until that post is confirmed or
  superseded.
- New per-room setting **push acknowledgement window** (seconds). Blank keeps the firmware formula;
  a value replaces it for every push from that room, flooded or direct.
- New per-room setting **push only to members heard within N days**. Blank pushes to every member
  (today's behaviour). A member not heard from within N days — by any activity with the room or by
  an advert — is skipped without counting a failure and with its position unchanged, and is pushed
  to again as soon as it is heard.
- Both settings are edited on a new room *delivery* page in the interface, shown in the rooms list,
  and take effect in the running process without a restart. Refreshing a served room's settings
  from the store also makes access and retention edits reach the running process, which today keeps
  the settings it started with until restart.
- Delivery reporting shows how long after transmission the acknowledgement arrived and whether it
  matched an earlier attempt, and how many members a room skipped as not recently heard.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `room-history`: when a push's acknowledgement window starts; late acknowledgements of earlier
  attempts count; the window length is configurable per room; delivery can be limited to members
  heard recently.
- `web-admin`: rooms are configured with the two delivery settings; room setting writes reach the
  running process without a restart.
- `web-rooms`: the rooms list shows each room's delivery settings and links its delivery page.

## Impact

- `src/sighop/net/room.py`: `deliver`, `push_once`, `_Delivery`, `_MemberState`,
  `_on_delivery_ack`, `_expire_deliveries`, `_clear_delivery`, `_push_timeout_ms`, `as_json`; new
  optional last-heard lookup into the contact store.
- `src/sighop/db/models.py`, `src/sighop/db/repositories.py` (`RoomRecord`, new
  `set_delivery`), new Alembic migration `0010` adding two nullable columns to `room`.
- `src/sighop/runtime.py`: `reconcile_rooms` refreshes served rooms' records; wiring of the
  last-heard lookup.
- `src/sighop/web/routes/admin.py`, new `admin/room_delivery.html`, `rooms/index.html`.
- Tests: `tests/test_room_sync.py`, repository/migration tests, web admin tests.
- No wire-format change. Defaults preserve current behaviour apart from the window-start fix.
