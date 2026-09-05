## 1. Persistent entity identity (`local-identity`)

- [x] 1.1 Add a keyfile module holding the JSON format — version, name, node type, 32-byte seed and public key, both hex — with load and create functions and a documented shape milestone 5 can import in one function (design D1)
- [x] 1.2 Create with `0600` permissions; refuse to write over an existing path with an error naming it; warn when loading a file whose mode is broader than owner-only
- [x] 1.3 Verify on load that the public key derived from the stored seed equals the stored public key, failing with the file name rather than preferring either value
- [x] 1.4 Add an entity registry that loads several keyfiles, rejects two local entities sharing a node hash (naming both files), and feeds generation the taken hashes so `generate_identity` avoids them (§3 rule 3)
- [x] 1.5 Decide where this lives with respect to `protocol/`'s import boundary — filesystem access does not belong under `protocol/`, so put the file I/O outside it and keep the seed→identity step in `protocol/identity.py`; `tests/protocol/test_import_boundary.py` must still pass
- [x] 1.6 Tests: create-then-load round-trip; refusal to overwrite; permission bits on creation; broad-permission warning; seed/public-key mismatch rejected; two colliding keyfiles rejected by name; generation avoids a loaded entity's node hash

## 2. Contacts (`contacts`)

- [x] 2.1 Create `src/sighop/net/contacts.py`: a store keyed by 32-byte public key holding node hash, name, appdata flags, node type, first and last heard
- [x] 2.2 Accept only verified adverts — take a `VerifiedAdvert`, not an `Advert`, so an unverified one cannot be recorded by mistake (design D5, §5)
- [x] 2.3 Index by node hash returning a **set**; a lookup for an unknown hash returns an empty set, never `None` and never a single contact (§3 rule 1)
- [x] 2.4 Support manual addition from a hex public key, marked as not advert-verified, and return the existing contact unchanged when the key is already known
- [x] 2.5 Resolve a peer reference by exact name or hex key prefix, failing with every candidate listed when ambiguous
- [x] 2.6 Report a name change on an existing key rather than swapping it silently — the key is the identity, the name is advert content
- [x] 2.7 Subscribe the store to the bus so verified adverts become contacts as they arrive
- [x] 2.8 Tests: verified advert creates a contact; unverified is refused at the type level; two contacts sharing a node hash both returned; name change reported; ambiguous reference lists candidates; manual add marked unverified; replaying a corpus file populates contacts with the expected key count

## 3. Outbound direct messages (`direct-messaging`)

- [x] 3.1 Create `src/sighop/net/dm.py` with plaintext composition `timestamp(4, LE) ‖ (attempt & 3) | (txt_type << 2) ‖ text`, transmitting no NUL terminator (`BaseChatMesh.cpp:425-433`, design D8)
- [x] 3.2 Reject text above the firmware's `MAX_TEXT_LEN` (160) before anything is queued, naming the limit
- [x] 3.3 Encrypt-then-MAC through the existing `SharedSecretCache`, and build the `TXT_MSG` `DirectEnvelope` with the recipient's node hash as destination and the sending entity's as source
- [x] 3.4 Choose routing: `DIRECT` with the learned path, an empty path for a zero-hop route, and a refusal when no route is known unless flooding was explicitly permitted (design D4)
- [x] 3.5 Compute the expected acknowledgement as the first 4 bytes of `sha256(transmitted plaintext ‖ our public key)` via the existing `ack_checksum_for`, and hold it per attempt
- [x] 3.6 Compute the acknowledgement timeout from `net/airtime.py` using the peer's formula — `500 + 16 × t` flooded, `500 + (6 × t + 250) × (hops + 1)` direct (`MyMesh.cpp:851-858`, design D9)
- [x] 3.7 Submit at priority class 2 with the deadline set to that timeout, so a message that cannot reach the air within its own retry window is dropped and logged rather than sent late (design D10)
- [x] 3.8 Retry on timeout with the same timestamp and an incremented attempt, capped at attempt 3 so the extended-attempt tail encoding is never produced; accept an acknowledgement matching any attempt's expectation
- [x] 3.9 Resolve a send as acknowledged, unacknowledged-after-attempts, or dropped — and report every one of those; never abandon a send silently
- [x] 3.10 Tests (with the injected clock, no radio): composition byte layout against a hand-built vector; NUL not transmitted; oversized text refused; zero-hop route yields an empty-path `DIRECT`; unknown route refuses without the flood flag and floods with it; retry cadence matches the formula; a late acknowledgement for attempt 0 resolves a send now on attempt 2; four failures resolve as unacknowledged; class 2 submission with the right deadline

