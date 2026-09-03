## Why

Milestone 0 produced 351 real frames off the live mesh (`captures/2026-09-02.jsonl`,
`captures/2026-09-03.jsonl`) and stopped at raw bytes: sighop can hear the mesh but cannot
read a word of it. Milestone 1 turns those bytes into structure — packet codec, payload
codec, and the MeshCore cryptography — as pure functions with no radio, no database and no
network, verified against the captured corpus rather than against a reading of the spec.

Everything above this layer (live decode, scheduler, room server, bots, UI) is a consumer of
it, and per DESIGN.md §11 `protocol/` is the layer "where correctness matters most". It is
also the cheapest place in the project to be wrong and find out: the corpus is on disk, the
tests are offline, and nothing transmits.

A first pass over the corpus already found one place where DESIGN.md is wrong about the wire
format, which is precisely the class of error this milestone exists to catch (see Impact).

## What Changes

- New `protocol/packet.py` — v1 packet decode/encode: header (`0bVVPPPPRR`), the optional
  4-byte transport codes for `ROUTE_TYPE_TRANSPORT_*`, the packed `path_length` byte, the
  path, and the raw payload slice. Structural only; it does not interpret payload bodies.
- New `protocol/payloads.py` — parse and build each payload type v1 uses: ADVERT, ACK, the
  shared REQ/RESPONSE/TXT_MSG/PATH envelope (dest hash, src hash, 2-byte MAC, ciphertext),
  ANON_REQ, GRP_TXT, GRP_DATA, TRACE. Plaintext bodies too: the timestamp + `txt_type`/attempt
  text-message layout, the room-server login body, the returned-path body with its bundled
  `extra` payload.
- New `protocol/crypto.py` — MeshCore's cryptography exactly as it is on the wire (DESIGN.md
  §5): Ed25519 identity and node hash, Ed25519→X25519 conversion and cached ECDH, AES-128-ECB
  over zero-padded plaintext keyed on the first 16 bytes of the shared secret, HMAC-SHA256
  truncated to 2 bytes, channel-key derivation from a hashtag, advert signing and
  verification, and the ACK CRC32.
- New `protocol/identity.py` — keypair generation with node-hash collision rejection against
  a supplied set of existing local hashes (DESIGN.md §3).
- New regression corpus harness — the milestone 0 capture files are replayed in the test
  suite: every frame must decode, the aggregate type/route/hop distribution is asserted, and
  a golden-file snapshot of decoded output guards against silent regressions.
- New dependencies: `PyNaCl` (Ed25519→Curve25519 conversion, which `cryptography` does not
  expose) and `cryptography` (AES-128-ECB).
- **Correction to DESIGN.md §4.2** — the documented validation rule `path_length > 64` is
  wrong; `path_length` is a packed byte, not a length (see Impact).
- Receive-only remains trivially true: this milestone adds no code that can reach a radio.
  The encode paths exist so decode can be tested by round-trip and so milestone 4 has
  something to call.

## Capabilities

### New Capabilities
- `packet-codec`: structural decode and encode of the MeshCore v1 packet — header fields,
  optional transport codes, the packed `path_length` hop-count/hash-size encoding, path
  bytes, and payload extent, together with the size limits that make a packet invalid.
- `payload-codec`: parse and build the body of each v1 payload type, including the encrypted
  envelope shape shared by REQ/RESPONSE/TXT_MSG/PATH and the plaintext layouts those
  envelopes carry once decrypted.
- `mesh-crypto`: MeshCore's cryptographic primitives as the wire requires them — identity
  keys and node hashes, ECDH shared secrets, AES-128-ECB with zero padding, the 2-byte
  truncated HMAC, channel keys, advert signature verification, and the ACK checksum.
- `protocol-corpus`: the captured-frame regression corpus and the harness that replays it,
  so the protocol layer is verified against recorded reality rather than synthetic fixtures.

### Modified Capabilities
(none — `kiss-transport`, `modem-rx` and `capture-cli` are untouched; this milestone sits
entirely above the radio and does not open a serial port)

## Impact

- **A wire-format finding that contradicts DESIGN.md.** §4.2 says to reject packets with
  `path_length > 64`. `path_length` is not a byte count: bits 0-5 are the hop count and bits
  6-7 are `hash_size - 1`. Multi-byte path hashes are not hypothetical on this mesh — of the
  351 captured frames, 152 use 1-byte hashes, 106 use 2-byte and 93 use 3-byte. Decoding
  adverts under the assumption of 1-byte hashes yields corrupt flags and truncated names
  (`0xfa`/`rala Hill repeater`); decoding with the encoded hash size yields consistent flags
  and clean names (`0x92`/`[redacted]`). The correct check is
  `hop_count * hash_size <= 64` (`MAX_PATH_SIZE`). DESIGN.md is updated in this change, per
  its own rule that reality disagreeing with it is fixed in the same change.
- **A second, smaller correction:** DESIGN.md §3 and §5 read the advert appdata flags byte as
  pure bit flags, but `0x03` (room server) is not `0x01|0x02`. The low nibble is a node-type
  enum; only the high nibble (`0x10` location, `0x20`/`0x40` features, `0x80` name) is
  bitwise. Observed live: `0x92` (repeater, located, named) and `0x93` (room server, located,
  named).
- **Reference material:** the vendored `related-repos/MeshCore/docs/packet_format.md` and
  `payloads.md` are authoritative for this change and are to be consulted directly rather
  than re-derived from DESIGN.md's summary. `src/Packet.cpp`, `Identity.cpp` and `Utils.cpp`
  in the same submodule settle anything the docs leave ambiguous.
- **New code:** `src/sighop/protocol/` (packet, payloads, crypto, identity), `tests/protocol/`
  and the corpus harness. No changes to `src/sighop/radio/`.
- **New dependencies:** `pynacl`, `cryptography`. Both are C-extension packages and land in
  the container image at milestone 9 — worth noting there, not a problem here.
- **Corpus coverage is good but not complete.** The 351 frames exercise TXT_MSG (108),
  GRP_TXT (91), ADVERT (53), ACK (42), ANON_REQ (16), PATH (15), RESPONSE (11), REQ (6),
  GRP_DATA (5) and TRACE (4), across flood and direct routing and hop counts 0-5. Not
  present: any `ROUTE_TYPE_TRANSPORT_*` packet (so transport-code decoding gets synthetic
  tests only), MULTIPART (an explicit v1 non-goal), CONTROL and RAW_CUSTOM.
- **Decryption cannot be verified against the corpus.** Every encrypted payload in it is
  addressed to someone else, so ciphertext handling is provable only by round-trip against
  our own keys plus a fixed known-answer vector. The corpus proves framing and adverts; it
  cannot prove we decrypt like MeshCore does. Milestone 4's exchange with the reference peer
  board is what actually closes that, and this change should not pretend otherwise.
- **Out of scope, deliberately:** dedup, path learning, the bus and the scheduler
  (milestone 3); anything touching the database (milestone 5); `PAYLOAD_TYPE_MULTIPART` and
  originating transport codes (v1 non-goals, DESIGN.md §2).
