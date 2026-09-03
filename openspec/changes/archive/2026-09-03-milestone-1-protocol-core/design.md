## Context

Milestone 0 left sighop able to hear the mesh and record it: 351 frames across two overnight
runs, zero reconnects, zero malformed frames. Nothing above `radio/` exists. This milestone
builds `protocol/` — the layer DESIGN.md §11 constrains to have **no dependency on `db/` or
`net/`**, "pure functions over bytes", "the layer where correctness matters most".

Three constraints shape every decision below:

1. **Interoperability is the whole point.** MeshCore's wire cryptography is weak — AES-128-ECB,
   no nonce, a 2-byte MAC, unkeyed acknowledgement checksums. We reproduce it exactly. The
   design's job is to reproduce it *and* to stop the rest of the system from trusting it more
   than it deserves.
2. **The corpus is ground truth, and it has a hard limit.** It proves framing, adverts and
   signature verification. It cannot prove decryption: every encrypted payload in it is
   addressed to someone else. Pretending otherwise would be the most dangerous kind of green
   test suite.
3. **The authority is the firmware source, not the docs, and not DESIGN.md.** Checking the
   vendored submodule while writing these specs produced two corrections to DESIGN.md, one of
   which (the ACK construction) would have silently broken every acknowledgement sighop ever
   sent.

### What the corpus already told us

| Finding | Evidence | Consequence |
|---|---|---|
| Multi-byte path hashes are live | 152 frames 1-byte, 106 2-byte, 93 3-byte | The codec must implement the packed `path_length` encoding; DESIGN.md §4.2's `path_length > 64` check is wrong |
| Advert appdata low nibble is an enum | Flags `0x92`/`0x93` observed; `app_data[0] = _type` then `\|= mask` in `AdvertDataHelpers.cpp` | `0x03` is "room server", not `chat\|repeater` |
| ACK is truncated SHA-256, not CRC32 | `BaseChatMesh.cpp:243` — `sha256(ack_hash, 4, data, 5+text_len, pub_key, 32)` | DESIGN.md §5 is wrong; a CRC32 implementation would never produce an acknowledgement any node accepts |
| HMAC key is the full 32-byte secret | `Utils.cpp:133` uses `PUB_KEY_SIZE`, while `encrypt()` uses `CIPHER_KEY_SIZE` | Cipher key and MAC key are different slices of the same secret |
| Decode is unambiguous under the right reading | Forcing 1-byte hashes yields `0xfa`/`rala Hill repeater`; the encoded size yields `0x92`/`[redacted]` | The corpus discriminates between candidate readings — use it that way |

## Goals / Non-Goals

**Goals:**

- A packet codec that decodes and re-encodes every corpus frame byte-for-byte.
- A payload codec covering every payload type v1 originates, plus preservation of the ones it
  does not.
- Cryptography that matches the firmware bit-for-bit, with the trust boundaries expressed in
  the API rather than only in comments.
- A regression corpus harness that fails loudly on drift, and that documents its own gaps.
- DESIGN.md corrected in this change, per its own rule.

**Non-Goals:**

- Anything stateful: dedup, path learning, contact tracking, replay-rejection storage
  (milestone 3 and 5). This layer *exposes* the timestamps and identities those need; it does
  not remember anything.
- Any I/O. No serial, no sockets, no database, no `asyncio`.
- MULTIPART reassembly and originating transport codes — explicit v1 non-goals (DESIGN.md §2).
- A high-level "session" or "conversation" API. Milestone 4 will show what shape that wants;
  guessing now would be building the wrong thing carefully.

## Decisions

### D1. Two layers, not one: structural codec below, payload codec above

`packet.py` decodes header/transport codes/path/payload-extent and stops at the payload
boundary. `payloads.py` interprets the payload bytes. Alternative considered: one decoder
producing a fully-interpreted packet. Rejected because the split is exactly the seam the
corpus harness needs — a frame whose payload we cannot interpret (MULTIPART, CONTROL, a
reserved type) must still decode structurally, round-trip, and count toward the corpus
distribution. Fusing the layers would force us to choose between rejecting such frames and
letting the payload layer's failures look like framing failures.

### D2. Three-stage payload model: envelope → ciphertext → body

