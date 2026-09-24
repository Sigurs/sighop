# Proposal

## Why

A stock MeshCore client posting to a sighop room kept retrying: sighop stored the post and
transmitted its acknowledgement every time (`issue-room-post`, 2026-09-23 17:58–17:59), but the
client never confirmed delivery. Posts that arrive flooded are acknowledged along whatever route the
path store has learned. For a client that sends zero-hop floods, that route is a zero-hop DIRECT
learned from the post itself. That ACK is sent once, no repeater relays it, and a single missed
reception costs the member another retry. The client also never learns a route to the room, so every
later post floods the mesh again. The room-server spec already requires that a flooded request be
answered with a reply carrying the route back, and post acknowledgements do not follow it.

## What Changes

- A post that arrives **flooded** is acknowledged with a flooded `PATH` return whose extra payload is
  the post's ACK. The packet carries the path the post travelled, encrypted to the member. This is
  the same mechanism sighop already uses for a flooded login, and the same one a companion uses for
  a flooded direct message (`BaseChatMesh.cpp:328-340`). The client learns a direct route to the room
  and confirms the post in the same exchange. Its reciprocal `PATH` back is already adopted by the
  room (`net/pathbodies.adopt_path_body`).
- A post that arrives **direct** keeps today's behaviour: a bare ACK along the member's known route,
  falling back to a flood when no route is known.
- Retries are handled the same way. A retried flooded post gets a path return again, carrying the
  ACK for that attempt.
- The route an acknowledgement took is reported with the stored post (`room_post_stored` gains the
  route), so a log like `issue-room-post` shows whether the ACK went DIRECT, flooded, or as a path
  return.
- This is a deliberate difference from `simple_room_server`, which sends a bare flooded ACK when it
  has no out-path for the client (`MyMesh.cpp:494-497`). The difference is recorded in DESIGN.md.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `room-server`: "A reply is routed the way the request arrived" gains explicit coverage of a post's
  acknowledgement. A flooded post is answered by a path return bundling the ACK, a direct post by an
  ACK along the known route. The route taken is reported.

## Impact

- `src/sighop/net/room.py`: `_acknowledge_post` chooses between a path return with the ACK bundled
  (flooded arrival) and a bare ACK (direct arrival). `_store_then_acknowledge` logs and emits the
  route.
- `src/sighop/protocol/payloads.py`: no change expected. `ReturnedPathBody` already supports
  `extra_ack`.
- `tests/test_room_posts.py`: new cases for flooded and direct arrivals, retries, and the reported
  route. Existing tests that assume a bare ACK for every post are updated where their post arrives
  flooded.
- `DESIGN.md`: room-server section notes the divergence from firmware and why.
- Airtime: a flooded post's ACK grows from about 6 to about 22 bytes plus the path (about 250 ms to
  380 ms at EU868 narrow). This is repaid once the client stops flooding its posts.
- No schema, config, or web changes.
- Out of scope: flooded direct messages to ordinary entities (`net/dm.py`) are still ACKed along the
  learned route. Same question, but a separate change.
