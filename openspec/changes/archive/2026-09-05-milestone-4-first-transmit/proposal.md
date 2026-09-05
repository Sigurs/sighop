## Why

Milestone 3 built the entire outgoing path with the gate held shut: the scheduler, the
duty-cycle budget, the advert policy and the modem's `Data`/`TxDone`/`TxBusy` correlation all
ran a full night against live load without keying the transmitter once. Milestone 4 is the
moment that path is allowed to reach the antenna, and DESIGN.md §12 sets its exit criterion
deliberately narrow: **the first successful decrypt of a real MeshCore DM**.

That criterion is the point of the milestone. Everything in `protocol/crypto.py` is currently
verified against ourselves — round-trips, and known-answer vectors derived from reading the
firmware source. The 997-frame corpus cannot close the gap, because every encrypted payload in
it is addressed to a third party (§12's "one hard limit"). Only a peer running stock MeshCore
firmware, encrypting to a key we hold, proves the ECDH, the AES-128-ECB key slice and the
2-byte HMAC truncation are right. Until that packet decrypts, the cipher layer is an
untested assumption sitting under every entity type milestones 5–8 will build.

The second reason to do this as its own milestone is that first transmission is a
**one-way door**: it is the first sighop bug that can land on somebody else's network. It
therefore gets a dedicated, watched, minimal exercise against a peer board on the desk, not a
side effect of building room servers.

## What Changes

- **The gate opens, once, deliberately.** `--enable-transmit` stops being a flag nothing
  reaches and becomes the flag that keys the radio. Nothing else about the gate changes: it
  stays off by default, and every safety property milestone 3 built stays exactly as it is.
- **New persistent local identity.** An entity's keypair is written to a keyfile and reloaded
  on the next run, so the peer's contact list stays valid across restarts. Milestone 3's advert
  stubs generate a fresh key per process, which is correct for a receive-only dry run and
  useless the moment another node stores our public key. The database is milestone 5; this is
  the deliberate bridge to it, and it is the smallest thing that makes the exercise repeatable.
- **New contact table.** Verified adverts become contacts — public key, node hash, name, flags,
  first/last heard — so a peer can be addressed by name instead of by pasted hex, and so an
  inbound ciphertext has a set of candidate public keys to try. A peer may also be added by hex
  key for the case where its advert has not been heard yet.
- **New direct messaging, both directions.**
  - *Outbound*: compose the MeshCore text-message plaintext (`timestamp ‖ flags ‖ text`, no NUL
    terminator on the wire), encrypt-then-MAC to the peer's shared secret, address the envelope,
    choose zero-hop `DIRECT` over `FLOOD` where a path is known, submit at priority class 2, then
    wait for the matching ACK and retry with an incremented attempt within a bounded budget.
  - *Inbound*: a bus subscriber filters by destination hash across every local entity, trials the
    2-byte MAC against each (local entity × contact) shared secret, decrypts, parses, prints the
    message, and answers with an ACK at priority class 0.
- **ACK semantics taken from the firmware, not invented.** The expected ACK is the first 4 bytes
  of `sha256(timestamp ‖ flags ‖ text ‖ our public key)` (`BaseChatMesh.cpp:431`); a received ACK
  may be **4 or 6 bytes** and only its first 4 are compared (`BaseChatMesh.cpp:245`, `:740`); the
  retry deadline uses the reference peer's own formula — `500 ms + 16 × airtime` for flood,
  `500 ms + (6 × airtime + 250 ms) × (hops + 1)` for direct (`MyMesh.cpp:851-858`). Matching the
  peer's arithmetic is what keeps our retry cadence from fighting its expectations.
- **A first-transmit runbook**, executed and written up: peer board flashed with stock
  `companion_radio` firmware, sighop's entity advertising **zero-hop** so the first packet on
  the air is not amplified across every repeater in the mesh, the peer's client showing the
  contact, then a DM in each direction.
- **The corpus gains what it structurally could not hold.** The session is appended whole, per
  §12's rule, and brings two firsts: frames **sighop itself transmitted**, and a ciphertext that
  **decrypts**. That recorded DM becomes a fixed known-answer vector against a foreign
  implementation, which is the milestone-1 limitation finally closed rather than restated.

## Capabilities

### New Capabilities
- `local-identity`: an entity keypair that survives the process — keyfile format and
  permissions, loading an existing key rather than silently generating a new one, node-hash
  collision avoidance across local entities, and the refusal to overwrite a key that exists.
- `contacts`: the contact table built from verified adverts and manual additions — keying by
  public key, indexing by node hash where collisions are expected and lookups therefore return
  every candidate, and the rule that an unverified advert never becomes a contact.
- `direct-messaging`: DM composition, addressing and routing; the ACK checksum, its 4-or-6-byte
  comparison and the firmware's timeout formula; bounded retries with the attempt counter; and
  the inbound path — destination-hash fan-out, MAC trial across candidate secrets, decryption,
  parsing, and the ACK reply — including the rule that a 2-byte MAC match is never treated as
  authentication.

### Modified Capabilities
- `runtime-cli`: `sighop run` gains the identity keyfile, peer selection and message-sending
  surface, renders decoded DMs and ACK outcomes, and reports at startup which entity identity
  it is running as. The transmit flag's meaning changes from "scheduled and suppressed" to
  "keyed", so its startup banner must say so.
- `advert-policy`: adverts may be driven by a **persistent** entity identity rather than only by
  an ephemeral stub, and a zero-hop advert must be requestable as a deliberate one-shot — the
  cheapest way to introduce ourselves to a peer on the same desk without touching the wider mesh.
- `mesh-crypto`: decryption acquires a known-answer vector produced by **another
  implementation**. The capability's stated limitation — that ciphertext handling is verifiable
  only by round-trip and by vectors read out of the firmware source — is the thing this
  milestone removes.
- `protocol-corpus`: the corpus admits frames sighop transmitted, alongside received ones, and
  carries its first decryptable payload; counts and provenance are updated accordingly.

## Impact

- **New code:** `src/sighop/net/contacts.py`, `src/sighop/net/dm.py`, and an identity keyfile
  store (`src/sighop/protocol/keyfile.py` — pure bytes-and-JSON, so it stays inside `protocol/`'s
  import boundary, or `src/sighop/keystore.py` if it must touch the filesystem; the design
  artifact settles this).
- **Modified code:** `src/sighop/runtime.py` (wire the DM subscriber and the entity identity),
  `src/sighop/net/adverts.py` (accept a supplied identity; one-shot zero-hop advert),
  `src/sighop/cli.py` (`run` gains `--entity-key`, `--peer`, `--send`), and
  `src/sighop/monitor/render.py` (DM and ACK lines).
- **`net/rx.py` stays stateless.** DM decryption needs keys and a contact table, so it lives
  behind the bus as a subscriber, never inside the decode stage. Milestone 2's property — that
  replaying a capture reproduces every reception exactly — must survive this milestone.
- **`protocol/` gains no new cryptography.** Everything the DM path needs already exists there
  (`encrypt_then_mac`, `mac_then_decrypt`, `ack_checksum_for`, `build_text_message_body`); this
  milestone is the first caller, not a new primitive.
- **No new runtime dependencies.** Driving the peer board is a development-time activity: the
  vendored `related-repos/meshcore_py` or the MeshCore companion app, never a sighop dependency.
- **Hardware reality:** the peer is the **Heltec V3**, which §12 established is faulty — it
  power-cycles on a 75.07 s timer. The exercise is therefore designed to complete inside a
  75-second window, and peer resets are expected noise rather than evidence of a sighop bug.
  Contacts persist in the peer's flash across those resets; in-flight state does not.
- **DESIGN.md** is updated in this change, per its own rule: §12's milestone 4 entry records
  what was found, §11 gains the new modules, and §5's note that decryption is unproven against
  a foreign implementation is retired.
- **Regulatory surface.** This is the first milestone that actually transmits. The duty-cycle
  budget, the advert floor and the priority classes are unchanged and unrelaxed; the exercise
  is a handful of packets, and the status line reports the real consumption for the first time.
- **Out of scope, deliberately:** any database write (milestone 5); room-server login,
  `ANON_REQ` handling and history sync (milestone 6); group/channel messaging; bots (milestone
  7); the WebUI (milestone 8); path scoring beyond milestone 3's most-recently-confirmed-wins;
  and any transmission aimed at the public mesh rather than at the peer board.
