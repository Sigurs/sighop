# Design

## Context

- `parse_payload` (`src/sighop/protocol/payloads.py`) sends CONTROL to the fallback
  `UnparsedPayload`. `net/rx.py::_payload_outcome` turns that into `Uninterpreted`, and
  `outcome_fields` reports it as `uninterpreted_payload`.
- Every other `ParsedPayload` member falls into the default `outcome_fields` case, which writes
  `parsed` + `decrypt_outcome=no_key_held`. A discovery payload has no key to hold, so that
  case would be wrong for it.
- The web feed's detail cell (`static/feed.js`) shows only `reason`, which is a failure
  explanation, and falls back to `raw` for undecodable frames. No successfully decoded frame
  shows detail today. The packet log persists `outcome` and `reason`, and keeps `raw` only for
  failures. History replay builds rows from those columns.
- Wire format sources: `related-repos/MeshCore/examples/simple_repeater/MyMesh.cpp`
  (`onControlDataRecv`, `sendNodeDiscoverReq`), `examples/simple_sensor/SensorMesh.cpp`,
  `src/Mesh.cpp:70` (firmware acts on these only when they arrive DIRECT with zero hops), and
  `meshcore_py/reader.py` (SNR divided by 4; key shorter than 32 bytes treated as an 8-byte
  prefix).
- Corpus evidence: 168 CONTROL frames. 9 are 10-byte requests, 3 are 6-byte requests, 156 are
  38-byte responses, and every response has node type 2 (REPEATER). All are DIRECT, zero hops.
  Filters seen: `0x04`, `0x06`, `0x10`. No 14-byte (prefix-only) response has been captured.

## Goals / Non-Goals

**Goals:**
- Discovery frames become first-class parsed payloads that round-trip byte-for-byte.
- Every view says what the frame is. The claimed responder key is never presented as
  authenticated.

**Non-Goals:**
- Any stateful use of discovery traffic: pairing requests with responses in code, a neighbour
  table, correlating a claimed key with contacts. Pairing stays visual, through the shared tag.
- Any change to what the codec accepts for other CONTROL subtypes.
- Route policing. The codec parses discovery on any route type. Only firmware cares that it is
  DIRECT zero-hop.

## Decisions

**D1: Two payload types, `DiscoverRequest` and `DiscoverResponse`, as new members of
`ParsedPayload`, not a single `ControlPayload` with a subtype field.** Their fields share only
the tag. Separate types keep the match arms in `outcome_fields`, `_render_payload` and
`golden.py` exhaustive and typed. Alternative: add a `subtype` field to `UnparsedPayload`.
Rejected, because consumers would still have to parse raw bytes.

**D2: Preserve every bit of the control byte.** For a request, keep the low nibble as `flags`
and expose `prefix_only` as a property of bit 0. For a response, the low nibble is the node
type, stored as a raw `int` and shown as a `NodeType` when it is a known value. This follows
the advert node-type handling, which tolerates unknown values. Bits 1–3 of the request byte are
unused today. If they were dropped, a rebuild would stop being byte-identical. The request's
filter byte is likewise kept raw, with a helper that lists the `NodeType`s it selects.

**D3: Strict lengths: 6|10 for requests, 14|38 for responses. Anything else is a
`BAD_PAYLOAD_LENGTH` failure.** This matches `parse_ack`. The firmware is laxer: it accepts a
request of 6 bytes or more and ignores a partial `since`. But a length it never *sends* is more
useful surfaced as a visible payload failure than parsed with bytes left over, because a rebuild
could not reproduce those extra bytes. An empty CONTROL payload, and every subtype other than
`0x80`/`0x90`, stays `UnparsedPayload`, so unknown future subtypes are still preserved, not
failed.

**D4: `since` is `int | None`.** `None` marks the 6-byte form, so the rebuild picks the right
length, and a present `since=0` (the 10-byte form the repeater's own `sendNodeDiscoverReq` sends)
stays distinct from an absent one.

**D5: The SNR is stored as the signed raw byte (`snr_quarter_db`) and exposed as dB through a
property.** Storing raw keeps the rebuild exact. Only a view computes the float.

**D6: The claimed key is typed and named as a claim.** The field is `claimed_key: bytes`, with
`key_is_prefix` derived from its length. Nothing in the protocol layer exposes it as an
`Identity` or public key object. Every view takes its label, including the word
"unauthenticated", from one shared helper, so the monitor and the web cannot drift apart.
Alternative: withhold the key, as unverified adverts withhold their names. Rejected. The key
is the responder's main content and is harmless to show. The risk lies in *trusting* it, which
D7 covers.

**D7: Discovery never reaches identity state.** `dm.py`, `acks.py`, contacts and path learning
match on specific payload types, so a new type is invisible to them. Only the non-effect needs
a test: decoding a response whose claimed key is a known contact's changes nothing.

**D8: Outcome names `discover_request` / `discover_response` in `outcome_fields`.** These are
explicit arms placed before the default case, with the discovery fields added to the wide
event (`control_tag`, `discover_filter` or `discover_node_type`, `discover_snr_db`,
`discover_key_prefix` as 8 bytes of hex). Alternative: keep `parsed` and add fields. Rejected,
because the feed and packet log show only the outcome for a successful decode, so `parsed`
would say nothing more than `uninterpreted_payload` did. Adverts already have their own
outcome names.

**D9: The web detail uses a new feed field `summary`, live-only, not persisted.** `rx_record`
adds `summary`, built by a pure function from the record (`None` for anything that is not
discovery), and `feed.js` renders `record.reason || record.summary || raw`. The packet log
schema is unchanged. History rows keep the outcome name but have no summary. Alternatives:
- Put the summary in `reason`. Rejected: `reason` means "why the outcome is what it is" and is
  documented as failure context, so mixing the two would blur what that column means.
- Add a nullable `summary` column with a migration. Deferred: that is more than this
  observability fix needs, and the outcome name already answers "what is this frame".

**D10: Regenerate the corpus golden file and review the diff before committing.** Expected
diff: exactly 168 changed lines, all `type=CONTROL`, each changing from `unparsed len=N sha=…`
to a `discover_req …` or `discover_resp …` line. No other lines change. Any other diff line is
a regression. `EXPECTED_PAYLOAD_TYPES` does not change. `test_corpus.py` gains the
request/response form counts (6-byte 3, 10-byte 9, 38-byte 156).

## Risks / Trade-offs

- [A future firmware extends a discovery payload, for example a response carrying more than 38
  bytes] → That frame becomes `payload_failure`, logged at error level. This is visible, not
  silent, and fixing it means adding one length. Accepted in exchange for exact round-trip.
- [Log queries or dashboards filter on `outcome=uninterpreted_payload` to find CONTROL] → The
  proposal flags this as BREAKING (observability only). No in-repo consumer matches that string
  apart from the test being updated.
- [An operator reads a claimed key as proof that a repeater is present] → Every rendering says
  "unauthenticated". The SNR is also a claim by the responder, so it is labelled "reported", not
  "measured".
- [History and live rows differ, since only live rows have a summary] → The spec states this.
  A migration can close the gap later without changing the outcome vocabulary.
- [`golden.py` and `_render_payload` end in `AssertionError` for unhandled types] → Adding the
  arms is required. The corpus test fails loudly if one is missed.

## Migration Plan

This is a code-only deploy with no schema change. Packet log rows recorded before the deploy
keep `uninterpreted_payload`, and new rows get the discovery outcomes. Rollback means reverting
the commit. Rows written in the meantime keep the new outcome strings, which the old code
displays as text without problems.
