## Context

See proposal.md — Why. What shapes the approach is what already exists and what the firmware
actually does.

**What exists.** `net/dm.py` is the working reference for an entity that answers traffic: a bus
subscriber that decrypts by trying candidate keys, acknowledges with the firmware's own hash
construction, routes through `net/paths.py`, and submits to the transmit scheduler under the
duty-cycle ceiling and the `--enable-transmit` gate. `protocol/payloads.py` already parses
`AnonRequestEnvelope`, `DirectEnvelope` and `ReturnedPathBody`, already carries
`TextType.SIGNED_PLAIN` with its 4-byte sender-key prefix, and already has
`parse_room_login_body` / `build_room_login_body` — the login body was decoded during milestone 1
and has been waiting for a server. `NodeType.ROOM_SERVER` exists, `entity_type_for()` already maps
it to the stored type `room_server`, and `PriorityClass.REPLY` is §4.3's class 1. Milestone 5 left
`entity`, `contact`, `path` and `packet_log` in place with a bounded write-behind writer, a
degraded-state probe and a pruner to copy the shape of.

**What the firmware does**, read from `examples/simple_room_server/MyMesh.cpp` and
`src/helpers/ClientACL.h` rather than from the payload documentation:

* Login is `onAnonDataRecv` (`:324-411`): two little-endian timestamps then a NUL-terminated
  password, admin password checked before guest, `allow_read_only` deciding whether a wrong
  password still gets in as a spectator, and **no reply at all** when none matches. The reply is 13
  bytes and goes back as a `PATH` return when the request arrived flooded, as a `RESPONSE` datagram
  otherwise.
* A post is an ordinary `TXT_MSG` with `TXT_TYPE_PLAIN` (`:441-537`), replay-guarded on the
  sender's own timestamp, acknowledged with `sha256(plaintext ‖ sender pubkey)[:4]`, and refused
  silently from a `PERM_ACL_GUEST` member.
* Sync is a round-robin push loop (`:995-1039`) over a 32-entry RAM ring, one outstanding
  acknowledgement per client, the cursor advancing only when that acknowledgement arrives
  (`:121-132`), the author never receiving its own post, and a post held back for
  `POST_SYNC_DELAY_SECS` before it is pushed at all.
* `ClientInfo` keeps `sync_since`, `last_timestamp`, `permissions` and `out_path` per client — the
  ACL is the routing table as well as the permission table.

**Constraints that are not negotiable here.** The reception path stays a pure decode (design D6 of
milestone 2): a replayed capture must keep reproducing every reception exactly, so nothing the room
server does may run inside `net/rx.py`. `protocol/` may not import `net/` or `db/`
(`tests/protocol/test_import_boundary.py`). No test may require Postgres. The transmit gate and the
airtime ceiling are untouched.

## Goals / Non-Goals

**Goals:**

- A room server that a **stock MeshCore client** can log in to, post to, and sync history from,
  proven on the air rather than against our own client.
- History that is durable and ordered, with a per-member cursor, so a member returning after a
  week gets what it missed and not merely the last 32 messages.
- An ACL that survives restart, and a password store from which a database dump yields nothing.
- Every transmission the room server makes traceable to a request it authenticated, bounded by a
  throttle, and counted.

**Non-Goals:**

- Being a *client* of someone else's room server (§7's companion, milestone 8).
- Remote administration over the air, transport-scoped flooding, region maps, channels/`GRP_TXT`,
  and `REQ_TYPE_GET_ACCESS_LIST`.
- Bit-compatibility with every historical firmware revision. We target the version in
  `related-repos/MeshCore` (`v1.17.1`, `FIRMWARE_VER_LEVEL 1`) and record where older clients
  differ rather than branching for them.
- Multi-room-per-entity. One room per room-server identity, because that is what the mesh sees: an
  identity is a node, and a node is a room.

## Decisions

### D1 — Argon2id from `nacl.pwhash`, verified off the event loop, with bounded concurrency

PyNaCl 1.6.2 is already a dependency and exposes libsodium's Argon2id as
`nacl.pwhash.argon2id.str()` / `nacl.pwhash.verify()`, producing the standard
`$argon2id$v=19$m=65536,t=2,p=1$…` encoded string — parameters, salt and tag in one column, so a
future parameter change needs no schema change. Alternatives: `argon2-cffi` (a new dependency for
something libsodium already gives us) and `cryptography`'s `Argon2id` (added in 44.0 and requiring
OpenSSL 3.2+, against our floor of `cryptography>=43`). Neither buys anything.

