## Why

sighop can hear the mesh, be heard by it, and remember what it heard. It cannot yet *be* anything
to anyone. Every entity it runs is a chat node that answers a direct message; DESIGN.md §12's
milestone 6 is the first one that gives an entity a **role** — a room server, with members, a
history and a sync protocol — and §6 states the reason plainly: *"message history is durable and
survives reboot, per spec — the whole reason a room server beats a walkie-talkie."*

Milestone 5 built the floor and stopped exactly here: four of §6's eight tables, with `room`,
`room_member` and `message` deliberately left out because *"milestone 6 will discover things about
ACLs and retention that change those four tables."* This is that milestone. It is also the first
one where sighop transmits *because a stranger asked it to* — a login reply, an acknowledgement, a
history push — which is a different risk posture from milestone 4's operator-typed `--send`, and
the design has to answer for it.

## What Changes

- **A room server entity.** A stored entity of type `room_server` adverts with MeshCore's room
  node type (`0x03`), accepts logins, stores what members post, pushes history to members who are
  behind, and answers status and telemetry requests. Everything it puts on the air goes through
  the existing transmit scheduler under the existing duty-cycle ceiling and the existing
  `--enable-transmit` gate; nothing about the radio's safety posture changes.
- **Login over `ANON_REQ`, with a persistent ACL.** §7's flow, implemented against the firmware
  rather than against the payload documentation: an `ANON_REQ` carrying the sender's full public
  key and an encrypted `timestamp ‖ sync_timestamp ‖ password`, validated against the room's guest
  or admin password, answered with a 13-byte `RESPONSE` — or, when the login arrived flooded, with
  an encrypted `PATH` return carrying that `RESPONSE` inside it, so the client learns a route home
  in the same packet. The member's public key and permission level are stored, so *"subsequent
  traffic arrives as ordinary `REQ`, no re-login"* and a restart does not make every member
  re-authenticate.
- **Argon2id password hashes, never plaintext.** From `nacl.pwhash` — libsodium, already a
  dependency, no new one added. A distinct admin password per room-server entity, per §7, because
  *"a shared admin credential would mean compromising one exposes all."* An empty guest password
  is legal and must be chosen explicitly; it is never a default. Passwords are never accepted on
  the command line, where `ps` would publish them.
- **Three tables, in migration `0002`.** `room`, `room_member` and `message` — the three §6
  assigned to this milestone, each shipping with behaviour and tests behind it, and each shaped by
  what the firmware actually does rather than by the sketch.
- **Durable history and a per-member sync cursor.** The firmware holds 32 posts in a RAM ring and
  loses them on reboot; sighop stores them in Postgres and keeps each member's cursor in
  `room_member`, so a member returning after a week gets everything it missed rather than the last
  32 messages. Pushes are paced and round-robin across members, one outstanding acknowledgement at
  a time, at scheduler priority 1 (`REPLY`) as §7 requires — *"a member returning after a week must
  not monopolize the channel."*
- **Retention exists and defaults to unlimited.** Per-room age and count policies with a periodic
  pruner, both bounds NULL out of the box: nothing is ever deleted until an operator sets a policy.
  §13's unknown #1 says the sensible default *"depends on observed message volume"*, and this
  milestone is the first that can observe any; a default that silently deletes history before the
  question has been answered is the wrong way round.
- **A request surface: keep-alive, status, telemetry.** `REQ_TYPE_KEEP_ALIVE` (0x02) answered with
  an acknowledgement carrying the unsynced count, `REQ_TYPE_GET_STATUS` (0x01) with the room
  server's own 52-byte statistics struct, and `REQ_TYPE_GET_TELEMETRY_DATA` (0x03) with a
  CayenneLPP frame. Values sighop genuinely has are reported; values a KISS modem cannot give are
  reported as absent rather than invented.
- **Answering a stranger is rate-limited and never free.** A login that fails is answered with
  silence, exactly as the firmware does, so a wrong password cannot make sighop transmit. A login
  that succeeds can, so replies are throttled per source node hash and globally, counted, and
  reported — a room server must not be usable as a flood amplifier.
- **Inbound `PATH` bodies are decrypted.** A client that answers a flooded push with a path return
  carries our acknowledgement *inside* it (`MyMesh.cpp:601-620`). Without decrypting that body the
  acknowledgement is invisible and the push is retried three times for nothing, so this milestone
  learns the explicit out-path and the embedded acknowledgement from it.
- **One acknowledgement registry, not two.** The room server and the direct messenger both wait on
  acknowledgements and both would otherwise log every one of the other's as unmatched. The
  outstanding-expectation table moves into one place both register with.
- **`sighop room` command surface.** Create a room on an entity, set and rotate passwords, list and
  revoke members, post as the server, read history, set retention. Plus `sighop run` reporting
  which rooms are active, how many members and messages each holds, and what its retention policy
  is.
- **A live exercise on real air, appended to the corpus.** The exit criterion is a stock MeshCore
  client logging in to a sighop room server over the air, posting a message, and — after sighop is
  restarted — receiving the history it missed. The session is appended whole to the corpus with its
  own provenance header, as every session since milestone 2 has been.

