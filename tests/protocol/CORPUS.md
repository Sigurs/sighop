# The protocol regression corpus

1093 frame records in eight files: **1058 received** off the live mesh, and
**35 that sighop itself transmitted**.

| File | Frames | Recorded | Provenance |
|---|---|---|---|
| `captures/2026-09-02.jsonl` | 152 | milestone 0, Heltec V3 | `.meta.json` sidecar |
| `captures/2026-09-03.jsonl` | 199 | milestone 0, Heltec V3 | `.meta.json` sidecar |
| `captures/2026-09-04.jsonl` | 56 | milestone 2, Heltec V4 OLED | `capture_meta` header |
| `captures/2026-09-04-02.jsonl` | 2 | milestone 2, Heltec V4 OLED | `capture_meta` header |
| `captures/2026-09-04-03.jsonl` | 33 | milestone 2, Heltec V4 OLED | `capture_meta` header |
| `captures/2026-09-05.jsonl` | 555 | milestone 3, Heltec V4 OLED | `capture_meta` header |
| `captures/2026-09-04-first-transmit.jsonl` | 6 (3 rx, 3 tx) | milestone 4, V4 OLED ↔ V3 peer | `capture_meta` header, both boards |
| `captures/2026-09-06-room-server.jsonl` | 90 (58 rx, 32 tx) | milestone 6, V4 OLED ↔ stock `v1.17.1` client | `capture_meta` header |

Per DESIGN.md §12 these files are the permanent regression corpus for the
protocol layer. The two milestone 0 files predate the `capture_meta` header
record and keep their sidecars; everything recorded from milestone 2 onward
carries its provenance in-band, as the first line of the file itself. Either
way every corpus file states the conditions it was recorded under —
`test_every_corpus_file_carries_provenance` refuses a file that states neither.

The first-transmit file is milestone 4's exercise: sighop's first three
transmissions and the peer's three replies, recorded on 2026-09-04 between
21:44 and 21:49 UTC. It is the only file holding frames sighop sent, and its
`capture_meta` header records **both** boards — the V4 that ran sighop and the
Heltec V3 running stock `companion_radio` v1.17.1-d929643 that produced the
ciphertext. Transmitted frames carry the record kind `tx_frame`; they decode
through the same codecs as receptions and are excluded from every
reception-derived measurement, the duplicate rate included.

The 2026-09-04 files are one overnight session split by device restarts. The
2026-09-05 file is milestone 3's long receive-only run — 2 h 54 min on the V4,
17:06–20:00 UTC, recorded by `sighop run --capture` while the TX scheduler
carried live advert load behind a closed gate. All of them were kept whole
rather than filtered down to their novel frames: a corpus of hand-picked
interesting frames stops being a sample of what the mesh actually carries.

The 2026-09-06 file is milestone 6's live room-server exercise, run in three
`sighop run` invocations against the same capture file: receive-only, then
transmit enabled with a zero-hop advert, then a restart partway through to
exercise the exit criterion. It holds the first live room login (`ANON_REQ`
carrying the login envelope), the first live room post and refusal (`TXT_MSG`
to the room's own node hash), the first pushes and their acknowledgements
(`ACK`, both 4-byte forms — this session never saw the 6-byte form the other
sessions did), and the two `PATH` exchanges either side used to learn a route
home. It also caught the discovery that drove design D4's revision: five posts
refused `too_long` at 156–158 bytes, before the fix that truncates and
acknowledges instead. Kept whole for the same reason as the rest: the refused
posts are exactly the evidence that the original design was wrong, and a
corpus edited down to only the "working" frames would have erased it.

**They are read-only evidence.** The harness opens them for reading and never
writes to them or their sidecars. Nothing regenerates them.

## What the corpus proves

- **Framing.** Every frame decodes structurally and re-encodes byte-identically,
  including the packed `path_length` encoding, all three live path hash sizes,
  hop counts 0-5, and the one transport-routed frame's transport codes.
- **Payload shapes.** Every payload parses to its envelope and rebuilds
  byte-identically across eleven payload types, CONTROL included — preserved
  uninterpreted, which is itself the recorded behaviour.
- **Adverts.** All 92 ADVERT frames pass Ed25519 signature verification, and
  their appdata decodes to consistent flags, node types and UTF-8 names.
- **That the multi-byte hash reading is the correct one.** Forcing 1-byte hashes
  yields corrupt flags and truncated names (`0xfa` / `rala Hill repeater`); the
  encoded hash size yields `0x92` / `[redacted]`. The corpus
  discriminates between the two candidate readings, which is how DESIGN.md §4.2's
  original `path_length > 64` rule was found to be wrong.

## What the corpus does NOT prove

**Decryption, except for exactly one exchange.** sighop holds the key for the
two direct messages in `2026-09-04-first-transmit.jsonl` (records 2 and 3) and
for **no other encrypted payload in the corpus**. Those two are decrypted on
every commit by `test_foreign_decrypt.py`, and record 3 was produced by a
different implementation — stock `companion_radio` v1.17.1-d929643 — which is
what makes it evidence rather than a round-trip against ourselves. Decryption is
therefore confirmed against live MeshCore traffic **for that exchange only**.

Every other ciphertext here stays unopenable and is verified only by round-trip
against our own keys and by the fixed known-answer vectors in `test_crypto.py`.
Nearly all of it is third-party traffic; the 2026-09-04 files also caught a
handful of exchanges involving the operator's own MeshCore node (`Sigurs`, key
`[redacted]7064e837…`), whose key sighop still does not hold. The golden file's
no-plaintext rule holds regardless, and now means something it did not before:
we hold a key for two of these frames and the golden file still renders them as
ciphertext digests.