## 4. Inbound direct messages (`direct-messaging`)

- [x] 4.1 Add a bus subscriber that takes `TXT_MSG` receptions only, downstream of dedup and path learning, leaving `net/rx.py` untouched and stateless (design D6)
- [x] 4.2 Build the candidate set: local entities whose node hash equals the destination hash, crossed with contacts whose node hash equals the source hash, falling back to all contacts when the source hash matches none (design D7)
- [x] 4.3 Verify the MAC before decrypting each candidate, using the shared-secret cache; on no match, report an undecryptable direct message with the candidate count rather than dropping it
- [x] 4.4 Parse the decrypted body with `parse_text_message_body`; a MAC match whose plaintext does not parse is reported with the parse reason and is **not** acknowledged
- [x] 4.5 Emit an acknowledgement — first 4 bytes of `sha256(received plaintext ‖ sender public key)` — at priority class 0, routed by the same rules as an outbound message
- [x] 4.6 Accept inbound acknowledgements of 4 **or** 6 bytes, comparing only the first 4 against outstanding expectations (`BaseChatMesh.cpp:245`, `:740`); report one matching nothing
- [x] 4.7 Render decrypted messages with the sender marked as **claimed**, in the same visual convention `runtime-cli` already uses for unverified content, and never as verified
- [x] 4.8 Tests: loopback between two local entities through a fake sender — compose, decrypt, acknowledge, match; destination-hash collision where the wrong entity's MAC fails and the right one succeeds; forced MAC match with unparseable plaintext is reported and not acknowledged; 6-byte acknowledgement matches on its first 4 bytes; undecryptable message reports its candidate count
- [x] 4.9 Note in the test module that a loopback proves self-consistency only: both halves are sighop, so this cannot confirm the wire format — section 8 is what does

## 5. Runtime and command line (`runtime-cli`, `advert-policy`)

- [x] 5.1 Add `sighop keys new --name --out` (creates a keyfile, prints the public key in hex) and `sighop keys show <path>` (name, node type, public key, node hash — never the seed) (design D12)
- [x] 5.2 Add `--entity <keyfile>` (repeatable) to `sighop run`, loading each through the registry and reporting name, public key and node hash at startup
- [x] 5.3 Let a loaded identity drive adverts on the same terms as a stub, marking persistent and ephemeral entities distinctly in the startup listing and applying the inter-entity gap between them
- [x] 5.4 Add a one-shot zero-hop advert request for a named entity, submitted at class 3 through the scheduler and charged normally, leaving the recurring zero-hop interval disabled
- [x] 5.5 Add `--peer <name|hex-prefix>`, `--send <text>` and `--allow-flood`; an unresolvable peer says so and the run continues receiving rather than exiting
- [x] 5.6 Wire the contact store, the DM subscriber and the send path into `Runtime`, keeping the module free of policy as it is today
- [x] 5.7 Rewrite the transmit-enable startup banner: with the gate open it states that packets will be transmitted on air, names each originating identity, and states the ceiling
- [x] 5.8 Add render functions for message sent, acknowledgement matched, send unacknowledged, message received, and message undecryptable — pure formatting, tested by string comparison
- [x] 5.9 Extend the capture writer with a record kind for frames sighop transmitted, so a run with transmission enabled produces a corpus-grade file containing both directions
- [x] 5.10 Tests: `keys new`/`keys show` output including the absence of the seed; startup banner text with the gate open and closed; colliding keyfiles fail startup; unresolvable peer keeps the run alive; end-to-end replay run with two entities exchanging a message through a fake sender

## 6. Offline verification before any transmission

- [x] 6.1 Run the full suite plus `ruff` and `mypy` clean, with the gate's existing test — zero `Data` frames on the transport while disabled — still passing
- [x] 6.2 Replay the existing 997-frame corpus through the new pipeline and confirm byte-identical decode output to the current golden file: the milestone must add nothing to the decode stage
- [x] 6.3 Dry-run `sighop run --replay` with an entity keyfile, a peer and a message, gate closed, and confirm the message is composed, charged and suppressed exactly as milestone 3's stubs are
- [x] 6.4 Confirm from the logs that the composed packet's route, length and computed time-on-air are what the runbook expects before it is real

