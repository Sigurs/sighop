# Tasks

## 1. Payload codec

- [x] 1.1 Add `DiscoverRequest` (`flags`, `type_filter`, `tag`, `since: int | None`, with `prefix_only` and `selected_node_types` properties) and `DiscoverResponse` (`node_type: int`, `snr_quarter_db`, `tag`, `claimed_key`, with `snr_db` and `key_is_prefix` properties) to `src/sighop/protocol/payloads.py`, together with the `CTL_TYPE_NODE_DISCOVER_REQ/RESP` constants cited to `MyMesh.cpp`. Add both to `ParsedPayload` and export them from `src/sighop/protocol/__init__.py`. Verify with `uv run python -c "from sighop.protocol import DiscoverRequest, DiscoverResponse"`
- [x] 1.2 Implement `parse_control` (called from `parse_payload` for CONTROL): discovery subtypes with strict lengths 6|10 and 14|38 (design D3), `BAD_PAYLOAD_LENGTH` naming the valid lengths otherwise, and `UnparsedPayload` for an empty payload or any other subtype. Verify with new `tests/protocol/test_payloads.py` cases for each payload-codec spec scenario: the 6-byte and 10-byte requests, the 38-byte and 14-byte responses, a 7-byte request and a 20-byte response failing, an unknown node-type nibble, and `de ad` staying unparsed
- [x] 1.3 Implement `build_discover_request`/`build_discover_response` and the `build_payload` arms. Reject a tag that is not 4 bytes and a claimed key that is neither 8 nor 32 bytes with `EncodeError`. Verify with round-trip tests covering reserved request flag bits 1–3 and `since=0` compared with `since=None`, plus the invalid-size build tests

## 2. RX decode

- [x] 2.1 Add `discover_request`/`discover_response` arms to `outcome_fields` in `src/sighop/net/rx.py`, before the default case, carrying `control_tag`, the filter/prefix-only flag or node type, `discover_snr_db` and `discover_key_prefix` (design D8), with no `decrypt_outcome`. Verify with `tests/test_rx.py` tests asserting the outcome and fields for a request and a response. Update `test_an_uninterpreted_payload_type_is_an_outcome_not_a_failure`, which uses `de ad` and should still pass unchanged
- [x] 2.2 Add one shared helper that produces the discovery summary text, with the claimed key labelled "unauthenticated" and the SNR labelled "reported" (design D6), returning `None` for any record that is not discovery. Verify with unit tests for both subtypes and a non-discovery record
- [x] 2.3 Prove D7: decode a discovery response whose claimed key equals a verified contact's key through the ingress pipeline, and assert that contacts, learned paths and signal records are unchanged. Verify with a new test in the contacts or ingress test module

## 3. Views

- [x] 3.1 Add discovery arms to `_render_payload` in `src/sighop/monitor/render.py` using the shared helper, never using `VERIFIED_MARK`. Verify with `tests/test_render.py` cases for the monitor-cli spec scenarios (filter `0x06` names CHAT and REPEATER; the response line shows the tag, REPEATER, SNR in dB and "unauthenticated")
- [x] 3.2 Add `summary` to `rx_record` in `src/sighop/web/serialize.py` and change the detail cell in `static/feed.js` to `record.reason || record.summary || (record.raw ? "raw " + record.raw : "")`. Verify with a `tests/test_web_feed.py` case asserting that a discovery row has `outcome == "discover_response"` and a summary containing the tag and "unauthenticated", and that a history row built from the packet log shows the discovery outcome with `summary` absent
- [x] 3.3 Check the live view by running the app (`/run`) and replaying a capture containing CONTROL (for example `captures/2026-09-05.jsonl`) into the web feed. Confirm that the rows show `discover_*` outcomes and summaries with matching tags on request/response pairs, instead of `uninterpreted_payload` with a blank detail

## 4. Corpus

- [x] 4.1 Add `DiscoverRequest`/`DiscoverResponse` arms to `tests/protocol/golden.py`, regenerate `corpus_golden.txt` with `generate_golden.py`, and review the diff: it must change exactly 168 lines, all `type=CONTROL`, and nothing else (design D10). Verify with `git diff --stat tests/protocol/corpus_golden.txt` and `git diff tests/protocol/corpus_golden.txt | grep '^[-+]' | grep -vc CONTROL` giving only the two diff header lines
- [x] 4.2 Add a corpus test asserting that every CONTROL frame decodes as discovery (3 six-byte requests, 9 ten-byte requests, 156 thirty-eight-byte responses, 0 uninterpreted) and rebuilds byte-identically. Verify with `uv run pytest tests/protocol`
- [x] 4.3 Update `tests/protocol/CORPUS.md`: CONTROL is now decoded as discovery, and the gap list gains the 14-byte prefix-only response and non-discovery CONTROL subtypes, covered by the synthetic fixtures from 1.2. Verify by reading the updated section against the protocol-corpus delta spec

## 5. Wrap-up

- [x] 5.1 Run the full suite and linters (`uv run pytest`, and the project's lint/type-check commands) and confirm they are green
- [x] 5.2 Run `openspec validate decode-control-discovery --strict` and confirm it passes