A green corpus run still must not be read as evidence that decryption works in
general — it is evidence that it worked for one message from one firmware build
on one evening, which is exactly one more than the corpus could hold before.

Shapes absent from the corpus, each covered by a synthetic fixture instead:

| Absent shape | Where the synthetic fixture lives |
|---|---|
| `ROUTE_TYPE_TRANSPORT_DIRECT` | `test_packet.py::test_transport_routed_packet_carries_transport_codes` |
| Hop counts above 5 | `test_packet.py::test_hop_count_above_the_corpus_maximum` |
| The reserved `0b11` hash size code | `test_packet.py::test_reserved_hash_size_code_is_rejected` |
| Payload versions other than v1 | `test_packet.py::test_non_v1_payload_version_is_rejected` |
| MULTIPART, RAW_CUSTOM, reserved payload types | `test_payloads.py::test_unsupported_payload_types_are_preserved_not_dropped` |
| Adverts setting feature 1 / feature 2 (`0x20` / `0x40`) | `test_payloads.py::test_feature_fields_decode_in_wire_order` |
| Adverts with no name flag, or a non-UTF-8 name | `test_payloads.py::test_advert_with_no_name_flag_preserves_trailing_bytes`, `::test_name_that_is_not_valid_utf8_is_flagged_not_discarded` |
| Every decrypted body layout (text, group text, returned path, room login) | `test_payloads.py`, over synthetic plaintext |
| Node types NONE and SENSOR | `test_payloads.py::test_feature_fields_decode_in_wire_order` and neighbours |

The 2026-09-04 session closed three of these gaps with live evidence:
**`ROUTE_TYPE_TRANSPORT_FLOOD`** (one frame, an advert, transport codes
`0x0075`/`0x0000` — the first proof that the transport-code field is read from
real air and not only from a fixture we wrote), **CONTROL** (six frames, three
distinct lengths, all preserved uninterpreted), and the **CHAT** node type (six
adverts, flags `0x81`). The synthetic fixtures for them stay: they cover the
shapes around what happened to arrive.

The 2026-09-05 session added three more, and settled how ordinary one of the
2026-09-04 findings really was:

- **A chat node that reports a location** — flags `0x91`, two adverts from one
  node. Every chat advert before it was `0x81`, named with no location, so
  `ADV_LATLON_MASK` and a non-repeater node type had never been seen set
  together on real air.
- **A 10-byte TRACE**, against 13 and 21 bytes in every earlier trace.
- **CONTROL is routine traffic, not a curiosity.** 162 frames in this one
  session against six in the entire corpus before it, 154 of them at 38 bytes.
  The shape has not changed; its frequency has. Preserving CONTROL
  uninterpreted is now a path taken by roughly a fifth of the corpus.

## Recorded composition

Asserted by `test_corpus.py`; a decoder change that shifts classification fails
loudly even when every frame still decodes.

Counted over all 1093 records, receptions and transmissions alike — the
first-transmit session's 6 frames are every one `DIRECT` with an empty path, so
its whole contribution is `TXT_MSG` +2, `ACK` +2, `ADVERT` +2, `DIRECT` +6, hop
count 0 +6 and hash size 1 +6. The room-server session's 90 frames contribute
`TXT_MSG` +36, `ADVERT` +4, `ACK` +19, `ANON_REQ` +15, `PATH` +14, `REQ` +2,
`FLOOD` +51, `DIRECT` +39, hop count 0 +67, hop count 1 +23, hash size 1 +26,
hash size 3 +64.

- **Payload types**: GRP_TXT 341, TXT_MSG 229, CONTROL 168, ADVERT 98, ACK 88,
  ANON_REQ 57, PATH 52, RESPONSE 36, REQ 14, GRP_DATA 5, TRACE 5.
