## 1. Scaffolding and dependencies

- [x] 1.1 Add `pynacl` and `cryptography` to `pyproject.toml` dependencies and refresh `uv.lock`
- [x] 1.2 Create `src/sighop/protocol/` (`__init__.py`, `packet.py`, `payloads.py`, `crypto.py`, `identity.py`) and `tests/protocol/`
- [x] 1.3 Add an import-boundary test asserting `sighop.protocol.*` imports nothing from `sighop.radio`, `sighop.db`, `sighop.net`, `asyncio` or `serial` (DESIGN.md §11)
- [x] 1.4 Define the shared result type used across the decode path: either a decoded value or a structured failure carrying reason, byte offset and raw bytes (design D3)

## 2. Packet codec (`packet-codec`)

- [x] 2.1 Define header constants and enums: `RouteType`, `PayloadType` (including reserved values), payload version; decode `0bVVPPPPRR` with masks `0x03`/`0x3C`/`0xC0`
- [x] 2.2 Reject non-v1 payload versions, reporting the observed version rather than parsing under v1 field sizes
- [x] 2.3 Decode the optional 4-byte transport code block (two little-endian `uint16`) for `TRANSPORT_FLOOD`/`TRANSPORT_DIRECT` only, preserving both values uninterpreted
- [x] 2.4 Decode the packed `path_length` byte: hop count from bits 0-5, hash size from bits 6-7 plus one; split the path into hops of that size; reject hash size code `0b11` as reserved
- [x] 2.5 Enforce size limits: path extent (`hop_count * hash_size`) ≤ 64, payload ≤ 184, total ≤ 255, and truncation before any declared field — each failure naming the limit violated
- [x] 2.6 Implement `encode()` mirroring the above, computing the path length byte from hop count and hash size, and raising on over-limit input (design D3)
- [x] 2.7 Represent decoded packets as `@dataclass(frozen=True, slots=True)` over `bytes` (design D4)
- [x] 2.8 Unit tests for every `packet-codec` scenario, including synthetic fixtures for the corpus gaps: transport-routed packets, hop counts above 5, the reserved hash size code, mid-path truncation, empty payload

## 3. Payload codec — envelopes (`payload-codec`)

- [x] 3.1 Define the three-stage payload model — envelope, ciphertext, body — as distinct types (design D2)
- [x] 3.2 Parse and build the shared REQ/RESPONSE/TXT_MSG/PATH envelope: dest hash, src hash, 2-byte MAC, ciphertext; reject ciphertext that is not a positive multiple of 16
- [x] 3.3 Parse and build ANON_REQ: dest hash, 32-byte sender public key, 2-byte MAC, ciphertext
- [x] 3.4 Parse and build GRP_TXT and GRP_DATA: channel hash, 2-byte MAC, ciphertext
- [x] 3.5 Parse and build ACK: a 4-byte checksum plus the optional 2-byte tail (extended attempt byte + random byte) current firmware appends, accepting lengths 4 and 6 only — the corpus carries 31 of the former and 11 of the latter, so the original "exactly 4" rule was wrong (`BaseChatMesh.cpp:245-247`)
- [x] 3.6 Represent MULTIPART, CONTROL, RAW_CUSTOM and reserved types as recognized-but-unparsed payloads preserving raw bytes
- [x] 3.7 Preserve TRACE payload bytes and identify the type without interpreting per-hop SNR
- [x] 3.8 Unit tests for each envelope shape, including the block-alignment rejection and the ACK length rejection

## 4. Payload codec — adverts (`payload-codec`)

- [x] 4.1 Parse ADVERT: 32-byte public key, 4-byte LE timestamp, 64-byte signature, appdata remainder; reject payloads shorter than 101 bytes
- [x] 4.2 Parse appdata flags with the **low nibble as a node type enum** (`0` none, `1` chat, `2` repeater, `3` room server, `4` sensor) and the **high nibble as bit flags** (`0x10`/`0x20`/`0x40`/`0x80`) — see `AdvertDataHelpers.cpp`
- [x] 4.3 Decode optional appdata fields in wire order: lat and lon as 4-byte LE signed integers scaled by 1,000,000, then 2-byte feature 1, then 2-byte feature 2, then the name as the remainder
- [x] 4.4 Handle a name that is not NUL-terminated, is absent, or is invalid UTF-8 — the last exposing raw bytes plus a flagged replacement rendering rather than failing
- [x] 4.5 Reject appdata truncated before a field its flags declare
- [x] 4.6 Build ADVERT payloads and appdata, enforcing the 32-byte `MAX_ADVERT_DATA_SIZE` limit
- [x] 4.7 Unit tests for every advert scenario, using real appdata bytes lifted from the corpus (`0x92`, `0x93`) plus synthetic cases for the flag combinations the corpus lacks

## 5. Payload codec — plaintext bodies (`payload-codec`)

- [x] 5.1 Parse and build the text-message body: 4-byte LE timestamp, `txt_type` in the upper six bits and attempt in the lower two, text as the remainder with trailing zero padding stripped by the body parser (design D6)
- [x] 5.2 Handle `txt_type` 0 (plain), 1 (CLI command) and 2 (signed — first four bytes are the sender pubkey prefix)
- [x] 5.3 Parse the GRP_TXT body in the text-message layout, splitting on the first `": "` into sender name and body, with the sender name typed as unauthenticated; no split when the separator is absent
- [x] 5.4 Parse and build the returned-path body: path length byte using the **same packed hop-count/hash-size encoding as the packet header** (`Mesh.cpp:167-168`, not the plain byte count `payloads.md` documents), the path, a 1-byte extra payload type read from its low nibble, and the bundled extra preserved verbatim with its padding; handle the no-extra case
- [x] 5.5 Parse and build the room-server login body: 4-byte LE timestamp, 4-byte LE sync timestamp, password as the remainder with padding stripped; allow an empty password
- [x] 5.6 Unit tests for each body layout, including the padding-strip cases and a group message with no name separator

