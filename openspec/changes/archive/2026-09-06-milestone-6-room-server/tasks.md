## 1. Schema and migration (`room-acl`, `room-history`)

- [x] 1.1 Add the `room` model — id, unique `entity_id` foreign key to `entity`, name, `admin_password_hash`, nullable `guest_password_hash`, `guest_open`, `allow_read_only`, nullable `retention_days` and `retention_messages`, `created_at` — with every timestamp `TIMESTAMPTZ` (milestone 5 design D7); verify a unit test asserts the unique constraint on `entity_id` and that both retention columns are nullable and default to NULL
- [x] 1.2 Add the `room_member` model with primary key `(room_id, public_key)`, `node_hash` **indexed but not unique** (§3, design D2), `permissions`, `sync_since`, `last_timestamp`, `first_login`, `last_activity`; verify a test asserts two members of one room may share a node hash and that the same public key cannot be inserted twice into one room
- [x] 1.3 Add the `message` model — id, `room_id`, `author_public_key`, `post_timestamp`, nullable `sender_timestamp`, `text` as bytes (design D4), `posted_at` — with `UNIQUE (room_id, post_timestamp)` and an index on `(room_id, post_timestamp)`; verify a test asserts the unique constraint rejects a second post with the same ordering value in one room and accepts it in another
- [x] 1.4 Write migration `0002` creating the three tables with `ON DELETE CASCADE` from `room` to `room_member` and `message`, and a `downgrade()` that drops them; verify an upgrade→downgrade→upgrade cycle in a throwaway schema leaves the schema at head with no leftover objects
- [x] 1.5 Update `0002`'s docstring to record which of §6's tables now exist and that `bot_state` remains milestone 7's, so absence still reads as intent; verify by review of the migration file
- [x] 1.6 Verify the schema-version check refuses a database at `0001` when the code expects `0002`, naming both revisions and the command that reconciles them, by a test that stamps `0001` and asserts startup fails with that message

## 2. Password storage and verification (`room-acl`)

- [x] 2.1 Implement password hashing and verification over `nacl.pwhash.argon2id` producing the encoded `$argon2id$…` string (design D1); verify round-trip, a wrong-password rejection, and a test asserting the stored value contains neither the password nor anything derivable from it
- [x] 2.2 Run every hash and verification in a worker thread with a bounded concurrency semaphore (default 2); verify a test asserts the event loop stays responsive during a verification and that concurrent verifications above the bound queue rather than run
- [x] 2.3 Make the "no password configured" and "empty password" cases distinct in storage and in behaviour: a NULL guest hash with `guest_open` false refuses guest logins, `guest_open` true admits an empty password (design D2); verify a test per case
- [x] 2.4 Assert no password is rendered anywhere: `repr`, wide events, error messages, status output and CLI output; verify a test creates a room with a known password and asserts the string appears in none of them

## 3. Payload codecs (`payload-codec`)

- [x] 3.1 Add the room login response body — server timestamp, result code, legacy interval byte, client-kind byte, permission byte, four random bytes, protocol level — with build and parse; verify a round-trip test and a hand-built 13-byte vector taken from `MyMesh.cpp:382-391`
- [x] 3.2 Add request body parse/build: 4-byte sender timestamp, request-type byte, remaining arguments preserved for types not interpreted; verify tests for a keep-alive with a position, one without, and an uninterpreted type whose arguments survive
- [x] 3.3 Add the room server statistics body in the exact 52-byte layout of `MyMesh.cpp:24-39` behind the 4-byte echoed timestamp; verify a test asserts the total is 56 bytes, asserts each field's offset against the struct, and round-trips every field
- [x] 3.4 Record in the statistics codec's documentation that offsets 48..52 are the posted and pushed counters for a room server where a generic client parser reads a receive-airtime value (design D13); verify by review, and by a test naming both interpretations so the divergence cannot be silently "fixed" later
- [x] 3.5 Add the CayenneLPP telemetry frame builder — channel byte, type byte, value **most significant byte first** — with voltage (`0x74`, unsigned, 0.01 V) and temperature (`0x67`, signed, 0.1 °C) (design D14); verify tests for a voltage entry, a negative temperature, an empty frame, and one asserting the byte order is big-endian against a hand-built vector
- [x] 3.6 Extend returned-path building to emit a bundled extra payload of a stated type after the path hashes; verify a build→parse round trip carrying a login response as the bundled payload, and one carrying an acknowledgement
- [x] 3.7 Verify `tests/protocol/test_import_boundary.py` still passes unchanged: nothing added in this group imports `net/` or `db/`

## 4. Shared acknowledgement registry and packet ownership (`direct-messaging`, `room-server`)

