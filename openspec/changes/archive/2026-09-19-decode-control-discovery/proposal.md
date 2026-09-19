# Proposal

## Why

The live web feed shows DIRECT, zero-hop CONTROL frames as `uninterpreted_payload`, with the
detail column blank. On 2026-09-18 these arrived in pairs: an 8-byte frame, then a 40-byte one
about 3 s later. They are MeshCore **node discovery**: `NODE_DISCOVER_REQ` (control subtype
`0x80`, 6-byte payload) sent by a client looking for nearby repeaters, and `NODE_DISCOVER_RESP`
(subtype `0x90`, 38-byte payload) from a repeater answering within its randomised ×4 reply delay
(`examples/simple_repeater/MyMesh.cpp::onControlDataRecv`).

This traffic is common, not rare. All 168 CONTROL frames in the regression corpus are discovery
frames (12 requests, 156 responses, all DIRECT zero-hop), about a sixth of the corpus. Showing
them as uninterpreted hides three things an operator can use: which repeaters answer near this
station, how well each one heard the request (the SNR it reports back), and which request each
response answers (the tag that pairs them). The format is small, fixed and implemented on both
sides in the reference repos, so decoding it is cheap and low-risk.

## What Changes

- The payload codec parses CONTROL payloads whose subtype is `NODE_DISCOVER_REQ` or
  `NODE_DISCOVER_RESP` into structured payloads:
  - request: prefix-only flag, node-type filter, tag, optional `since`
  - response: responder node type, the SNR it reports for the request, tag, and the claimed
    public key (full 32 bytes or an 8-byte prefix)
- The codec builds both payloads back to their exact wire bytes, so the corpus round-trip covers
  them.
- A discovery-shaped CONTROL payload with a length the firmware never sends becomes a payload
  failure naming the length, the same policy as ACK.
- Any other CONTROL subtype, and an empty CONTROL payload, is still preserved uninterpreted.
  MULTIPART, RAW_CUSTOM and the reserved types are unchanged.
- RX decode reports discovery frames with their own outcomes, `discover_request` and
  `discover_response`, in place of `uninterpreted_payload`. The *Packet RX* wide event carries
  the tag, the filter or node type, and for responses the reported SNR and the claimed key
  prefix.
- `sighop monitor` renders a detail line for each: the tag, the filter or node type, the
  reported SNR, and the claimed key marked as unauthenticated.
- The web feed's detail column shows a one-line summary for live discovery frames. The claimed
  key is marked as unauthenticated.
- The responder's public key is **not authenticated**: the response carries no signature.
  Nothing feeds it into contacts, path learning or any identity table. Every view draws it as a
  claim, not as a verified identity.
- **BREAKING** (observability only): the `packet_rx` wide event's and the packet log's
  `outcome` for these frames changes from `uninterpreted_payload` to `discover_request` or
  `discover_response`. Anything filtering logs on the old value for CONTROL frames will stop
  matching.
- Corpus golden output changes for the 168 CONTROL lines, from `unparsed` to structured
  discovery lines. The change is reviewed, not accepted blind.

Non-goals: sighop does not answer discovery requests (it hosts room servers and companions, and
MeshCore's room server firmware does not answer them either). It does not send discovery
requests. It does not keep a neighbour table built from responses. It does not match a claimed
key to known contact names.

## Capabilities

### New Capabilities

None. Discovery decoding belongs in the existing codec, decode, rendering and feed capabilities.

### Modified Capabilities
- `payload-codec`: new requirement for parsing and building node-discovery CONTROL payloads.
  The "unsupported payload types are preserved" requirement narrows to the CONTROL subtypes not
  interpreted. Payload building adds the discovery payloads.
- `rx-decode`: new requirement that discovery frames produce their own outcomes, with the
  claimed responder key carried as unauthenticated. The wide event carries the discovery fields.
- `monitor-cli`: per-frame rendering adds discovery detail lines.
- `web-dashboard`: the live feed shows a discovery summary in its detail column. The claimed key
  is drawn as unauthenticated.
- `protocol-corpus`: the corpus assertion for CONTROL changes from "uninterpreted bytes
  preserved" to "decoded as discovery, round-trips byte-identically".

## Impact

- `src/sighop/protocol/payloads.py`: new `DiscoverRequest` and `DiscoverResponse` types, which
  join `ParsedPayload`, plus the parse and build functions and the dispatch in `parse_payload`
  and `build_payload`. `src/sighop/protocol/__init__.py` exports them.
- `src/sighop/net/rx.py`: new cases in `outcome_fields`, so these frames no longer reach the
  default `parsed`/`no_key_held` case.
- `src/sighop/monitor/render.py`: `_render_payload` gets new cases, which its
  `AssertionError` fallback requires.
- `src/sighop/web/serialize.py`: feed row gains a `summary` field.
- `src/sighop/web/static/feed.js`: the detail cell falls back to `summary`.
- `tests/protocol/golden.py` and `corpus_golden.txt` (168 lines), `test_corpus.py`,
  `CORPUS.md`, `test_payloads.py`, `test_rx.py`, `test_render.py`, and the web serialisation
  tests.
- No database migration. The packet log already stores `outcome` as free text. The summary is
  not persisted (see design).
- No new dependencies. No change to TX, dedup, contacts or path learning.