The consequence that matters is operational, not cryptographic: the interactive parameters
allocate **64 MiB and run for tens of milliseconds** per verification. Running that inline on the
asyncio loop would stall the RX pipeline for the duration, and running several concurrently would
allocate 64 MiB each. So every hash and every verification runs in `asyncio.to_thread`, and a
semaphore bounds how many may run at once (default 2). The login throttle in D8 is therefore also
the memory-exhaustion guard, which is the honest way to describe it.

### D2 — Three tables, shaped by the firmware rather than by §6's sketch

§6 called its eight-table list a sketch and predicted this milestone would change the shape. It
did, in three places: the ACL is also a routing and cursor table, the message table needs an
ordering that the wire protocol can name, and retention needs two independent bounds.

```
room          id, entity_id (unique FK -> entity), name,
              admin_password_hash, guest_password_hash NULL, guest_open,
              allow_read_only, retention_days NULL, retention_messages NULL,
              created_at
room_member   PK (room_id, public_key), node_hash, permissions,
              sync_since, last_timestamp, first_login, last_activity
message       id, room_id, author_public_key, post_timestamp, sender_timestamp NULL,
              text, posted_at        UNIQUE (room_id, post_timestamp)
```

`room_member.node_hash` is indexed and **not** unique, for the reason §3 gives and milestone 5
already applied to two tables: one byte of identity collides at 1 in 256 and the whole design is
built on candidate sets. `entity_id` *is* unique — one room per identity (see Non-Goals).
`guest_password_hash NULL` with `guest_open` false means guest logins are refused; `guest_open`
true with a NULL hash is §7's *"empty guest password is legal… but must be an explicit choice,
never the default"* expressed as a column an operator has to set on purpose. Every timestamp is
`TIMESTAMPTZ` (milestone 5 design D7); `sync_since`, `last_timestamp` and `post_timestamp` are
`BIGINT` because they are MeshCore's unsigned 32-bit epoch seconds *as they appear on the wire*,
not instants — mixing the two representations is how a comparison silently changes meaning.

### D3 — `post_timestamp` is a per-room strictly increasing second counter, and it is the cursor

The firmware stamps posts with `getCurrentTimeUnique()` and the entire sync protocol compares
against that value: the push carries it, the acknowledgement advances the client's `sync_since` to
it, and a keep-alive may force the cursor to a specific one. So the cursor has to be exactly this
value and it has to be a **total order** — two posts in the same second must not share a stamp.
sighop computes `max(now_unix, last_post_timestamp_for_this_room + 1)` and enforces it with
`UNIQUE (room_id, post_timestamp)`, so a clock that steps backwards produces a stall in stamping,
never a duplicate or a reordering.

Alternative considered: use the row id as the cursor and keep the timestamp cosmetic. Rejected —
the id is ours and the timestamp is the peer's; the peer compares timestamps, and a cursor the peer
cannot name is not a cursor.

### D4 — Message text is stored as bytes, and posts over 156 bytes are truncated and acknowledged

Text off the wire is `WireText`: not guaranteed valid UTF-8, and the corpus rule is that we record
what arrived rather than a rendering of it. A room server that re-transmits a post to every member
must reproduce it byte for byte, so `message.text` is `bytea` and rendering to a string happens at
the edges, exactly as `WireText` already does for names and messages.

**Revised after the milestone 6 live exercise**, twice: first the rule, then the number.

The original decision was to refuse an over-long post — no acknowledgement, a reported reason —
rather than store something unpushable, with the limit taken from the firmware's
`MAX_POST_TEXT_LEN (160-9)` = 151. The exercise showed both halves were wrong.

**The rule.** A stock client composed a 156-byte post, sighop refused it, and the client showed it
undelivered and retried. The firmware's receive path has no length check at all: `addPost` runs and
`send_ack = true` whatever the length (`:484-488`), with the acknowledgement computed over the
**full received** text (`:461-462`). On this protocol a refusal *is* silence, indistinguishable
from a lost packet, so refusing costs the message and the retries both. Truncating loses only the
tail — and only when there is a tail to lose.

