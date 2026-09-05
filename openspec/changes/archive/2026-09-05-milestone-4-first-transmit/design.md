## Context

Milestone 3 finished the outgoing path and never used it. The scheduler, the sliding-window
airtime budget, the advert policy and the modem's `Data`/`TxDone`/`TxBusy` correlation all ran
for a 2 h 54 min live session with the gate shut, charging and suppressing every packet they
would have sent. What remains for milestone 4 is small in code and large in consequence: open
the gate, and close the one hole the corpus cannot close.

Four constraints shape everything below.

1. **The exit criterion is a decrypt, not a transmit.** DESIGN.md §12 asks for "the first
   successful decrypt of a real MeshCore DM". Transmitting is the means; the ECDH → AES-128-ECB
   → truncated-HMAC stack being confirmed against a foreign implementation is the end. Every
   `protocol/crypto.py` test today is either a round-trip against ourselves or a vector read out
   of the firmware source — the same reading that was wrong about the ACK construction until
   `BaseChatMesh.cpp` was checked (§5).
2. **The corpus structurally cannot help.** All 997 recorded frames encrypt to third parties.
   No amount of additional listening produces a ciphertext we hold the key to.
3. **First transmission is a one-way door.** It is the first sighop defect class that lands on
   other people's networks. The exercise is therefore aimed at a board on the same desk, at
   zero hops, with the wider mesh given nothing to repeat.
4. **The peer board is faulty.** §12 established that the Heltec V3 power-cycles on a 75.07 s
   timer, reproducibly, invariant to software. It is nonetheless the only second board there is,
   and stock `companion_radio` firmware persists contacts to flash, so the fault costs us
   in-flight state and nothing else — provided each exchange fits inside a 75-second window.

Milestone 3's design closed with a note that this milestone's first transmission should be "a DM
to our own peer board, not an advert, precisely so the mesh is not told about identities that
vanish at process exit". D1 and D2 below are what make that possible rather than aspirational.

## Goals / Non-Goals

**Goals:**

- A local entity identity that survives the process, so a peer's stored contact stays valid.
- A contact table built from verified adverts, with the §3 node-hash collision rule honoured
  rather than assumed away.
- DMs out and in: composition, addressing, routing, ACK matching, bounded retry; and
  destination-hash fan-out, MAC trial, decryption, parsing, ACK reply.
- A first transmission that is deliberate, watched, zero-hop, and reaches exactly one board.
- A **regression test**, not just a log line, proving decryption against another implementation —
  the recorded peer DM as a committed known-answer vector.
- Session captured and appended to the corpus, giving it its first transmitted frames and its
  first decryptable payload.

**Non-Goals:**

- Persistence of contacts, paths or messages (milestone 5 owns the database; only the entity
  keypair is written to disk here, and D1 says why that exception exists).
- Room-server login, `ANON_REQ`, history sync (milestone 6); channel/group messaging; bots
  (milestone 7); WebUI (milestone 8).
- Responding to `PATH` / returned-path requests, `TRACE`, or telemetry requests. Unhandled
  payload types keep milestone 2's behaviour: preserved uninterpreted, never a crash.
- Path scoring beyond most-recently-confirmed-wins (§13 unknown #3 still needs two observed
  routes to one peer).
- Any change to the scheduler, the budget, the advert floor or the gate's default. The safety
  properties milestone 3 built are used here, not revised.
- Talking to the public mesh at all. Every packet this milestone emits is addressed to the peer
  board and routed so no repeater has anything to forward.

## Decisions

### D1 — The entity keypair goes in a keyfile, and that is a deliberate exception to "no persistence"

Milestone 3's advert stubs generate a keypair per process. That is right for a receive-only dry
run and wrong the instant another node stores our public key: a peer whose contact list is full
of one-run identities makes the exercise unrepeatable, and re-injecting a new key before every
run turns a two-minute test into a ritual.

So: one JSON keyfile per entity — the 32-byte seed, the derived public key, the name, the node
type — written `0600`, loaded if present, generated only when absent, and **never silently
overwritten**. Generation rejects a keypair whose node hash collides with another loaded local
entity (§3, rule 3), which is the same check `net/adverts.py` already performs for stubs.

*Alternatives rejected:* bringing milestone 5's Postgres forward (a database, a migration tool
and `SIGHOP_SECRET_KEY` encryption-at-rest, all to store one key); keeping the ephemeral stub and
re-injecting into the peer each run (turns the peer's contact list into a graveyard and makes
"is this the same identity as last time?" unanswerable during debugging).

