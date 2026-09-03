# The protocol regression corpus

351 frames captured off the live mesh on two consecutive nights, in
`captures/2026-09-02.jsonl` (152 frames) and `captures/2026-09-03.jsonl` (199).
Provenance is in the paired `.meta.json` sidecars. Per DESIGN.md §12 these files
are the permanent regression corpus for the protocol layer.

**They are read-only evidence.** The harness opens them for reading and never
writes to them or their sidecars. Nothing regenerates them.

## What the corpus proves

- **Framing.** Every frame decodes structurally and re-encodes byte-identically,
  including the packed `path_length` encoding, all three live path hash sizes,
  and hop counts 0-5.
- **Payload shapes.** Every payload parses to its envelope and rebuilds
  byte-identically across ten payload types.
- **Adverts.** All 53 ADVERT frames pass Ed25519 signature verification, and
  their appdata decodes to consistent flags, node types and UTF-8 names.
- **That the multi-byte hash reading is the correct one.** Forcing 1-byte hashes
  yields corrupt flags and truncated names (`0xfa` / `rala Hill repeater`); the
  encoded hash size yields `0x92` / `[redacted]`. The corpus
  discriminates between the two candidate readings, which is how DESIGN.md §4.2's
  original `path_length > 64` rule was found to be wrong.

## What the corpus does NOT prove

**Decryption.** This is the important one. Every encrypted payload in the corpus
is addressed to a third party and sighop holds no key for any of it. Ciphertext
handling is verified only by round-trip against our own keys and by the fixed
known-answer vectors in `test_crypto.py`. Nothing here confirms that sighop
decrypts the way MeshCore does. The milestone 4 exchange with the reference peer
board is what closes that gap, and "first successful decrypt of a real MeshCore
DM" is its explicit exit criterion. A green corpus run must not be read as
evidence that decryption works.

Shapes absent from the corpus, each covered by a synthetic fixture instead:

| Absent shape | Where the synthetic fixture lives |
|---|---|
| `ROUTE_TYPE_TRANSPORT_FLOOD` / `TRANSPORT_DIRECT` and their transport codes | `test_packet.py::test_transport_routed_packet_carries_transport_codes` |
| Hop counts above 5 | `test_packet.py::test_hop_count_above_the_corpus_maximum` |
| The reserved `0b11` hash size code | `test_packet.py::test_reserved_hash_size_code_is_rejected` |
| Payload versions other than v1 | `test_packet.py::test_non_v1_payload_version_is_rejected` |
| MULTIPART, CONTROL, RAW_CUSTOM, reserved payload types | `test_payloads.py::test_unsupported_payload_types_are_preserved_not_dropped` |
| Adverts setting feature 1 / feature 2 (`0x20` / `0x40`) | `test_payloads.py::test_feature_fields_decode_in_wire_order` |
| Adverts with no name flag, or a non-UTF-8 name | `test_payloads.py::test_advert_with_no_name_flag_preserves_trailing_bytes`, `::test_name_that_is_not_valid_utf8_is_flagged_not_discarded` |
| Every decrypted body layout (text, group text, returned path, room login) | `test_payloads.py`, over synthetic plaintext |
| Node types NONE, CHAT and SENSOR | `test_payloads.py::test_feature_fields_decode_in_wire_order` and neighbours |

## Recorded composition

Asserted by `test_corpus.py`; a decoder change that shifts classification fails
loudly even when every frame still decodes.

- **Payload types**: TXT_MSG 108, GRP_TXT 91, ADVERT 53, ACK 42, ANON_REQ 16,
  PATH 15, RESPONSE 11, REQ 6, GRP_DATA 5, TRACE 4.
- **Route types**: FLOOD 171, DIRECT 180. No transport-routed frames.
- **Hop counts**: 0→119, 1→66, 2→95, 3→64, 4→5, 5→2.
- **Path hash sizes**: 1-byte 152, 2-byte 106, 3-byte 93.
- **Adverts**: 53, all verifying; 47 with flags `0x92` (repeater, located,
  named), 6 with `0x93` (room server, located, named). Five distinct names.
- **ACK payload lengths**: 4 bytes ×31, 6 bytes ×11.

## Flood repetition rate (input to the milestone 3 dedup cache)

DESIGN.md §13 lists dedup cache sizing as an in-flight unknown. Counting
duplicates the way the firmware does — `Packet::calculatePacketHash`, over the
payload type and payload bytes, plus `path_len` for TRACE — over the decoded
corpus:

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

So on this mesh a dedup cache of ~128 entries with a 60 s TTL covers every
duplicate observed with an order of magnitude of headroom. That is one location
on two nights, not a design limit — milestone 3 should re-measure rather than
treat these as constants.

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