**The number.** 151 turned out to be a convention rather than a limit, and so did the 150 that a
first reading replaced it with. Three numbers are actually in play:

- **167** — the hard ceiling from the packet format. A push payload is
  `dest_hash(1) + src_hash(1) + MAC(2) + ciphertext` within `MAX_PACKET_PAYLOAD` (184), so the
  ciphertext may be 180 rounded down to a whole cipher block, 176; the push plaintext spends 9
  bytes on timestamp, flags and author prefix before the text. Above this, no push exists.
- **156** — what a stock client can send *and be shown*. `queueMessage`
  (`companion_radio/MyMesh.cpp:432`) bounds the frame the radio hands the phone app against
  `MAX_FRAME_SIZE` (176), and a signed post to a v3 app spends
  `4 + 6 + 1 + 1 + 4 + 4 = 20` bytes of prefix. 176 − 20 = 156 — which is exactly the composer
  limit observed in the exercise. The same budget sizes both ends.
- **150** — what stock firmware keeps. `StrHelper::strncpy(posts[idx].text, postData,
  MAX_POST_TEXT_LEN)` (`MyMesh.cpp:57`) copies while `buf_sz > 1` (`TxtDataHelpers.cpp:3-9`), so
  the firmware passes the limit where `sizeof(buf)` was meant and lands one below the 151 its own
  constant reads as. 151 in turn is `MAX_TEXT_LEN` (160, ten cipher blocks chosen for *chat*
  messages) minus the push prefix.

We store **156**. It is the only one of the three derived from what a client can actually do, and
at 156 nothing a stock client composes is ever shortened — the truncation below guards only the
range between 156 and what a push can carry, which nothing stock reaches. A post over it is stored
shortened, acknowledged over the text as sent, and reported as truncated with the length it arrived
at, so the drop is visible to the operator even though the author cannot be told.

What this gives up: a room served by stock firmware truncates at 150, so a history that moves
between the two implementations is not byte-identical for posts of 151–156 bytes. That is worth
less than not mangling messages a client can legitimately send. The cost on the air is one extra
cipher block (16 bytes) on posts above 150, and nothing on any other post.

A post made *locally* by an operator is still refused, because that author is present and can
shorten it. The asymmetry is the point: truncation is what you do when you cannot ask.

### D5 — The database is the authority for history; memory is the authority for everything else

Milestone 5's design D2 made memory authoritative for contacts and paths because both are read
*inside* the reception path — a route lookup sits in packet composition and a contact lookup inside
the MAC trial — and a database round trip there would risk the replay-reproducibility property and
let an unreachable Postgres take the radio down.

History is different in exactly the way that matters: it is unbounded, it exists to outlive the
process, and it is read by the push loop, which is a background task nothing waits on. So messages
are read from and written to the database directly, with no in-memory mirror to fall out of sync.
The ACL is read at startup into memory (it is consulted per packet, in the MAC trial) and written
through on change, which is milestone 5's contact policy applied to the same kind of data.

This makes rooms require a database, which is stated rather than worked around: a room is bound to
a stored entity, stored entities require a database, and `sighop run` without one says there are no
rooms rather than pretending to serve one.

### D6 — A post is acknowledged only once it is stored, and the store attempt is bounded by the sender's own acknowledgement window

The firmware acknowledges after putting the post in a RAM ring, which cannot fail. Ours can. An
acknowledgement is a promise the client will not retry and will show the message as delivered, so
sending it before the row lands would make sighop lie about the one property the milestone exists
to provide.

So: the inbound handler starts a task, the task inserts the row with a deadline derived from the
peer's own acknowledgement timeout (`dm.py`'s `ack_timeout_ms`, itself copied from
`MyMesh.cpp:851`), and the acknowledgement is submitted only after the insert succeeds. If it does
not land in time, nothing is sent, the client retries, and its own UI reports the failure honestly.
The insert never runs on the bus handler and never blocks a reception. While the database is
degraded, a room accepts nothing and says so in the status line — a room server is exactly as
available as its history, which is the price of the history being real.

### D7 — Reply routing follows the request, and a flood reply is a reply, never an origination