The keyfile is explicitly a bridge. Milestone 5 imports it and encrypts it at rest per §6; the
file format therefore stays boring enough to migrate in one function.

### D2 — The peer learns our key over USB. sighop's first RF transmission is the DM itself

Stock MeshCore only decrypts a DM from a **known contact** — `BaseChatMesh` trials the MAC
against its contact list and drops what matches nothing. The peer must therefore hold our public
key before we can be answered, and there are two ways to put it there: advert on the air, or
inject it over the peer's own USB link.

We inject. `meshcore_py`'s `update_contact` (companion command `0x09`) writes a contact — public
key, type, flags, out-path, name — straight into the peer's flash over serial. No sighop
transmission is involved, so the first packet sighop ever keys is the DM, watched, at a moment
we choose. That is exactly the property milestone 3's design asked for.

*Alternative rejected:* a zero-hop advert first. It works, and it is a fine thing to prove
**afterwards** as its own step, but it makes the historic first transmission an incidental
side-effect of an advert timer rather than a deliberate act. Adverts also cost the mesh (§4.3),
and there is no reason for a first-ever transmission to be the one that is rebroadcast.

The injected contact's `out_path` is set to **empty with length 0** — zero-hop direct — so the
peer's ACK and its own DM come back direct rather than flood-scoped. A contact with an unknown
path makes the peer flood its replies, which is precisely the traffic this exercise should not
generate.

### D3 — We learn the peer's key by hearing its zero-hop advert, not by pasting hex

The peer is asked (over its USB link, `send_advert(flood=False)`) to emit one zero-hop advert.
sighop hears it, verifies the Ed25519 signature, parses the appdata, and creates a contact. Its
node hash and public key arrive by the route the design intends, and the advert-verification path
gets exercised against traffic aimed at us rather than overheard.

Manual addition by hex public key stays supported — it is needed the moment an advert is missed
and it costs one function — but it is the fallback, not the plan.

### D4 — Zero-hop DIRECT in both directions, and flood requires an explicit flag

Outbound routing: if the path store holds a route to the peer, send `DIRECT` with it (a zero-hop
route is an empty path, which milestone 3 established is how MeshCore's own zero-hop adverts
arrive). If no route is known, **refuse to send** unless `--allow-flood` is given.

That inverts the firmware's default, which floods when the path is unknown. The inversion is the
point: on a desk-to-desk exercise, a flood is the only way this milestone can put load on other
people's repeaters, and it should not be reachable by mistyping a peer name. The peer's advert of
D3 is itself a zero-hop direct reception, so milestone 3's path learning has a route before the
first DM is composed — the flag should never be needed and its being needed is a signal.

### D5 — Contacts are keyed by public key, indexed by node hash, and a lookup returns every candidate

The index maps a 1-byte node hash to a *set* of contacts, because §3 says collisions are routine
and this design makes them likelier. Nothing in the DM path may assume a node hash identifies a
peer; it selects a candidate set that MAC trial then narrows.

Only a **verified** advert becomes a contact (§5: an unsigned or badly-signed advert is a
discard, not a warning). A contact records the public key, node hash, name, appdata flags and
first/last-heard timestamps, held in memory; milestone 5 gives it a table.

### D6 — Inbound DM handling is a bus subscriber, never part of `net/rx.py`

Decryption needs the local entity keys and the contact table; `net/rx.py` is a pure function of
one frame and stays that way. The DM handler subscribes to the bus like any other consumer,
downstream of dedup and path learning.

This preserves the property milestone 2 built and milestone 3 relied on: replaying a capture
reproduces every reception exactly, duplicates included. It is also what will let the recorded
first-decrypt session be replayed as a test.

### D7 — MAC trial: (local entities matching the destination hash) × (contacts), with cached secrets

For a `TXT_MSG` envelope, the candidate set is every local entity whose node hash equals the
envelope's destination hash, crossed with every contact whose node hash equals the source hash —
falling back to all contacts when the source hash matches none, since a contact we have not heard
from is still a possible sender. Each candidate pair yields a shared secret from the existing
`SharedSecretCache`, and `mac_then_decrypt` decides.

