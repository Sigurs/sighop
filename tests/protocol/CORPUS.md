# The protocol regression corpus

282 frame records in three files under `tests/corpus/`: **264 received** and **18 that
sighop itself transmitted**. The corpus is **synthetic**: a fictional mesh generated
from sighop's own encoders under a fixed seed, and committed.

| File | Frames | Job |
|---|---|---|
| `synthetic-ambient.jsonl` | 180 (all rx) | A receive-only mesh: adverts of every node type and flag form, flood and direct routing, hop counts 0–5, path hash sizes 1–3, TRACE of two lengths, PATH, discovery requests and responses, one `TRANSPORT_FLOOD`, third-party direct messages, acknowledgements, room logins, requests and responses, the flood repeats and the late echo |
| `synthetic-channels.jsonl` | 63 (all rx) | Group text on the Public channel (`0x11`) and on a second synthetic channel (`0x33`), and a few group-data frames |
| `synthetic-exchange.jsonl` | 39 (21 rx, 18 tx) + 1 `unparsed` | sighop and a synthetic peer: direct messages in both directions with both acknowledgement forms, a flooded message answered by a returned path, one identical retransmission, and a room sighop hosts — login, requests, posts, a guest whose posts are never acknowledged, pushes — plus sighop's own adverts as `tx_frame` |

Tests take the corpus by role (`AMBIENT`, `CHANNELS`, `EXCHANGE` in `corpus.py`), not by
date. Most only want "a realistic stream of receptions" and take `AMBIENT`.

## Why it is generated

The corpus used to be 13 files of live LoRa traffic recorded off a real mesh. That put
other people's node names, keys and Public-channel chat in the repository, and it made
coverage a matter of volume: about a third of the receptions were flood repeats of a
packet already seen. A generated corpus fixes both. Nothing real can be in it, and its
composition is chosen — wide coverage, a controlled and declared amount of repetition.

`tests/protocol/synthetic.py` is the generator. Same generator version and seed, same
bytes; `SEED` and `GENERATOR_VERSION` are written into every file's `capture_meta`
header, which also says the file is synthetic and describes no real device, board,
firmware or mesh. Regenerate deliberately and review the diff:

    uv run python -m tests.protocol.generate_corpus

That writes `tests/corpus/*.jsonl` and prints a **manifest**: record counts per file, per
payload type, route type, hop count, path hash size and advert form. The expectations in
`corpus.py` and `test_corpus.py` are typed in from a reviewed run of it, never written by
tooling, so a change to the generator fails the drift check, the manifest and the
recorded expectations together until each is updated in the same reviewed change.

**The corpus files are read-only generated artefacts.** The harness opens them for reading
and never writes to them; only the generator does. `test_synthetic_corpus.py` regenerates
the corpus in memory and fails, naming the file and the first differing line, if the
committed files differ. The golden file is regenerated the same way, with
`tests/protocol/generate_golden.py`.

## The cast

Every identity is a seeded Ed25519 key and every name carries the fixed prefix `syn-`, so
a reader cannot mistake one for real: twelve repeaters (`syn-ridge-repeater`, …), three
room servers (`syn-cellar-room`, `syn-attic-room`, and `syn-lounge-room`, the room sighop
hosts, which advertises with no location), eight chat nodes (`syn-alder`, …, four of them
with a location), and two local identities, `syn-sighop` and `syn-peer`. Locations are in a
small square around 0°N 0°E. The chat lines are written out in the generator; no text is
produced at run time. Every node's first key byte is its own, so a 1-byte path hash names
exactly one node.

The **no-real-data guard** (`test_synthetic_corpus.py`, `corpus_checks.py`) checks
membership in this cast, not a denylist of real names, so it needs no real data to exist
anywhere in the repository: every advert's key and name, every discovery response's key,
every ciphertext (which must open with a cast key) and every sender and text it opens to
must be the cast's. A scan of the tracked files also fails if any holds private-key
material outside test code that builds its keys in memory.

## What the corpus proves

- **Framing.** Every frame decodes structurally and re-encodes byte-identically, including
  the packed `path_length` encoding, all three path hash sizes, hop counts 0–5, and the
  one transport-routed frame's transport codes.