A login that arrived flooded is answered with an encrypted `PATH` return carrying the 13-byte
`RESPONSE` as its embedded payload (`Mesh.cpp:449-486`: packed hash-size/hop-count byte, the
received path, then the extra type and body, all encrypt-then-MAC'd), so the client learns a route
home in the same packet that tells it the login succeeded. A login that arrived direct is answered
with a `RESPONSE` datagram along the member's stored out-path, or flooded if there is none.

Milestone 4's design D4 — *"flooding requires an explicit flag"* — governs **originated** traffic,
where the failure mode was an operator mistyping a peer name and flooding the mesh. A reply to a
request that arrived flooded has no other way home, and the firmware does the same. The safety it
gives up is bought back in D8 rather than by refusing to answer.

Replies are sent **unscoped** (no transport codes). Region maps and transport keys are out of
scope, and the consequence is stated so the exercise can observe it: a repeater configured with
`flood.max.unscoped=0` drops an unscoped flood at hop 0, so a room server reachable only through
such a repeater will not complete a flooded login.

### D8 — A failed login is answered with silence; a successful one is throttled, counted and reported

The firmware returns without replying when no password matches, and that behaviour is load-bearing
for us: it means **an unauthenticated stranger cannot make sighop transmit**, which is the same
rule §7 states for the greeter bot — *"a spoofed advert can make sighop transmit on demand"* — one
milestone early.

A *successful* login can, and so can a replayed one (D9), so the reply path is bounded three ways:
a per-source-node-hash token bucket, a global reply rate, and the Argon2 concurrency semaphore from
D1. Each refusal is counted by reason and reported in the status line, because a throttle that
drops silently is indistinguishable from a mesh that went quiet. Chosen over the alternative of
trusting the duty-cycle ceiling alone: the ceiling bounds *airtime*, not how much of it one
stranger may claim, and it would let a login flood crowd out acknowledgements the whole system
depends on.

### D9 — The replay guard persists, and the firmware's empty-password re-login is matched deliberately

`room_member.last_timestamp` is written through, so the replay window a restart would open — the
firmware's copy is transient and clears on reboot — is closed. That is the point of a durable ACL.

The trade-off is real and is stated rather than hidden: a member whose clock steps backwards (a
factory reset, an RTC that lost power) is refused until an operator runs `sighop room revoke` and
lets it log in fresh. The CLI says exactly that when it revokes.

One firmware behaviour is matched on purpose even though it looks like a hole: an *existing* member
logging in with an **empty** password is answered without a timestamp check (`MyMesh.cpp:335-342`
short-circuits before the check). It is how a client re-establishes a lost path, a client that
cannot re-establish a path is a client that has silently left the room, and diverging here would
break interop with every stock client. It is also the single most replayable packet in the
protocol, which is precisely why D8's throttle exists.

### D10 — An entity is a room server or it is not, and its packets belong to exactly one subscriber

`DirectMessenger` filters inbound `TXT_MSG` by `entity.node_hash == envelope.dest_hash` across
every local entity. A room-server entity is in that list, so without a rule both subscribers would
decrypt a member's post and both would acknowledge it: two decryptions, two acknowledgements on the
air, two contradictory log lines.

The rule is static and needs no coordination: an entity claimed by a room server is skipped by the
direct messenger, and the room server handles `TXT_MSG`, `ANON_REQ`, `REQ` and `PATH` addressed to
it. Alternative considered: let both run and suppress the duplicate acknowledgement downstream —
rejected, because the ambiguity is resolvable at wiring time and a duplicate suppressed after the
fact is still a second decryption of a message with a different meaning.

### D11 — One acknowledgement registry, shared

`dm.py` owns `self._outstanding`, a map from expected checksum to the send waiting on it, and logs
`ack_unmatched` for anything absent from it. A room server waiting on push acknowledgements would
make every one of its matches look unmatched to the direct messenger, and vice versa.

So the table moves to `net/acks.py`: expectations are registered with an owner, one bus subscriber
matches an inbound acknowledgement against it, and `unmatched` regains its meaning — nobody in this
process was waiting for that. It is also the one place the ACK-carried-inside-a-`PATH` case from
D12 needs to reach, rather than two.

### D12 — Inbound `PATH` bodies are decrypted, for the acknowledgement inside them as much as the route

Path learning today is reverse-path learning from the frame a reception arrived on. A `PATH` payload
carries the peer's *explicit* statement of the route, and — critically — the firmware puts an
acknowledgement inside it (`MyMesh.cpp:601-620`): a client that answers a flooded push with a path
return encodes the ACK as the return's extra payload. Without decrypting that body, the
acknowledgement is invisible, the push is retried three times for nothing, and the member's cursor
never advances, which would look exactly like a client that is not receiving.

Decryption uses the same MAC trial as a text message, with the same caveat carried through
unchanged from milestone 4's design D7: **a MAC match selects a key, it never authenticates a
sender.** The learned route is recorded as a candidate keyed by the matched public key and marked
claimed, not proven, and `most-recently-confirmed-wins` is untouched.

### D13 — The status answer is the room server's 52-byte struct, and a known client-side disagreement is recorded rather than smoothed over

`REQ_TYPE_GET_STATUS` is answered with the sender's timestamp echoed back as a tag, then
`ServerStats` exactly as `examples/simple_room_server/MyMesh.cpp:24-39` lays it out — 52 bytes,
little-endian, naturally aligned with no padding: four 16-bit fields, eight 32-bit fields, then six
16-bit fields ending in `n_posted` and `n_post_push`.

The disagreement worth recording: `meshcore_py`'s generic `parse_status` reads offsets 48..52 as
`rx_airtime`, which is what a *repeater* puts there — the room-server firmware puts `n_posted` and
`n_post_push` there instead. We emit what the room-server firmware emits, because interop is with
the firmware, and a client using the generic parser will misread those four bytes against a stock
room server exactly as it will against ours.

What sighop can honestly fill is not the same set a board can, and the answer says so by
construction rather than by inventing values. Counters that describe the **shared radio** —
packets sent and received, flood and direct splits, total airtime, uptime, duplicates, transmit
queue depth — are runtime-wide, because one modem serves every entity in the process and there is
no per-entity radio to report. `n_posted` and `n_post_push` are per room. Battery and MCU
temperature come from the §4.1 probe readback, which is a real value the board gave us; the noise
floor has no equivalent on a KISS modem and is reported as zero, which is what the field's absence
looks like on the wire. None of this is guessed and all of it is documented in the spec, because a
statistic that quietly means something else is worse than one that is missing.

### D14 — Telemetry is a hand-rolled CayenneLPP frame, big-endian, on channel 1

`REQ_TYPE_GET_TELEMETRY_DATA` is answered with the echoed timestamp then an LPP frame: for each
value, one channel byte, one type byte, then the value. sighop emits channel 1 (`TELEM_CHANNEL_SELF`)
with voltage (type `0x74`, unsigned 16-bit, 0.01 V) and, when the board answered `GetMCUTemp`,
temperature (type `0x67`, signed 16-bit, 0.1 °C).

Two details are worth fixing in writing because they are the ones that get built wrong. **LPP values
are big-endian**, while every other integer in MeshCore is little-endian; a frame built with the
project's usual byte order parses as garbage. And the request's second byte is an inverse
permission mask for *external sensors* — sighop has none, so the mask is parsed, has nothing to
gate, and a guest gets the same base frame as a member; that is stated rather than left as an
apparent oversight.

Adding a CayenneLPP library for two value types is not worth a dependency; the encoder is a dozen
lines and lives in `protocol/payloads.py` with the rest of the byte work.

### D15 — Retention is built, defaults to unlimited, and the gap it can open is stated

Both bounds (`retention_days`, `retention_messages`) are NULL by default, so nothing is deleted
until an operator sets a policy. §13's unknown #1 says the sensible default *"depends on observed
message volume"* and this is the first milestone that can observe any; shipping a default that
deletes history before the question has been asked would answer it by accident.

The pruner copies `PacketLogPruner`'s shape — a periodic task, a deletion count, a reported
counter — and runs per room. The consequence that must not be discovered later: a member whose
`sync_since` predates what retention has removed silently misses those messages, because the push
loop can only send rows that exist. Retention wins over sync, deliberately, and the count of
messages deleted while at least one member was still behind them is reported so the operator sees
the trade being made.

### D16 — Passwords never reach `argv`, and rotation evicts nobody

`sighop room create` and `sighop room passwd` read passwords from a prompt or `--password-stdin`,
never from a command-line argument, because `ps` publishes `argv` to every user on the host. The
CLI prints §7's counterintuitive rule at the moment it matters — *"rotation evicts nobody"* —
because the ACL stores public keys after first login, so a rotated password gates only new joins
and removing a member takes an explicit `sighop room revoke`.

### D17 — `net/room.py` is a bus subscriber shaped like `net/dm.py`

Same seams, for the same reasons: policy in `net/`, no imports from `monitor/`, nothing inside the
decode stage, submissions through the scheduler with a deadline and a priority class, typed events
emitted for `monitor/render.py` to format. `protocol/` gains byte codecs and learns nothing about
rooms, members or storage; `db/` gains repositories beside milestone 5's four. The push loop is a
runtime task like the advert loop, not a thread.

## Risks / Trade-offs

- **Argon2id allocates 64 MiB per verification** → verified in a worker thread, bounded by a
  semaphore, and fronted by D8's throttle; a login storm costs a bounded amount of memory and a
  counted number of refusals rather than the process.
- **A successful login makes sighop transmit at a stranger's request** → authentication first,
  silence on failure, per-source and global throttles, priority class `REPLY`, the duty-cycle
  ceiling, and the transmit gate still off by default. Every reply is counted and reported.
- **A persisted replay guard can lock out a member whose clock resets** → `sighop room revoke`,
  and the CLI says so; the alternative is a replay window at every restart, which is worse.
- **A room is only as available as its database** → posts are not acknowledged while it is
  degraded, so the client reports the failure rather than losing the message silently. Accepted
  deliberately: the milestone's whole claim is that history is durable.
- **Retention can delete messages a member has not synced** → both bounds default to unlimited,
  and when a policy is set the pruner counts deletions that outran a member's cursor.
- **Unscoped flood replies may be dropped by scoped repeaters** → out of scope, recorded, and
  observable in the live exercise rather than assumed either way.
- **The 151-byte post limit is smaller than the 160-byte message limit** → refused at acceptance
  with a reason, never truncated.
- **Older clients read byte 7 of the login response as an unsynced count**, where `v1.17.1` puts
  the permissions byte → we emit the current form and record the divergence; a client old enough to
  disagree will show a wrong badge, not fail to log in.
- **The live exercise puts sighop on the air answering strangers** → the exercise room is created
  with a guest password rather than opened, the session is short, transmit is enabled explicitly
  for it, and the whole capture is appended to the corpus with its provenance header.

## Migration Plan

1. `alembic/versions/0002` adds `room`, `room_member` and `message` and drops them in
   `downgrade()`. There is no data migration: none of the three exists anywhere yet, and the four
   built in `0001` are untouched.
2. `0002`'s docstring updates `0001`'s note about deliberately absent tables, leaving `bot_state`
   named as milestone 7's.
3. `sighop db upgrade` applies it; `sighop run` continues to refuse a database that is not at the
   revision the code expects, so a half-migrated deployment fails at startup rather than at the
   first login.
4. Rollout for the exercise: create the entity, create the room with a guest password, run
   receive-only to confirm the advert is seen by the client, then enable transmit for the login and
   sync run.
5. Rollback is `sighop db downgrade` plus removing the room-server entity; no other component reads
   the three tables, and a runtime with no room rows behaves exactly as milestone 5's does.

## Open Questions

- **What retention policy is sensible** (§13 unknown #1). Answerable only from observed volume, and
  deliberately not guessed: the defaults are unlimited and the milestone reports what it saw. If
  the exercise is too short to say anything, that is recorded as a non-observation rather than as
  confirmation, in the same terms milestone 5 used for path candidates.
- **Whether a peer that learns us only from a zero-hop advert floods its replies** (§13 unknown
  #4, open since milestone 4). A room login is exactly that exchange, so the exercise is the first
  real chance to observe it; the answer changes nothing in these specs, only what a later milestone
  does about soliciting a path.
- **Whether push pacing needs to adapt to the mesh.** The firmware's 1.2 s round-robin interval is
  copied as-is. Whether that is right when several members are far behind at once is not
  observable until several members are, and the airtime ceiling bounds the damage meanwhile.