A MAC match is **not** authentication and the code must never phrase it as one: 2 bytes is ~1 in
2^16 per candidate key (§3, rule 2). It selects a decryption key; the fact that the plaintext then
parses as a text message is what makes it believable, and even that is evidence rather than proof.
Rendered output marks a decrypted DM as originating from a *claimed* contact, in the same way
milestone 3 marks unverified content as visually distinct.

### D8 — ACK construction and comparison are taken verbatim from the firmware

Nothing here is derived from documentation. From `BaseChatMesh.cpp`:

- **Expected ACK** (sender side, `:431`): first 4 bytes of `sha256(timestamp ‖ flags ‖ text ‖
  our public key)`, over exactly `5 + text_len` plaintext bytes — the NUL terminator that
  `composeMsgPacket` copies into its buffer is **not** transmitted and **not** hashed.
- **Flags byte** (`:427`): `(attempt & 3) | (txt_type << 2)`.
- **ACK we emit** (receiver side, `:243`): the same hash over the received plaintext with the
  *sender's* public key. `protocol/crypto.py::ack_checksum_for` already implements both sides.
- **Comparison**: an ACK payload may be **4 or 6 bytes** — the firmware appends an extended
  attempt byte and a random byte in one path (`:245`) — and the sender compares only the first 4
  (`:740`). We do the same, and we emit the 4-byte form.

Attempts are capped at 3 so the `attempt > 3` variant, which hides the attempt number in a tail
after a NUL (`:434-437`), is never produced. That variant is understood and deliberately not
implemented; producing it would be untested code on the air.

### D9 — Retry cadence copies the reference peer's own timeout formula

From `MyMesh.cpp:851-858`:

- flood: `500 ms + 16 × airtime_ms`
- direct: `500 ms + (6 × airtime_ms + 250 ms) × (hops + 1)`

`airtime_ms` comes from milestone 3's own `net/airtime.py` calculation, which D2 of that
milestone cross-checked against the board to sub-millisecond agreement. Using the peer's
arithmetic means our resend does not arrive while it still considers the first attempt live, and
does not wait so long that a human calls it a failure.

A retry re-uses the **same timestamp** and increments the attempt. That changes the flags byte,
hence the ciphertext, hence the packet hash — which is the reason the field exists, since an
identical retransmission is what every repeater in the path would dedup. It also changes the
expected ACK, so an outstanding send tracks the expected checksum of *every* attempt it has made
and accepts a match against any of them.

Four attempts (0–3), then the send resolves as unacknowledged and says so. An unacknowledged DM
is a reported outcome, never a silent one.

### D10 — Priority classes and deadlines follow §4.3 without amendment

Outbound DM: class 2 ("originated messages"). ACK we emit: class 0 ("the sender is retrying
until it hears one; delay multiplies traffic"). A DM's scheduler deadline is its ACK timeout from
D9 — a DM that cannot reach the air before its own retry window closes is better dropped and
logged than sent late into a retry.

### D11 — The exercise runs on a burned test identity whose keyfile is committed

The exit criterion needs a **regression test**, not only a log line from one evening. That means
the recorded ciphertext and the private key that opens it must both live in the repo, or the
proof evaporates the moment the session scrolls off.

So the exercise uses a dedicated identity generated for it, whose keyfile is committed as a test
fixture and marked, in the file and in the test, as **burned**: it has been published, it must
never be used on air again, and any real deployment generates its own. What it buys is permanent:
the appended capture contains a DM from a foreign implementation that the test suite can decrypt
on every commit, which is exactly the milestone-1 limitation this milestone exists to remove.

*Alternative rejected:* keeping the key out of the repo. The corpus would then hold a ciphertext
nothing can open, which is the situation we started in.

This does not weaken §6's rule about keys at rest. That rule governs identities with something to
protect; this one is a test vector with a keypair attached, and saying so explicitly in the
fixture is what keeps the two from being confused.

### D12 — CLI surface: `sighop keys` for the identity, `sighop run` for the exchange

`sighop keys new --name <name> --out <path>` creates a keyfile and prints the public key (which is
what D2 injects into the peer); `sighop keys show <path>` prints name, public key and node hash
without printing the seed.

`sighop run` gains `--entity <keyfile>` (repeatable — the node-hash collision check is why),
`--peer <name|hex-prefix>`, `--send <text>` for a one-shot after the peer is known, and
`--allow-flood` from D4. DMs, ACKs and unacknowledged sends render as lines through
`monitor/render.py` like everything else, and the startup banner states which identity is running
and — now that it means keying a transmitter — what `--enable-transmit` will do.

### D13 — The exercise is a written runbook executed in order, with the capture kept

1. Peer (V3, `/dev/ttyUSB0`) flashed with stock `companion_radio`; sighop's board (V4,
   `/dev/ttyACM0`) keeps the `kiss_modem` firmware.