Payload parsing has three distinct stages with different trust levels, and the types make the
distinction:

- **Envelope** — parseable from the packet alone (hashes, MAC, ciphertext extent). Always
  available.
- **Ciphertext** — needs a key to go further. May be un-openable forever; that is normal, not
  an error, and is the state of every encrypted frame in the corpus.
- **Body** — only exists after successful MAC verification and decryption.

Alternative considered: a single `parse_payload(payload, keys)` returning an optional body.
Rejected because it makes "we could not decrypt this" and "this is malformed" the same
outcome, and because the RX pipeline (milestone 3) needs the envelope *before* it has decided
which entity keys to try.

### D3. Errors are returned, not raised, on the decode path

Decoding returns a result carrying either a packet or a structured failure (which limit was
violated, at what offset, with the raw bytes). Alternative considered: exceptions. Rejected
because the RX pipeline must log every malformed frame as a wide event and carry on — DESIGN.md
§4.1 is explicit that a silently dropped frame is invisible forever — and because the corpus
harness needs to report *which* frame failed, not just that something did. Encoding, whose
inputs are ours, raises instead: an over-limit outbound packet is a bug in our code.

### D4. Immutable dataclasses over `bytes`, no `memoryview` optimisation

`@dataclass(frozen=True, slots=True)` with `bytes` fields. Packets are ≤255 bytes and arrive a
few times a minute; zero-copy slicing would buy nothing and would make the frozen-value
guarantee unenforceable. Revisit only if profiling ever says otherwise, which at this traffic
rate it will not.

### D5. `PyNaCl` for Ed25519 and X25519, `cryptography` for AES-128-ECB

`cryptography` does not expose `crypto_sign_ed25519_pk_to_curve25519`, which the ECDH
derivation requires; PyNaCl does, via libsodium. PyNaCl in turn does not offer raw AES-ECB —
deliberately, since it is unsafe — so `cryptography`'s `hazmat` layer provides it. Alternative
considered: a pure-Python Ed25519→X25519 conversion to avoid a dependency. Rejected: hand-rolled
curve arithmetic in the layer "where correctness matters most" is precisely the wrong trade.

MeshCore private keys carry a pre-clamped scalar, so the usual Ed25519 hash-and-clamp step is
skipped (DESIGN.md §5). This is the single most fiddly detail in the milestone and gets a
fixed known-answer vector, not just a round-trip test — a round-trip against ourselves is
self-consistent even when it is wrong in exactly this way.

### D6. Padding ambiguity is resolved by the body parser, never by the cipher

AES-128-ECB decryption returns a multiple of 16 bytes; the original plaintext length is not
recoverable, and a plaintext that happened to end in zeros is indistinguishable from padding.
The cipher layer therefore returns the padded buffer verbatim and each body parser strips
trailing zeros using its own knowledge of the layout. Alternative considered: stripping zeros
centrally in the cipher. Rejected — it would corrupt any future binary body whose last field
legitimately ends in `0x00`, and GRP_DATA already carries length-prefixed binary.

### D7. Verification results are values, not side effects

`verify_advert()` returns a `VerifiedAdvert` or a failure; there is no way to obtain parsed
advert content without holding the verification outcome in your hand. Likewise MAC checking
returns a candidate-match value whose type name says "candidate". Alternative considered: a
`verified: bool` field on a single advert type. Rejected because a boolean is trivially ignored,
and DESIGN.md §5 and §8 both make "never present unverified data as verified" a hard rule.
Making it a type is how the rule survives contact with the UI three milestones later.

### D8. The corpus harness asserts three things, at three granularities

- **Per-frame**: every frame decodes, and re-encodes byte-identically.
- **Aggregate**: the distribution of payload types, route types, hop counts and hash sizes
  matches recorded counts — this catches a misread header that still parses.
- **Golden file**: a checked-in rendering of every decoded frame, diffed on each run.

The golden file contains structural fields and ciphertext digests only. It never contains
decrypted plaintext, because the corpus is other people's traffic and sighop holds no key for
any of it — but the rule is written down now so it still holds at milestone 4, when we *will*
hold keys and the temptation to snapshot a decrypted body will be real.

### D9. Corrections land in DESIGN.md in this change