## 7. Peer board preparation (no transmission)

- [x] 7.1 Flash the Heltec V3 (`/dev/ttyUSB0`, CP2102) with stock `companion_radio` firmware; record the firmware version and radio configuration for the capture provenance
- [x] 7.2 Confirm the peer is driveable over USB with the vendored `related-repos/meshcore_py` — development-time only, never a sighop dependency
- [x] 7.3 `sighop keys new` for the exercise identity, and inject its public key into the peer's contact list with `update_contact`, `out_path` empty and length 0 so the peer answers zero-hop direct rather than flood (design D2)
- [x] 7.4 Confirm the contact is present on the peer by reading its contact list back; **no sighop transmission has occurred at this point**
- [x] 7.5 Ask the peer for one zero-hop advert (`send_advert(flood=False)`), with `sighop run` receiving and capturing: the advert must verify and become a contact, and path learning must record a zero-hop route (design D3)

## 8. First transmission and the exit criterion

- [x] 8.1 Start `sighop run --enable-transmit --entity <keyfile> --capture <file>`; read the banner back before proceeding
- [x] 8.2 Send the DM. **This is sighop's first transmission.** Record the interval between hand-off and `TxDone` — milestone 3's timeout factor has never met a real transmission (its design D8, open question 3)
- [x] 8.3 Confirm the peer received and displays the message, and that its acknowledgement matches the expectation sighop computed
- [x] 8.4 Have the peer send a DM to sighop. **Decrypt it. This is the milestone's exit criterion** (DESIGN.md §12)
- [x] 8.5 Confirm sighop's acknowledgement is accepted — the peer marks its message delivered rather than retrying
- [x] 8.6 Emit one zero-hop advert from sighop as a separate deliberate step, and confirm the peer records the contact from the air rather than from the USB injection
- [x] 8.7 Record whether the peer answered direct or flood-scoped (design open question 5), and whether the measured acknowledgement latency matches the formula's prediction (open question 1)
- [x] 8.8 Keep the whole exchange inside a 75 s window between the peer's power cycles; a reset mid-exchange means repeat, not debug (design D13)
- [x] 8.9 Note the duty-cycle consumption reported for the session — the first real figure the status line has ever shown

## 9. Turning the session into evidence

- [x] 9.1 Append the capture whole to the corpus per §12's rule, with a `capture_meta` header recording both boards' provenance
- [x] 9.2 Commit the exercise keyfile as a test fixture, marked **burned** in the file and at the point the test loads it (design D11)
- [x] 9.3 Add the foreign-implementation known-answer test: decrypt the recorded peer DM with the fixture key, assert the MAC verifies, the plaintext recovers and it parses with the expected timestamp and text, naming the capture file and record index
- [x] 9.4 Add the negative half of that vector: the same ciphertext with the cipher key taken as the full 32 bytes, or the MAC keyed on only the first 16, must fail — the two key slices stay distinguishable by evidence rather than by comment
- [x] 9.5 Add the acknowledgement vectors: the peer's acknowledgement for our message matches our computed expectation, and ours for its message matches what it accepted
- [x] 9.6 Teach the corpus harness the transmitted-frame record kind, decoding it through the same codecs and excluding it from reception-derived measurements such as duplicate rate
- [x] 9.7 **Replace every `TBD` in the `protocol-corpus` delta** with the measured figures, and update the harness's frame count, payload-type and hash-size distributions, and the coverage notes — this change cannot be archived while a placeholder remains
- [x] 9.8 Update the corpus documentation: decryption is now confirmed against live traffic for exactly this exchange, and for no other ciphertext in the corpus

## 10. Design document and close-out

- [x] 10.1 Update DESIGN.md §12 milestone 4 with what was found — the measured `TxDone` interval, the acknowledgement latency against the formula, how the peer routed its reply, and anything that contradicted this plan
- [x] 10.2 Update DESIGN.md §11 with the new modules, and §5 to retire the claim that decryption is unconfirmed against another implementation
- [x] 10.3 Update DESIGN.md §12's milestone 1 note about the corpus's "one hard limit", which this milestone removes for one exchange
- [x] 10.4 Record the milestone's decisions for cross-session recall, and close out any design open question the run answered