## 6. Cryptography (`mesh-crypto`)

- [x] 6.1 Implement identity: Ed25519 keypair type, node hash as the public key's first byte, and generation that rejects a hash colliding with a supplied set, failing cleanly after a bounded number of attempts
- [x] 6.2 Implement shared secret derivation: Ed25519→X25519 public key conversion via PyNaCl, scalar multiplication using MeshCore's pre-clamped private scalar (no re-hash/re-clamp), returning 32 bytes
- [x] 6.3 Add a per-(local entity, peer) shared secret cache (DESIGN.md §5)
- [x] 6.4 Implement AES-128-ECB encrypt/decrypt keyed on the **first 16 bytes** of the secret, zero-padding plaintext to a 16-byte boundary, returning the padded buffer verbatim on decrypt
- [x] 6.5 Implement the cipher MAC: HMAC-SHA256 over the **ciphertext**, keyed on the **full 32-byte secret**, truncated to 2 bytes, compared with `hmac.compare_digest`
- [x] 6.6 Expose MAC verification as a candidate-match value whose type name and docstring state that it is not proof of identity (design D7)
- [x] 6.7 Implement channel keys: 16- or 32-byte pre-shared keys, hashtag derivation as `sha256(b"#name")[:16]` flagged as brute-forceable, and channel hash as `sha256(key)[0]`
- [x] 6.8 Implement advert signing and verification over `pubkey ‖ LE timestamp ‖ appdata` (`Mesh.cpp::createAdvert`), returning a verification-result value that is the only route to parsed advert content, and carrying the advert timestamp for replay comparison upstream
- [x] 6.9 Implement the acknowledgement checksum as **the first 4 bytes of SHA-256** over `(4-byte timestamp ‖ txt_type/attempt byte ‖ text) ‖ sender public key` — **not CRC32**; see `BaseChatMesh.cpp:243`
- [x] 6.10 Write fixed known-answer vectors for shared-secret derivation, AES-128-ECB, the truncated HMAC, the channel hash and the ACK checksum, each citing the firmware source line it was derived from (design risk: KAT provenance)
- [x] 6.11 Unit tests for every `mesh-crypto` scenario, including the wrong-key MAC failure, the exact-block-multiple padding case, tampered-advert verification failure, and the all-256-hashes-taken generation failure

## 7. Regression corpus (`protocol-corpus`)

- [x] 7.1 Build the corpus loader: read `rx_frame` records read-only from both capture files, never writing to them or their `.meta.json` sidecars
- [x] 7.2 Assert every one of the 351 frames decodes, failing with capture file, record index and raw hex on any failure
- [x] 7.3 Assert byte-identical re-encoding of every corpus frame
- [x] 7.4 Assert the recorded distributions: payload types (TXT_MSG 108, GRP_TXT 91, ADVERT 53, ACK 42, ANON_REQ 16, PATH 15, RESPONSE 11, REQ 6, GRP_DATA 5, TRACE 4), route types, hop counts 0-5, hash sizes (1-byte 152, 2-byte 106, 3-byte 93)
- [x] 7.5 Assert all 53 corpus adverts pass Ed25519 signature verification, and that recovered names and node types match the recorded expectation
- [x] 7.6 Implement the golden-file renderer covering structural fields and ciphertext digests only — never decrypted plaintext — plus the generator script and the checked-in golden file (design D8, D10)
- [x] 7.7 Write `tests/protocol/CORPUS.md` recording what the corpus covers, and explicitly what it does not: no `TRANSPORT_*` route types, no MULTIPART/CONTROL/RAW_CUSTOM, no feature-1/feature-2 adverts, and **no verification of decryption**, since every encrypted frame in it is addressed to a third party
- [x] 7.8 Spot-check the generated expectations against the independent analysis in the proposal (advert names, type counts) before committing them as fixtures (design D10)

## 8. DESIGN.md corrections (done — applied ahead of implementation)

- [x] 8.1 Correct §4.2: replace the `path_length > 64` rejection rule with the packed encoding — hop count in bits 0-5, hash size in bits 6-7 plus one, effective extent `hop_count * hash_size` ≤ 64 — and record that 2- and 3-byte hashes are observed live in the corpus
- [x] 8.2 Correct §5: the ACK value is the first 4 bytes of SHA-256 over the message body prefix concatenated with the sender's public key, not a CRC32; keep the "delivery evidence, never authentication" conclusion, which the correction only strengthens
- [x] 8.3 Clarify §5 and §3: the advert appdata flags byte is a node-type enum in its low nibble plus bit flags in its high nibble, so `0x03` is "room server" rather than a combination
- [x] 8.4 Clarify §5: the cipher key is the first 16 bytes of the shared secret while the HMAC key is the full 32 bytes
- [x] 8.5 Add a line to §12's milestone 4 entry making "first successful decrypt of a real MeshCore DM" its explicit exit criterion, since milestone 1 cannot verify decryption

## 9. Verification and close-out

- [x] 9.1 Run the full suite (`uv run pytest`) and confirm the milestone 0 radio tests still pass unchanged
- [x] 9.2 Confirm the import-boundary test from 1.3 passes against the finished modules
- [x] 9.3 Compute and record the corpus's flood repetition rate as input to the milestone 3 dedup cache sizing question (DESIGN.md §13 unknown 2, design open question 3)
- [x] 9.4 Record in the change any frame or field the corpus contained that this milestone could not fully explain, so milestone 2 has a watch list