- [x] 4.1 Extract the outstanding-expectation table from the direct messenger into a shared registry keyed by expected checksum and carrying an owner (design D11); verify the existing direct-message tests pass unchanged against it
- [x] 4.2 Make one bus subscriber match inbound acknowledgements against the shared registry and dispatch to the owner, reporting `unmatched` only when no owner was waiting; verify a test with two registered owners asserts each match reaches its own owner and neither is reported unmatched
- [x] 4.3 Add the rule that a local entity serving a room is excluded from the direct messenger's candidate entities (design D10); verify a test sends a text message to a room-server entity and asserts the direct messenger neither decrypts it nor acknowledges it, and a second test asserts an ordinary entity sharing that node hash is still tried
- [x] 4.4 Verify exactly one acknowledgement reaches the scheduler for a post addressed to a room server, by a test counting submissions with both subscribers wired

## 5. Inbound path bodies (`path-learning`)

- [x] 5.1 Decrypt inbound `PATH` payloads addressed to a local entity using the same candidate-key trial as a text message, reporting the candidate count on failure (design D12); verify tests for a successful trial, a failed one, and that the decode stage is unchanged
- [x] 5.2 Record the route a decrypted path body declares as a candidate keyed by the matched public key and marked claimed, subject to the existing candidate limits and most-recently-confirmed-wins; verify a test asserts a path-body route and a reverse-learned route coexist as candidates and the newer one is selected
- [x] 5.3 Deliver a payload bundled inside a decrypted path body for handling as though it had arrived alone, so a bundled acknowledgement resolves an outstanding delivery; verify a test pushes a message, answers it with a path return carrying the acknowledgement, and asserts the delivery resolves rather than retrying

## 6. Login and the ACL (`room-acl`, `room-server`)

- [x] 6.1 Add the room server as a bus subscriber handling `ANON_REQ`, `TXT_MSG`, `REQ` and `PATH` addressed to its entity's node hash, holding no state in the decode stage (design D17); verify a replay of the corpus produces the same decode records as before the subscriber existed
- [x] 6.2 Implement login: derive the shared secret from the public key the envelope carries, verify the MAC, decrypt, and read the two timestamps and the password; verify a test builds a login with a known key pair and asserts the body is read correctly, and a second asserts a failed MAC produces no member and no transmission
- [x] 6.3 Implement password evaluation in the firmware's order — admin, then guest, then read-only if allowed, then nothing at all (`MyMesh.cpp:343-356`); verify a test per branch, including one asserting a wrong password with read-only disallowed transmits nothing
- [x] 6.4 Create or update the member row on success with public key, node hash, permissions, sync position from the login's sync timestamp, and replay timestamp, writing through to the database; verify a test asserts the row exists after the login and carries the admitted permission level
- [x] 6.5 Implement the replay guard on `last_timestamp`, persisted, with the firmware's empty-password exception for existing members (design D9); verify tests for a replayed login refused, a refusal that survives a restart, and an existing member's empty-password re-login answered
- [x] 6.6 Build the login reply: a returned path carrying the response when the request arrived flooded, a response datagram otherwise, routed to the member's known route when there is one (design D7); verify tests for both shapes asserting the packet type, the route type and the embedded response bytes
- [x] 6.7 Implement the reply throttle — per source node hash, global, and the Argon2 concurrency bound — counting every refusal by reason (design D8); verify tests for a per-source burst, a multi-source burst, and that counters are exposed
- [x] 6.8 Load every room's members at startup into memory and answer per-packet membership lookups from memory, writing through on change (design D5); verify a test asserts a member that logged in before a restart is recognised after it with no further login
- [x] 6.9 Implement revocation removing membership, permissions, sync position and replay guard; verify a test asserts a revoked member is unknown and is admitted again only by a successful login

## 7. Posts and durable history (`room-history`)