- **Payload shapes.** Every payload parses to its envelope and rebuilds byte-identically
  across eleven payload types, CONTROL included.
- **CONTROL is node discovery.** All 25 CONTROL frames decode as MeshCore node discovery:
  2 six-byte and 3 ten-byte `NODE_DISCOVER_REQ`, 20 thirty-eight-byte
  `NODE_DISCOVER_RESP`, every response from a REPEATER, every frame DIRECT with zero hops.
  A response's key is what its sender claimed — the payload carries no signature.
- **Adverts.** All 81 ADVERT frames pass Ed25519 signature verification, and their appdata
  decodes to consistent flags, node types and UTF-8 names in every form the mesh uses:
  `0x92` repeater (located), `0x81` chat, `0x91` chat (located), `0x93` room server
  (located) and `0x83` room server (no location).
- **The multi-byte hash reading.** Read with 1-byte hashes, a multi-hop advert with a
  multi-byte path hash yields corrupt flags or a truncated name; read with its encoded
  hash size it is correct. The corpus keeps advert frames on which the two readings
  differ, which is how DESIGN.md §4.2's original `path_length > 64` rule was found wrong.
- **Both acknowledgement forms.** 4-byte and 6-byte (a 2-byte tail), each matching the
  checksum computed for the message it answers.
- **Decryption, end to end, with the generator's keys.** Direct messages between the cast's
  nodes, group text on Public and on the second channel, the `0x11` / `0x17` channel hash
  discrimination, the two key slices, and the closed-channel negative
  (`test_corpus_decrypt.py`, `test_corpus_channel_decrypt.py`).
- **Dedup and path learning have something to see.** See *Repetition* below.

## What the corpus does NOT prove

**Interoperability.** The corpus is generated by sighop's own encoders, so a green run
proves the decoders agree with the encoders and with the expectations recorded in the
tests. It does not prove interoperability with any other MeshCore implementation, and no
test here may be read as if it did.

**Cryptography.** The decryption tests are self-consistency tests: sighop's encryptor
against sighop's decryptor. What anchors sighop to other implementations is only the
**firmware-embedded signing keypair** and the **fixed known-answer vectors transcribed
from the firmware source**, both in `test_crypto.py` and neither touching the corpus.
The earlier recorded corpus once held one exchange with a stock firmware (a direct message
opened with a key we held, and both acknowledgement forms) and 85 Public-channel frames
from other nodes, which were decrypted on every commit. That evidence was withdrawn with
the recorded corpus, together with the key that opened it: a synthetic corpus can only
prove sighop against itself. DESIGN.md records that the exchange existed and passed once.
If interoperability evidence is wanted again it is its own change, and it must scrub
before it commits.

**A misreading shared by generator and decoder.** The recording found real misreadings —
the multi-byte path-hash rule, and a wrong acknowledgement construction — because it was
someone else's implementation. A generated corpus cannot surprise us that way. The
discriminations that recording taught are kept as explicit cases (multi-byte hash
adverts, both acknowledgement forms, the discovery forms), and the fixed vectors above
keep the constructions anchored.

Shapes the corpus does not exercise, each covered by a hand-built fixture instead:

| Absent shape | Where the fixture lives |
|---|---|
| `ROUTE_TYPE_TRANSPORT_DIRECT` | `test_packet.py::test_transport_routed_packet_carries_transport_codes` |
| Hop counts above 5 | `test_packet.py::test_hop_count_above_the_corpus_maximum` |
| The reserved `0b11` hash size code | `test_packet.py::test_reserved_hash_size_code_is_rejected` |
| Payload versions other than v1 | `test_packet.py::test_non_v1_payload_version_is_rejected` |
| MULTIPART, RAW_CUSTOM, reserved payload types | `test_payloads.py::test_unsupported_payload_types_are_preserved_not_dropped` |
| CONTROL subtypes other than node discovery, and an empty CONTROL payload | `test_payloads.py::test_non_discovery_control_is_preserved_uninterpreted` |
| The 14-byte key-prefix discovery response, and a response from a non-REPEATER node type | `test_payloads.py::test_discover_response_with_key_prefix_and_negative_snr`, `::test_discover_response_with_an_unnamed_node_type_still_parses` |
| A discovery request with the prefix-only flag or a filter other than `0x04`/`0x06`/`0x10` | `test_payloads.py::test_discover_request_preserves_every_flag_bit` |
| Adverts setting feature 1 / feature 2 (`0x20` / `0x40`) | `test_payloads.py::test_feature_fields_decode_in_wire_order` |
| Adverts with no name flag, or a non-UTF-8 name | `test_payloads.py::test_advert_with_no_name_flag_preserves_trailing_bytes`, `::test_name_that_is_not_valid_utf8_is_flagged_not_discarded` |
| Every decrypted body layout (text, group text, returned path, room login) | `test_payloads.py`, over synthetic plaintext |
| Node types NONE and SENSOR | `test_payloads.py::test_feature_fields_decode_in_wire_order` and neighbours |