## Capabilities

### New Capabilities
- `room-acl`: membership and authentication for a room server — the `ANON_REQ` login exchange, the
  guest/admin/read-only permission levels, Argon2id password storage and rotation that evicts
  nobody, the replay guard, the rule that a failed login is answered with silence, the throttle
  that keeps a successful one from being an amplifier, revocation, and the durability that makes
  *"ACLs must survive restart or every member re-authenticates"* true.
- `room-history`: what a room stores and how it syncs — which posts are accepted and from whom,
  the durable ordered history and its per-room timestamp ordering, the per-member sync cursor that
  only advances on an acknowledgement, the paced round-robin push and its priority and airtime
  behaviour, the rule that an author is not sent its own post, and retention policy and pruning.
- `room-server`: the entity itself — adverting as a room server, binding a room to a stored
  entity, the request surface (keep-alive, status, telemetry) and what each answer may and may not
  claim, which entity owns an inbound packet, and what a run reports about its rooms.

### Modified Capabilities
- `payload-codec`: gains the bodies this milestone puts on the wire — the room login `RESPONSE`,
  the keep-alive request, the room server's statistics structure, a CayenneLPP telemetry frame, and
  building (not just parsing) an encrypted `PATH` return with an embedded payload. Parsing and
  building stay pure functions over bytes with no new dependency on `net/` or `db/`.
- `direct-messaging`: an inbound direct message addressed to an entity a room server owns is that
  room server's, not the direct messenger's, so exactly one subscriber decrypts it and exactly one
  acknowledgement is sent. Outstanding acknowledgement expectations move to a shared registry, so
  "unmatched" keeps meaning unmatched.
- `path-learning`: an out-path may now also be learned from a decrypted `PATH` body — the explicit
  form a peer sends — in addition to the reverse path of a reception. Most-recently-confirmed-wins
  and the candidate-set rules are unchanged.
- `entity-store`: an entity may be created as a room server, carrying MeshCore's room node type in
  its advert config and `room_server` as its stored type, and a room is bound to exactly one such
  entity.
- `runtime-cli`: `sighop room` is added, and `sighop run` reports the rooms it is serving, their
  membership and history counts, and their retention policy — including saying plainly that
  without a database there are no rooms.

## Impact

- **No new dependencies.** Argon2id comes from `nacl.pwhash` (PyNaCl 1.6.2, already required for
  §5's sealing and identity code), and CayenneLPP is a dozen lines of encoding rather than a
  library. The `cryptography` and `sqlalchemy`/`asyncpg`/`alembic` stacks are unchanged.
- **New code:** `src/sighop/net/room.py` (the server: login, posts, push loop, requests),
  `src/sighop/net/acks.py` (the shared expectation registry), room models in
  `src/sighop/db/models.py` with repositories beside the existing four, `alembic/versions/0002`,
  telemetry and status body codecs in `src/sighop/protocol/payloads.py`, and `sighop room` in
  `src/sighop/cli.py`.
- **Modified code:** `src/sighop/net/dm.py` (entity ownership, shared ack registry),
  `src/sighop/net/paths.py` (learning from an explicit path body), `src/sighop/runtime.py` (wire
  and start room servers, report them), `src/sighop/db/persistence.py` (room repositories and the
  retention pruner beside the packet-log one), `src/sighop/monitor/render.py` (room lines in
  startup and status output).
- **`protocol/` gains code but no dependencies.** `tests/protocol/test_import_boundary.py` must
  keep passing unchanged: the status struct, the login response and the telemetry frame are byte
  encodings, and nothing in `protocol/` learns about rooms, members or the database.
- **No test may require Postgres to pass.** Room tests that need storage isolate in a throwaway
  schema and skip without a database URL, exactly as milestone 5's do. The login, push and request
  codecs are testable with no database at all.
- **The safety posture is unchanged and one property is added.** `--enable-transmit` stays off by
  default, the duty-cycle ceiling and priority classes are untouched, and a login sighop did not
  authenticate produces no transmission at all.
- **DESIGN.md is updated in this change**, per its own rule: §6's table list moves three rows from
  "not built yet" to built and records what the ACL and retention shapes turned out to be, §7's
  room-server section records what the firmware does where it differs from the section's summary,
  §12's milestone 6 entry records the exercise and its findings, and §13's unknown #1 is answered
  with observed volume or explicitly left open with the measurement that failed to settle it. §13
  unknown #4 — whether a peer that learns us only from a zero-hop advert floods its replies — is
  finally cheap to observe here, because a room login is exactly that exchange.
- **Out of scope, deliberately:** the client side of room membership — sighop joining *someone
  else's* room server — which belongs to §7's companion and milestone 8; admin CLI over the mesh
  (`TXT_TYPE_CLI_DATA`), which would make remote reconfiguration reachable over the air;
  `REQ_TYPE_GET_ACCESS_LIST`; transport-scoped flooding and region maps; channels and group
  messages (`GRP_TXT`); `bot_state` and bots (milestone 7); the WebUI (milestone 8); and path
  scoring beyond most-recently-confirmed-wins (§13 unknown #3).
