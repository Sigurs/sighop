# Design

## Context

See proposal.md for the incident and the timestamps. How things work today (`net/room.py`):

- Each `MemberRecord` has one `last_timestamp`, persisted in `room_member`. Password logins,
  blank-password logins, posts and requests all raise it with `max()`.
- `_handle_text` refuses a post when `body.timestamp < member.last_timestamp`, and sets
  `retry = body.timestamp == member.last_timestamp`. That `retry` only reaches the log and the
  `PostStored` event. Dedup is done separately: `_store_post` calls
  `storage.messages.find_retry(room, author, sender_timestamp)` before inserting, and returns the
  existing row if there is one.
- `_admit` builds the new member with `last_timestamp=max(timestamp, existing.last_timestamp)` for
  every admitted login, including the blank-password path at the top of `_handle_login`.
- Firmware (`simple_room_server/MyMesh.cpp`): blank-password logins from a known client bypass the
  block at `:343-376`, which is the only place a login writes `last_timestamp`. Posts use
  `>= last_timestamp` (`:448`), requests `< last_timestamp` refuses (`:541`).
- A companion stamps posts with the phone's clock (`companion_radio/MyMesh.cpp:1089-1107`) and
  logins and keep-alives with the radio's clock (`BaseChatMesh.cpp:577`, `:784`).

## Goals / Non-Goals

**Goals:**
- A member whose phone clock runs up to 5 minutes behind its radio clock can post.
- A blank-password re-login does not change the replay guard, as in the firmware.
- The next report of this kind shows which login path was taken and how large the gap was.

**Non-Goals:**
- Tolerance for requests (keep-alive, status, telemetry). They keep the firmware's strict check.
  This leaves the mirror case open: a phone clock *ahead* of the radio clock lets a post raise the
  guard above the radio's time, and the next keep-alive is refused. That case has not been seen,
  and a replayed keep-alive can move the member's sync cursor, so widening the request guard needs
  its own look.
- Changing what a blank-password login does to `sync_since`. sighop takes the login's value when
  it is non-zero (task 8.7 of the room-history milestone). The firmware leaves it alone, but that
  is a separate, deliberate choice.
- Stamping or ordering posts by any sender clock. Ordering stays on the room's own
  `post_timestamp`, as in the firmware (`MyMesh.cpp:59`).
- Making the tolerance configurable.

## Decisions

### D1. A fixed 300-second tolerance below the recorded timestamp, for posts only

The post check becomes `body.timestamp < member.last_timestamp - POST_CLOCK_SKEW_TOLERANCE_S`,
with `POST_CLOCK_SKEW_TOLERANCE_S = 300` as a module constant next to the other room constants.
The recorded timestamp still only moves up (`_record_last_timestamp` keeps its `max()`), so an
accepted post below it does not lower it.

Why 300 s: the observed gap was 15 s between the two clocks. A companion radio's clock is set
from the app on connect and drifts between syncs, so the gap can grow well beyond that. Five
minutes is far past any skew seen, and it keeps the replay window short: a captured post can be
replayed only within 5 minutes of the member's newest timestamp.

*Alternative: a separate, persisted post-only guard (`last_post_timestamp`).* Rejected: it needs a
migration and a change to the member upsert, and it is exact only for posts, while this fix is
also small enough to reason about.

*Alternative: judge freshness against sighop's own clock.* Rejected: a sender whose clock is
badly wrong (the firmware assumes this can happen, `MyMesh.cpp:443`) would be locked out entirely.

### D2. Replays inside the window are safe because of the existing dedup

A replayed post that was stored is found by `find_retry` and only acknowledged again, which is what
the firmware does for retries (`:449`). A replayed post that was never stored (refused for storage,
say) is the member's own genuine post, and storing it is harmless. `find_retry` already runs on
every post, so D1 needs no new lookup.

### D3. `retry` means "already stored"

With the tolerance, `body.timestamp == member.last_timestamp` no longer identifies a retry: a retry
of a post accepted below the recorded timestamp does not equal it. `_store_post` returns whether it
found an existing row (for example `tuple[PostRecord, bool]`, or a small result type), and
`_store_then_acknowledge` reports that as `retry`. The `retry` parameter to `_store_post`, which is
unused today, is removed.

### D4. A blank-password re-login keeps the recorded timestamp

`_admit` takes the recorded timestamp from `existing.last_timestamp` when the login came through
the blank-password path, and `max(timestamp, existing.last_timestamp)` otherwise. A new member
(no `existing`) always takes the login's timestamp. The flag is passed explicitly to `_admit`
rather than re-derived from the body there, so the branch that decided it is the one that says so.

### D5. Reporting

- `room_login_admitted` gains `empty_password: bool`.
- The post replay refusal's detail becomes, for example,
  `post timestamp 1790208107 is 409 s below the recorded 1790208516 (tolerance 300 s)`.

## Risks / Trade-offs

- [A skew larger than 5 minutes still locks posts out] → The refusal now states the gap in seconds,
  so it is diagnosable from one log line. Revoking the member resets the guard.
- [A captured post can be replayed for up to 5 minutes] → Only as a re-acknowledgement, or as a
  first store of the member's own unstored post (D2). Nothing new can be forged.
- [Diverges from firmware's strict post check] → Recorded in DESIGN.md next to the other room-server
  divergences.

## Migration Plan

No data or config changes. Deploy and restart. Rollback: revert the commit. Recorded timestamps
already raised by blank-password logins stay raised. The tolerance covers the observed case, and
revoking the member resets anything beyond it.