The corpus holds one `ROUTE_TYPE_TRANSPORT_FLOOD` frame and its CONTROL frames, and
`test_corpus.py` asserts them against the frames themselves — the transport codes, and
for CONTROL the discovery subtype, tag and length form with a byte-identical rebuild —
while the hand-built fixtures stay.

## Recorded composition

Asserted by `test_corpus.py`; a decoder change that shifts classification fails loudly
even when every frame still decodes. Counted over all 282 records, receptions and
transmissions alike.

- **Payload types**: GRP_TXT 59, ADVERT 81, TXT_MSG 43, CONTROL 25, ACK 24, ANON_REQ 14,
  PATH 14, RESPONSE 7, TRACE 6, REQ 5, GRP_DATA 4.
- **Route types**: FLOOD 188, DIRECT 93, TRANSPORT_FLOOD 1. No `TRANSPORT_DIRECT`.
- **Hop counts**: 0→83, 1→73, 2→57, 3→43, 4→19, 5→7.
- **Path hash sizes**: 1-byte 140, 2-byte 83, 3-byte 59.
- **Adverts**: 81, all verifying and all named, 25 distinct names: 53 with flags `0x92`,
  10 with `0x81`, 9 with `0x91`, 4 with `0x93` and 5 with `0x83`.
- **ACK payload lengths**: 4 bytes ×18, 6 bytes ×6.

## Repetition

The recorded corpus was about a third repeats. The budget here is: **at most 12% of
receptions repeat a packet already seen, and at most 3 copies of any packet.** The
generator has one function that emits a repeat and it takes a `reason`, so every duplicate
in the output traces to a line that says why a test needs it. The declared repeats are:

- **Flood repeats** over a different path, seconds apart — the same payload and hash
  size, a longer path, different hop count and signal readings, within ten seconds of the
  first copy (what `test_dedup` and path learning need). 23 receptions of 264 in all,
  8.7%.
- **Exactly one late echo**: a flood `ANON_REQ` whose copy arrives over a different path
  200.7 s after the first, which is what shows a 60 s dedup TTL is too short.
- **Exactly one retransmitted direct message**: byte-identical, 31.5 minutes after the
  first, standing for a sender retrying an unacknowledged message — which is why the TTL
  must stay short enough not to swallow a retry.

Everything else is unique by construction, and `test_synthetic_corpus.py` checks it: it
fails if the share is over the ceiling, a packet has too many copies, more than one
late echo or retransmission exists, or anything repeats that the generator did not
declare. The recorded measurements that sized the dedup cache are in DESIGN.md.

## The golden file

`corpus_golden.txt` renders every decoded frame, one line each. It holds
**structural fields and ciphertext digests only, never decrypted plaintext** — the
generator holds every key in the synthetic corpus, so plaintext could be rendered and is
not, and the rule stands if a recorded frame is ever added.

Regenerate deliberately and review the diff:

    uv run python -m tests.protocol.generate_golden

There is no `--update-golden` test flag, by design (D10): a generated expectation records
whatever the code did on the day, bugs included, so regenerating it is a reviewed step in
a change, not a way to make a red test go green.

## Live recordings

`sighop run --capture` (`SIGHOP_CAPTURE_FILE`) still writes ordinary capture JSONL, and
`captures/` is gitignored so a recording cannot be committed by accident: it is other
people's traffic. A recording can be replayed with `python -m sighop.replay`, and used to
find a bug; it is not corpus material until it has been scrubbed of every real name, key
and message.
