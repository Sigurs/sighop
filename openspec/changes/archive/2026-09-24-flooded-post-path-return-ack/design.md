# Design

## Context

See proposal.md for the motivation. How things work today:

- `RoomServer._acknowledge_post` (`net/room.py`) always builds a bare ACK (`build_ack_packet`) and
  sends it on `_route_to(member)`. That is the path store's most recent route to the member, keyed
  by public key, then by node hash, then a flood. `PathStore.observe` learns the reversed route from
  every flood reception, the post included. A zero-hop flooded post therefore produces a zero-hop
  DIRECT ACK: 6 bytes, transmitted once, relayed by no one.
- `_send_login_reply` already answers a flooded login with a flooded `PATH` return. The body is built
  with `build_returned_path_body` from the inbound packet's path and hash size, the flood is sent at
  sighop's own `path_hash_size`, and the bundled extra is a `RESPONSE`.
- `ReturnedPathBody` already has `extra_ack`, and `build_returned_path_body` encodes it as
  `extra_type=ACK` plus the 4-byte checksum.
- Stock companions handle a `PATH` with a bundled ACK (`BaseChatMesh::onContactPathRecv`). They store
  the path as `out_path`, call `processAck` on the extra, and, because it arrived flooded, send a
  reciprocal `PATH` back DIRECT (`Mesh.cpp:173-177`). The room already owns inbound `PATH` packets
  addressed to it and adopts them with `adopt_path_body`, so the member's route becomes a
  public-key-keyed claimed route.
- The logs cannot say which route an ACK took. `packet_tx` for `room_post_ack` carries no route and
  `room_post_stored` has none either.

## Goals / Non-Goals

**Goals:**
- A flooded post's ACK reaches a client that has no route to the room, and it teaches that client
  a route.
- The ACK route is visible in the log and on the monitor line.

**Non-Goals:**
- Changing ACK routing for ordinary direct messages (`net/dm.py`).
- Changing push routing, keep-alive, status or telemetry replies.
- Adding the firmware's extra "multi-ack" transmissions on direct routes.
- Changing the flood hash width for path returns. It stays `path_hash_size`, the same as login,
  whose test fixes that choice.

## Decisions

### D1. Choose the ACK form by how the post arrived, not by what the path store knows

A flooded arrival (`FLOOD` or `TRANSPORT_FLOOD`) gets a flooded path return. A direct arrival gets a
bare ACK on `_route_to(member)`. A client floods because it has no route to us, and a reversed flood
path is one-way evidence: the post got here over it, but that does not show our reply will get back
over it. This is the existing spec rule for requests ("a reply is routed the way the request
arrived"), which login already follows.

*Alternative: keep using the known route when one exists and flood only when none is known.*
Rejected. For zero-hop floods, the route learned from the post itself always exists, so this is
exactly today's failing behaviour.

### D2. Bundle the ACK in a path return instead of flooding a bare ACK

`simple_room_server` floods a bare ACK when it has no `out_path` (`MyMesh.cpp:494-497`). A path
return costs about 22 bytes instead of 6 (about 380 ms instead of 250 ms on air). In exchange the
client stores a route to the room and stops flooding its later posts, and each of those floods is
rebroadcast by every repeater. The user chose this deliberately (2026-09-24). It is recorded as a
divergence from firmware in DESIGN.md next to the "One thing sighop does that the firmware cannot"
paragraph.

Construction mirrors `_send_login_reply`: `ReturnedPathBody(hop_count, hash_size, path` from
`record.packet`, `extra_type=PayloadType.ACK, extra_ack=Acknowledgement(checksum))`. The body is
encrypted with the member's shared secret into a `PATH` packet addressed to `member.node_hash`, and
flooded at `path_hash_size`, at `PriorityClass.ACK` with origin `room_post_ack`. Where the two
functions build the same thing, a small shared helper builds the flooded path-return packet, so the
two cannot drift.

### D3. The checksum is unchanged

The checksum is still `ack_checksum_for(body, member.public_key)`, over the body as received.
Retries recompute it per attempt as they do today, so each retry's path return carries that
attempt's ACK.

### D4. Report the route

`_acknowledge_post` returns the route label along with whether it was sent. The labels are
`"PATH_RETURN"` for a path return, and `Route.label` (`"DIRECT h1"`, `"DIRECT h0?"`, `"FLOOD"`) for
a bare ACK. `room_post_stored` logs it as `ack_route`, `PostStored` gains `ack_route: str | None`
(None when not acknowledged), and `render_post_stored` appends it, for example
`... 'Qwerty123'  ack=PATH_RETURN`. The field is optional with a default, so existing constructors
and serialisers keep working.

## Risks / Trade-offs

- [The client ignores the path return, for example an older client that does not bundle-parse] →
  Stock `BaseChatMesh` has handled `PATH`+ACK since the path-return protocol existed, and the same
  code path already confirms login responses. The retry path still works: each retry gets a new
  path return.
- [The learned route later breaks and the client's DIRECT posts fail] → The stock client falls back
  to flooding after failed direct sends. Its next flooded post gets a path return again, which
  re-teaches it. This is the same exposure as the route learned at login.
- [Extra airtime from about 130 ms more per flooded ACK, plus the client's one reciprocal PATH] →
  Paid once per route discovery. It replaces full-mesh floods for every later post.
- [The diagnosis is not proven: the old log does not show which route was used] → D4 makes the next
  occurrence visible. The fix follows the spec rule whichever route was taken.

## Migration Plan

No data or config changes. Deploy and restart. Roll back by reverting the commit.