- [x] 7.1 Accept a `TXT_MSG` from a member with posting permission as a post, refusing one from a read-only member with no acknowledgement and a reported reason; verify a test per permission level
- [x] 7.2 Assign each post a room-scoped ordering value computed as `max(now, last + 1)` and enforced by the unique constraint (design D3); verify tests for two posts in one second, a clock that steps backwards, and continuation of ordering after a restart
- [x] 7.3 Store the post with author public key, raw text bytes, the sender's claimed timestamp and the room's ordering value; verify a test stores text that is not valid UTF-8 and asserts the bytes come back unchanged
- [x] 7.4 Truncate a post longer than a stock client can compose and display (156 bytes, `STORED_POST_TEXT_LEN` = `MAX_FRAME_SIZE` less the companion frame's prefix) and acknowledge it over the text as received, reporting the length it arrived at, while refusing an over-long *local* post whose author can be told (design D4, revised after the live exercise); verify a boundary test at 156, 157 and 160 bytes, a test that the acknowledgement matches the sender's own checksum over the untruncated text, and tests deriving 156 from the client's frame budget and `MAX_PUSHABLE_TEXT_LEN` (167) from the packet format
- [x] 7.5 Acknowledge a post only after the row lands, with the insert running off the bus handler and bounded by the sender's own acknowledgement window (design D6); verify a test with a storage sink that never answers asserts no acknowledgement is submitted and the reception path is not delayed
- [x] 7.6 Handle a retried post — same sender timestamp — by acknowledging again and storing once; verify a test asserts one row and two acknowledgements
- [x] 7.7 Report a room as refusing posts while its storage is degraded, in the status line; verify a test asserts the state appears and clears with the database's own degraded flag

## 8. History sync (`room-history`)

- [x] 8.1 Add the push loop as a runtime task: round-robin over members, one outstanding delivery per member, holding a new post for the reference implementation's delay before it becomes eligible, and never delivering a post to its author (`MyMesh.cpp:995-1039`); verify tests for alternation between two behind members, for the author suppression, and for the hold
- [x] 8.2 Compose a push body as timestamp, flags carrying the signed-plain text type, the author's 4-byte key prefix and the text, with a random attempt value for hash uniqueness (`MyMesh.cpp:69-108`); verify a hand-built vector test and one asserting two pushes of the same post differ in their packet hash
- [x] 8.3 Register each push's expected acknowledgement in the shared registry, computed over exactly the transmitted plaintext with the member's public key; verify a test asserts the expectation matches an acknowledgement the reference construction would produce
- [x] 8.4 Advance and persist a member's sync position only when its delivery is acknowledged; verify tests for an acknowledged delivery advancing it, an unacknowledged one leaving it, and the position surviving a restart
- [x] 8.5 Submit deliveries at the reply priority class with a deadline derived from the peer's own timeout formula, flooded or direct according to the member's known route; verify a test asserts the priority class and that a gated run resolves them as suppressed with no position advanced
- [x] 8.6 Bound consecutive unacknowledged deliveries per member, stop delivering at the bound, resume when the member is next heard from, and report how many members are in that state; verify a test drives three failures and asserts delivery stops and then resumes
- [x] 8.7 Adopt a sync position supplied by a member in a login or keep-alive, so a client that reset its history resynchronises; verify a test asserts a forced position causes redelivery from that point

## 9. Request surface (`room-server`)

- [x] 9.1 Handle keep-alive: acknowledge over the request bytes, append the member's unsynced count, adopt a supplied position, record activity, and answer only along a known route (`MyMesh.cpp:550-576`); verify tests for the acknowledgement construction, the appended count, and silence when no route is known
- [x] 9.2 Answer a status request with the statistics body, filling shared-radio counters from the runtime and post counters from the room (design D13); verify a test asserts the runtime counters match the scheduler and dedup statistics and the post counters match the room's
- [x] 9.3 Report measurements a KISS modem cannot provide in the way the wire format expresses absence, and document which fields those are; verify a test asserts the documented fields and that nothing is invented for them
- [x] 9.4 Answer a telemetry request with a frame carrying only values the board reported, from the startup probe readback, omitting any the board did not answer (design D14); verify tests for a board that answered both, one that answered neither, and a guest receiving the same base frame
- [x] 9.5 Transmit nothing for an unimplemented request type or one a member's permission level disallows, reporting the type and the reason; verify a test asserts no submission is made and the reason is reported

## 10. Retention (`room-history`)

- [x] 10.1 Implement the per-room retention pruner as a periodic task in the shape of the packet-log pruner, applying the age bound, the count bound, or both, and counting deletions (design D15); verify tests for each bound, both together, and a room with neither where nothing is deleted
- [x] 10.2 Count and report messages removed whose ordering values were above at least one member's sync position, so retention outrunning sync is visible; verify a test with a member left behind asserts the count is reported
- [x] 10.3 Report retention policy and deletions in startup and status output, stating "unlimited" plainly when no policy is set; verify a render test for both states

## 11. Runtime wiring and reporting (`room-server`, `runtime-cli`)

- [x] 11.1 Load rooms and their members at startup for enabled room-server entities, start their push loops and retention pruners, and stop them in the existing shutdown order; verify a test asserts a room is served, its tasks start, and shutdown drains without abandoning a delivery
- [x] 11.2 Serve no room and say so when no database is configured, and report a room bound to a disabled entity as not served with the reason (design D5); verify render tests for both statements
- [x] 11.3 Add the room lines to startup output — identity, members, messages, guest access, retention — and the room counters to periodic status output; verify render tests asserting each field appears and no password or hash does
- [x] 11.4 Emit typed room events for the renderer to format — login admitted, login refused with reason, post stored, post refused, delivery sent, delivery acknowledged, member backed off, retention pruned — with `net/` importing nothing from `monitor/`; verify a render test per event and the existing import-boundary test still passing

## 12. Command surface (`runtime-cli`)

- [x] 12.1 Add `sighop room create` binding a room to a stored room-server identity, refusing an identity that already has a room, and stating the resulting configuration; verify tests for the success path and the duplicate refusal
- [x] 12.2 Add `sighop room list` and `sighop room show`, reporting identity, members, messages, guest access and retention and never a password or a hash; verify a test asserts the hash string appears in no output
- [x] 12.3 Add `sighop room passwd` for the admin and guest passwords, reading from a prompt or standard input only, refusing a password given as an argument (design D16), and printing that rotation evicts nobody; verify tests for stdin input, for the argument refusal, and for the printed statement
- [x] 12.4 Add `sighop room members` and `sighop room revoke`, the latter stating that the member must log in again and that its sync position is discarded; verify tests for both outputs
- [x] 12.5 Add `sighop room retention` setting or clearing the age and count bounds; verify a test asserts clearing returns the room to unlimited and that the output says so
- [x] 12.6 Add `sighop room post` storing a post authored by the room's own identity, and `sighop room history` rendering stored messages with text that is not valid UTF-8 marked as a rendering; verify tests for both
- [x] 12.7 Add entity creation as a room server — the room-server entity type and node type in the advert configuration — to the existing key and entity commands (`entity-store`); verify a test asserts a created room-server identity carries both, and that an ordinary identity is unchanged

## 13. Offline verification

- [x] 13.1 Add a loopback exercise driving one sighop entity as a client against a sighop room server through the bus: login, post, push, acknowledge, cursor advance; verify it runs with no database by using the storage fixtures, and with one under the `database` marker
- [x] 13.2 Verify a room server restarted mid-exercise resumes delivery from each member's stored position, by a test that stops and rebuilds the server between a post and its delivery
- [x] 13.3 Verify the corpus still decodes identically and the corpus golden output is unchanged by anything in this milestone, by the existing corpus tests
- [x] 13.4 Verify the 42 `ANON_REQ` frames already in the corpus still parse as anonymous requests and that none of them is mistaken for a login to one of our entities, by a corpus test asserting zero login attempts during a full replay
- [x] 13.5 Verify the reception path is unaffected by a room server: replay the corpus with and without one wired and assert identical delivered, duplicate, contact and path counts
- [x] 13.6 Verify no test added in this milestone requires Postgres to pass, by running the full suite with no database URL set

## 14. The live exercise

- [x] 14.1 Write the runbook: create the room-server identity, create the room with a guest password rather than opening it, confirm the advert is seen by the client receive-only, then enable transmit for the login and sync run; verify by review before anything is transmitted
- [x] 14.2 Run the exercise: a stock MeshCore client logs in to the sighop room server over the air, and the login is reported as admitted with its permission level; verify by the client showing the room joined and by the run's own event stream
- [x] 14.3 Post from the client and verify the post is stored and acknowledged, by reading it back with `sighop room history` and by the client showing it delivered
- [x] 14.4 Post from the server with `sighop room post`, verify the client receives it, and verify the member's sync position advanced only on the acknowledgement
- [x] 14.5 Restart sighop, verify the member is still recognised with no re-login, and verify the client receives the history posted while it was away — the milestone's exit criterion
- [x] 14.6 Record the airtime the exercise cost as a fraction of the hourly ceiling, and the observed message volume against the retention question (§13 unknown #1); verify the numbers are read from the run's own counters rather than estimated
- [x] 14.7 Observe whether the peer floods its replies after learning us from an advert (§13 unknown #4) and record the observation or record explicitly that no opportunity arose; verify against the captured frames rather than inference
- [x] 14.8 Append the session whole to the corpus with its provenance header, update the corpus counts and `tests/protocol/CORPUS.md`, and verify the corpus tests pass against the new totals

## 15. Documentation

- [x] 15.1 Update DESIGN.md §6: move `room`, `room_member` and `message` from "not built yet" to built, and record the three shapes that turned out to differ from the sketch — the ACL as routing and cursor table, the room-scoped ordering value, and two independent retention bounds; verify by review
- [x] 15.2 Update DESIGN.md §7 with what the firmware does where it differs from the section's summary — the empty-password re-login, the read-only fallback, the push loop's constants and the 151-byte post limit; verify by review
- [x] 15.3 Write DESIGN.md §12's milestone 6 entry in the established form: what was done, and the findings the offline work could not have produced; verify by review
- [x] 15.4 Answer or explicitly leave open §13 unknowns #1 and #4 with the measurements from task 14.6 and 14.7, recording a non-observation as a non-observation rather than as confirmation; verify by review