- **Route types**: FLOOD 553, DIRECT 539, TRANSPORT_FLOOD 1. No
  `TRANSPORT_DIRECT`.
- **Hop counts**: 0→491, 1→349, 2→177, 3→69, 4→5, 5→2.
- **Path hash sizes**: 1-byte 519, 2-byte 109, 3-byte 465.
- **Adverts**: 98, all verifying and all named; 78 with flags `0x92` (repeater,
  located, named — the room-server session added one more `[redacted]`, no new
  name), 10 with `0x81` (chat, named, no location — one more `Sigurs`, also not
  new), 6 with `0x93` (room server, located, named), 2 with `0x91` (chat,
  located, named), and 2 with `0x83` — **room server, named, no location** — a
  shape not seen before this session: `[redacted]`'s zero-hop advert carries no
  lat/lon, where every earlier room-server advert did. Eleven distinct names —
  the room-server session's only new one is `[redacted]` itself.
- **ACK payload lengths**: 4 bytes ×68, 6 bytes ×20. The room-server session's
  19 acknowledgements were every one 4 bytes, the plain checksum form; the
  6-byte form with a tail stays confirmed only by the first-transmit session.

## Flood repetition rate (input to the milestone 3 dedup cache)

DESIGN.md §13 lists dedup cache sizing as an in-flight unknown. Counting
duplicates the way the firmware does — `Packet::calculatePacketHash`, over the
payload type and payload bytes, plus `path_len` for TRACE — over the 351-frame
milestone 0 subset:

- 351 receptions carried **205 distinct packets**: **41.6% of receptions are
  repeats**, 1.71 receptions per distinct packet.
- Copies per packet: 82 seen once, 101 twice, 21 three times, 1 four times.
  **Maximum 4.**
- Flood frames repeat far more than direct ones: 94 of 171 flood receptions were
  repeats, against 52 of 180 direct.
- Duplicates arrive close together: median spread between first and last copy
  **1.3 s**, p95 **3.6 s**, maximum **31.1 s**.
- Distinct packets within a sliding window: **16** in any 60 s, 49 in any 300 s,
  56 in any 900 s.

The 2026-09-04 session, measured the same way, came out **much quieter**: 91
receptions, 81 distinct packets, **11.0% repeats**, at most 2 copies of any
packet, median spread 2.8 s and maximum 4.6 s, 24 distinct packets in any 60 s.
The directional finding survives and sharpens — all 10 repeats were flood
receptions, and **not one of the 57 direct receptions repeated** — but the
*rate* clearly is not a constant of this mesh: it moved from 41.6% to 11.0%
between nights, on a different board.

The 2026-09-05 session, being both long and busy, is the one that settled the
sizing. 555 receptions, 370 distinct, **33.3% repeats**, at most 3 copies of any
packet, 16 distinct packets in any 60 s and 49 in any 300 s. Over all **1000
receptions** — the 3 frames sighop transmitted are excluded, since the subject
here is what the mesh sent us: 659 distinct packets, 34.1% repeats, median gap
between consecutive copies **0.99 s**, p95 **3.3 s**, and only **two** gaps anywhere
above 60 s. Those two are worth naming, because they are not the same thing:

- **200.7 s** — a flood ANON_REQ whose late copy arrived by a *different* path
  (`23` against `be`, SNR −10.25 against 14.25). A genuine late echo, six times
  the 31.1 s the milestone 0 subset called its worst case.
- **3158 s (52.6 min)** — two **byte-for-byte identical** zero-hop DIRECT
  TXT_MSG frames, same ciphertext, same SNR. Not a copy of one transmission but
  the sender **retransmitting an unacked DM**.

So the earlier recommendation here — "~128 entries with a 60 s TTL, an order of
magnitude of headroom" — was wrong, and wrong because three short nights cannot
sample the tail of a duration. A 60 s TTL would have missed the 200.7 s copy
outright. The shipped defaults are **300 s and 4096 entries**, and the TTL is
bounded from both sides: shorter discards real flood copies, much longer starts
swallowing sender retries, which are events a user should see rather than have
deduplicated away. Peak occupancy at 300 s is 49 entries, so the cap is headroom
against a busier mesh, not a fit to this one.

## The golden file

`corpus_golden.txt` renders every decoded frame, one line each. It holds
**structural fields and ciphertext digests only, never decrypted plaintext** —
today because there is no plaintext to hold, and from milestone 4 onward as a
standing rule.

Regenerate deliberately and review the diff:

    uv run python -m tests.protocol.generate_golden

There is no `--update-golden` test flag, by design (D10): a generated
expectation records whatever the code did on the day, bugs included, so
regenerating it is a reviewed step in a change, not a way to make a red test go
green.
