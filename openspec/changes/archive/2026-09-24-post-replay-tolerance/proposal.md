# Proposal

## Why

A stock MeshCore client could not post to a sighop room: every attempt was refused as a replay,
went unacknowledged, and the app showed "failed" (`issue-room-post-2`, 2026-09-24 00:08). The post
was written *after* the member's login, yet carried an older timestamp:

| Packet | Sender timestamp      | Stamped by                          | sighop received |
| ------ | --------------------- | ----------------------------------- | --------------- |
| Login  | 1790208516 (00:08:36) | companion radio RTC (`BaseChatMesh.cpp:577`) | 00:08:21 |
| Post   | 1790208507 (00:08:27) | phone app (`companion_radio/MyMesh.cpp:1089-1107`) | 00:08:29 |

A companion client stamps logins and keep-alives with the radio's clock, but posts with the phone's.
Here the radio ran about 15 s fast. The login raised the member's `last_timestamp` to the radio's
time, and every post stamped by the phone fell below it. The room's replay guard compares those two
clocks as if they were one. The firmware has the same single guard (`simple_room_server/MyMesh.cpp:448`)
and admits the problem only for CLI commands, which the companion restamps "to avoid tripping replay
protection" (`companion_radio/MyMesh.cpp:1103`).

sighop makes it worse in one place. When an existing member re-logs in with a blank password, the
firmware skips the whole update block, `last_timestamp` included (`MyMesh.cpp:335-376`). sighop's
`_admit` raises it to the login's timestamp (`net/room.py`), so a route re-establishment alone can
lock a member's posts out. sighop also persists the guard, so the lock survives a restart, where the
firmware's transient copy is cleared on reboot.

## What Changes

- A post is accepted when its sender timestamp is no more than **300 seconds** below the member's
  recorded timestamp, instead of requiring it to be at or above. Posts further below are still
  refused as replays. A resent post that is already stored is recognised by its sender timestamp
  (`find_retry`, unchanged) and acknowledged again without a second row, whether it falls inside or
  on the recorded timestamp.
- The `retry` reported with a stored post means "already stored", taken from the lookup, instead of
  "timestamp equal to the recorded one", which the tolerance makes wrong.
- An existing member's blank-password login no longer raises the recorded timestamp, matching the
  firmware. Logins with a password keep raising it and keep their strict guard.
- `room_login_admitted` logs whether the password was blank, so the next report shows which login
  path a client took.
- A refused post's detail states how far below the recorded timestamp it was, in seconds.
- DESIGN.md records the tolerance and the blank-password match, and why.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `room-history`: "A post from a permitted member is stored before it is acknowledged" gains the
  clock-skew tolerance, and states that a post below the tolerance is refused as a replay.
- `room-acl`: "A login is replay-guarded, and the guard survives restart" states that a
  blank-password login from an existing member leaves the recorded timestamp unchanged, and that the
  login report names whether the password was blank.

## Impact

- `src/sighop/net/room.py`: post replay check with a `POST_CLOCK_SKEW_TOLERANCE_S = 300` constant;
  `retry` derived from `_store_post`'s lookup; `_admit` leaves `last_timestamp` alone on the
  blank-password path; `room_login_admitted` gains `empty_password`.
- `tests/test_room_posts.py`, `tests/test_room_login.py`: new cases; the existing replay test moves
  its gap beyond the tolerance.
- `DESIGN.md`: room-server section.
- No schema, migration, config or web changes.
- Out of scope: requests (keep-alive, status, telemetry) keep the firmware's strict `<` check. See
  design.md for the mirror case this leaves open.