DESIGN.md's own header states it is the contract the implementation follows and that "when
reality disagrees with it, update this file in the same change". Both findings (path length encoding, ACK construction) are edits to
DESIGN.md §4.2 and §5 within this change, not follow-ups. The alternative — a note in the
proposal only — leaves the contract wrong for whoever reads it next.

### D10. Corpus expectations are generated once, then checked in and reviewed

The distribution counts and the golden file are produced by a script under `tests/`, committed,
and thereafter treated as fixtures. The risk is obvious — a generated expectation records
whatever the code did on the day, bugs included — so generation is a one-time reviewed step
whose output is spot-checked against the independent analysis in the proposal (advert names,
type counts), not a `--update-golden` flag anyone can reach for when a test goes red.

## Risks / Trade-offs

**[The corpus cannot verify decryption, and a green suite may imply otherwise]** → The gap is
stated in the `protocol-corpus` spec, in the corpus documentation, and in this design. Cipher,
MAC and ECDH get fixed known-answer vectors rather than only self-round-trips, so at minimum we
match a recorded expected output rather than merely agreeing with ourselves. The real close-out
is milestone 4's exchange with the reference peer, and the milestone 4 plan should treat "first
successful decrypt of a real MeshCore DM" as an explicit exit criterion.

**[Known-answer vectors have to come from somewhere]** → We cannot generate them from the
firmware without flashing and instrumenting a board, which this milestone deliberately avoids.
Vectors are derived from the firmware source's construction, reproduced independently in the
test (e.g. computing the ACK hash from primitives inline, then asserting our implementation
agrees). That catches an implementation slip but not a misreading of the source shared by both
— so the source references are cited inline at each vector, making the misreading reviewable.

**[Reproducing weak cryptography invites its misuse upstream]** → Handled in the type system
(D7) rather than in documentation: candidate-match naming, verification-result values, an
unverified flag on group sender names, a weak-key flag on hashtag-derived channels. The
milestone 8 UI is three milestones away from these decisions and will not remember the
warnings; it will still have to unwrap the types.

**[Multi-byte path hashes may interact with something we have not seen]** → 40% of corpus
frames use them, and adverts decode cleanly under that reading, so the encoding itself is
settled. What is not settled is whether the mesh's repeaters *append* to a path in a mixed-size
world. That is a milestone 3 path-learning concern; this layer decodes the field and records
the hash size, which is what milestone 3 will need.

**[The corpus is 351 frames from one location on two nights]** → It covers ten payload types
and hop counts 0-5, which is more than a synthetic suite would, but it is not exhaustive.
Absent shapes are enumerated in the corpus spec and get synthetic fixtures. Expect milestone 2
(live decode) to surface frames the corpus never contained; the corpus is designed to be
appended to when it does.

**[DESIGN.md may be wrong elsewhere in ways this milestone does not touch]** → Two errors in
the sections this milestone reads is not a reassuring rate. §4.3's airtime and duty-cycle
figures are the next-highest-stakes unverified claims in the document and are load-bearing for
regulatory compliance; milestone 3 should verify them against RadioLib's time-on-air
computation the same way this milestone verified the wire format against `Utils.cpp`.

## Open Questions

- **Does any repeater on this mesh mix path hash sizes within one packet's lifetime?** Not
  answerable from the corpus (a frame carries one size code). Milestone 2's live decode is
  where it would show up.
- **What does the 2-byte `feature 1`/`feature 2` appdata actually carry in the wild?** No corpus
  advert sets `0x20` or `0x40`. Preserved as raw bytes; not interpreted.
- **Is the corpus's dedup-relevant flood repetition rate high enough to size the milestone 3
  dedup cache?** DESIGN.md §13 lists this as an in-flight unknown. **Answered** (task 9.3):
  counting duplicates as `Packet::calculatePacketHash` does, the 351 receptions carried 205
  distinct packets — 41.6% repeats, at most 4 copies of any one packet, duplicates arriving
  within 3.6 s of each other at p95 and 31.1 s at worst, and at most 16 distinct packets in any
  60 s window. A ~128-entry cache with a 60 s TTL covers everything observed by an order of
  magnitude. Full numbers in `tests/protocol/CORPUS.md`; milestone 3 should re-measure rather
  than treat them as constants, since this is one location on two nights.