2. `sighop keys new` → public key.
3. Inject that key into the peer over USB with `out_path` empty, length 0 (D2). **No RF yet.**
4. `sighop run --enable-transmit --capture ...` — receive-only in practice, nothing queued.
5. Ask the peer for one zero-hop advert; sighop verifies it and creates the contact (D3). Still
   no sighop transmission.
6. `--send` the DM. **This is the first transmission.** Expect `TxDone`, then the peer's ACK.
7. Peer sends a DM to sighop; sighop decrypts it. **Exit criterion.**
8. sighop ACKs it; confirm the peer marks the message delivered.
9. Zero-hop advert from sighop, as a separate deliberate step, confirming the advert path on air.
10. Append the capture whole to the corpus per §12; extract the decrypt vector for D11.

Steps 6–8 must fit inside 75 s (the V3's power-cycle period). A reset mid-exchange is expected
noise: repeat, do not debug.

## Risks / Trade-offs

- **The decrypt fails and it is not obvious why** → the MAC trial and the decrypt are separable,
  and both are logged: a MAC match with an unparseable plaintext means the cipher key slice is
  wrong, a MAC miss across all candidates means the shared secret is. Our own outbound ciphertext
  can be decrypted with the same secret as an immediate self-check. `mesh-crypto`'s existing KATs
  say the two implementations agree on paper; this is where paper meets air.
- **The peer resets mid-exchange** (V3 fault, 75.07 s) → the runbook is sized to the window and
  says to repeat rather than investigate. Contacts survive in the peer's flash; nothing else does.
- **A mistyped peer name floods the public mesh** → D4 refuses to send without a known route, and
  flooding requires an explicit flag. The peer's advert supplies the route before the first DM.
- **The committed burned key is mistaken for a usable identity** (D11) → marked in the file, in
  the fixture's name and in the test that reads it; `sighop keys new` is one command, so there is
  no incentive to reuse it.
- **A false MAC match decrypts garbage and we render it as a message** (D7) → the plaintext must
  parse as a text message body, and rendering marks the sender as claimed rather than verified.
  The residual rate is ~2^-16 per candidate pair against a handful of contacts.
- **ECB determinism makes an identical resend invisible** → the timestamp advances between
  messages and the attempt counter advances within one, so no two transmissions carry identical
  ciphertext. This is the same phenomenon as milestone 3's pair of byte-identical DIRECT frames
  52 minutes apart, seen from the sending side.
- **Milestone 3's `TxDone` timeout factor has never met a real transmission** → first observable
  here; the run logs the interval between hand-off and `TxDone` so the assumption becomes a
  measurement.
- **Opening the gate is irreversible in the sense that matters** — a packet on the air cannot be
  recalled. Mitigated by scope (one peer, zero hops, a handful of packets), by the unchanged
  duty-cycle budget, and by the runbook's insistence that steps 1–5 involve no transmission at
  all, so the operator reaches step 6 having already seen the system working.

## Open Questions

1. **Do `SEND_TIMEOUT_BASE_MILLIS`/`FLOOD_SEND_TIMEOUT_FACTOR` (D9) match the peer's build?**
   They are `examples/companion_radio` defines, not library constants, so a different firmware
   build could differ. The measured ACK latency in step 7 settles it.
2. **Does the peer accept a zero-hop `DIRECT` DM from a contact whose `out_path` we set to empty**
   (D2/D4)? Expected from `sendDirect`'s handling of a zero-length path; confirmed or refuted at
   step 6.
3. **Where does the keyfile live by default** — `./sighop-entity.json`, an XDG path, or explicit
   only? Leaning explicit-only for this milestone, since milestone 5 replaces the storage anyway.
4. **Path scoring** (§13 unknown #3) stays open. This milestone produces exactly one route to one
   peer, which is not the observation it needs.
5. **Whether the peer's ACK arrives direct or flood-scoped** depends on question 2's answer and is
   worth recording either way — it is the first measurement of how a stock node answers us.
