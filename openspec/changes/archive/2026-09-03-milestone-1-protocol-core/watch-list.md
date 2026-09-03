# Watch list for milestone 2

Fields and frames the corpus contained that milestone 1 decoded structurally but
could not fully explain. Milestone 2's live decode is where these would show
their meaning; recorded here so they are looked at deliberately rather than
rediscovered.

## 1. TRACE payload contents (4 frames)

Preserved whole and identified, never interpreted — an explicit scope decision,
but the shape is worth noting. Observed payload lengths are 13 bytes (×1) and 21
bytes (×3), which is consistent with a fixed header plus one byte of SNR per hop,
but the corpus has too few samples and no matching outbound trace to confirm the
layout. Milestone 2 should decode a trace it originated itself, where the hop
count is known.

## 2. The ACK 6-byte tail's first byte

Current firmware builds `ack_hash[6]` as the 4-byte checksum, then
`ack_hash[4] = data[5 + text_len + 1]` — a byte read *past* the NUL terminator of
the message text, described as an "extended attempt byte" — and
`ack_hash[5] = random` (`BaseChatMesh.cpp:245-247`).

Of the 11 six-byte ACKs in the corpus, 10 carry `0x00` in that position and one
carries `0x0d`. Whether the non-zero case is a genuine extended attempt or a read
of block padding is not determinable from ACKs alone, since the corpus holds none
of the plaintexts they acknowledge. sighop currently preserves the tail without
interpreting it, and matches acknowledgements on the leading 4 bytes as the
firmware does. Milestone 4's own message exchange settles this.

## 3. `feature 1` and `feature 2` advert appdata

No corpus advert sets `0x20` or `0x40`, so the two 2-byte fields are decoded
positionally and preserved uninterpreted. Their meaning is reserved-for-future-use
in the firmware too. Open question carried forward from the design.

## 4. Whether repeaters mix path hash sizes

A frame carries one hash size code, so the corpus cannot show whether a packet's
path is appended to with a different size as it crosses the mesh. All three sizes
are live (152 / 106 / 93 frames), so the question is real. This is a milestone 3
path-learning concern; this layer records the hash size, which is what milestone 3
needs to detect it.

## 5. Transport codes

No `ROUTE_TYPE_TRANSPORT_*` frame appears in the corpus, so the 4-byte transport
code block is implemented from the format documentation and exercised only by
synthetic fixtures. The values are preserved uninterpreted; `transport_code_1` is
documented as derived from region scope and `transport_code_2` as reserved.
Milestone 2 should flag the first live one it sees.

## 6. Every ciphertext in the corpus

Not a defect, but the standing gap: all encrypted payloads are addressed to third
parties. Observed ciphertext lengths are all positive multiples of 16 across every
envelope type, which is consistent with the cipher and is the only thing the
corpus can say about them. Nothing in this milestone demonstrates that sighop
decrypts the way MeshCore does — see `tests/protocol/CORPUS.md`.
