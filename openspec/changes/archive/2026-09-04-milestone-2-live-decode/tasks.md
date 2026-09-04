## 1. Modem request/response plumbing (`modem-rx`, `modem-probe`)

- [x] 1.1 Add the `SetHardware` sub-command and response-code constants this milestone uses (`GetRadio` `0x0B`/`0x8B`, `GetTxPower` `0x0C`/`0x8C`, `GetVersion` `0x11`/`0x91`, `GetBattery` `0x13`/`0x93`, `GetMCUTemp` `0x14`/`0x94`, `GetSensors` `0x15`/`0x95`, `GetDeviceName` `0x16`/`0x96`) and the `Error` sub-codes `UnknownCmd` `0x05` and `NoCallback` `0x03`, citing `docs/kiss_modem_protocol.md`
- [x] 1.2 Add a single pending-request slot to `Modem` and a `request(sub_command, data, timeout)` API resolving from the one frame loop (design D2); raise on a second concurrent request rather than queueing
- [x] 1.3 Route matching `SetHardware` responses and `Error` frames to the pending request; resolve unanswered requests as a timeout failure without aborting the frame loop
- [x] 1.4 **Preserve `Data`/`RxMeta` correlation across a routed response** (design D3): a `SetHardware` response that resolves a request must not flush a pending `Data` frame; only genuinely unrecognized frames still do
- [x] 1.5 Report a `SetHardware` response matching no outstanding request as an unparsed-frame event, as before
- [x] 1.6 Refactor `_handshake` to use the request API, keeping its existing `SetRadio` OK/Error behaviour and its single-reader property
- [x] 1.7 Tests: request resolves; request rejected with an error code; request times out; second concurrent request raises; unsolicited response reported unparsed; **response interleaved between a `Data` frame and its `RxMeta` leaves the SNR/RSSI attached**
- [x] 1.8 Confirm the existing `tests/test_modem.py` scenarios still pass unchanged — they encode milestone 0's correlation invariants

## 2. Startup probe (`modem-probe`)

- [x] 2.1 Create `src/sighop/radio/probe.py` with a `ProbeResult` holding, per sub-command, either a parsed value or a structured absence carrying its reason (`unsupported` with the modem's error code, or `timeout`) — design D4
- [x] 2.2 Probe `GetDeviceName` (UTF-8, may be absent or non-UTF-8), `GetRadio` (freq 4 + BW 4 + SF 1 + CR 1, little-endian), `GetTxPower` (signed dBm) and `GetVersion` (version byte + reserved byte)
- [x] 2.3 Probe the optional telemetry sub-commands: `GetBattery` (millivolts, 2 bytes), `GetMCUTemp` (signed, 2 bytes) and `GetSensors` sent with permissions `0x07`, whose CayenneLPP response is recorded as **raw bytes, uninterpreted** (design D4)
- [x] 2.4 Make every probe failure non-fatal: a modem that answers nothing still reaches the ready state and receives frames normally
- [x] 2.5 Compare the `GetRadio` readback against the applied `SetRadio` parameters and emit an error-level wide event naming both on mismatch (design D5); record the readback as the observed parameters
- [x] 2.6 Run the probe as part of the startup handshake and re-run it after every reconnect; emit one wide event carrying the whole probe result
- [x] 2.7 Assert in a test that no code path in `radio/` sends `Data`, `SetTxPower` or `Reboot` — the receive-only property, checked rather than asserted in prose
- [x] 2.8 Tests: full probe success; per-sub-command `UnknownCmd`; per-sub-command timeout; every probe failing; radio readback mismatch; non-UTF-8 device name

## 3. Capture provenance header (`capture-cli`)

- [x] 3.1 Build the `capture_meta` record from the `ProbeResult` plus the sighop version and commit hash, with unanswered fields written as explicit nulls **with their reason** (design D6)
- [x] 3.2 Record the read-back radio parameters as the observed values and the configured parameters separately, so a disagreement is visible in the file
- [x] 3.3 Write the header as the first line of a newly created capture file, ahead of any frame record; write no header when appending to a file that already has records
- [x] 3.4 Wire the probe result into `CaptureRun` by injection rather than having it probe the modem itself, so it stays testable without a device
- [x] 3.5 Tests: header shape and field presence; absent fields carry null plus reason; header precedes a frame that arrived during probing; appending to a non-empty file adds no second header

## 4. Replay source (`capture-replay`)

- [x] 4.1 Create `src/sighop/radio/replay.py` producing the same `RxEvent`/`UnparsedEvent` types the modem produces, in file order (design D1)
- [x] 4.2 Re-hydrate `rx_frame` records with their packet bytes and RxMeta, and `unparsed` records with their raw bytes and reason; a null RxMeta must yield an event with no signal values, not zeros
- [x] 4.3 Consume a leading `capture_meta` record as the file's provenance and expose it to the caller without decoding it as a frame; report provenance as absent for the two pre-existing capture files
- [x] 4.4 Report unreadable lines — invalid JSON, unknown `kind`, missing fields, a truncated trailing line — identifying the line number, rather than skipping them
- [x] 4.5 Attribute each event the timestamp recorded in its record, and replay without pacing (design D10)
- [x] 4.6 Tests: round-trip a `CaptureRun` output back through replay and compare events; headerless file; truncated final line; unknown `kind`; null RxMeta

## 5. RX decode pipeline (`rx-decode`)

- [x] 5.1 Create `src/sighop/net/` with `__init__.py` and `rx.py`, consuming an async iterable of modem events and knowing nothing about the source (design D1)
- [x] 5.2 Define the decoded-record type: reception id, timestamp, raw bytes, header fields, route type, hop count, path, hash size, SNR/RSSI as optional values, and the payload outcome
- [x] 5.3 Mint the reception id per frame at ingress and thread it through the record and every derived log event; document that it identifies the reception, **not** the packet content (design D7)
- [x] 5.4 Compose `decode_packet` then `parse_payload`; carry a structural failure, a payload failure on a structurally valid packet, and an uninterpreted payload type as three distinct outcomes
- [x] 5.5 Verify ADVERT signatures via `verify_advert` and carry the verification result in the record such that advert content is unreachable without it (design D8, milestone 1 D7)
- [x] 5.6 Treat an encrypted payload with no key held as a normal outcome carrying the envelope fields, never as a failure
- [x] 5.7 Forward modem unparsed-frame events as their own outcome carrying the raw bytes and stated reason
- [x] 5.8 Emit the DESIGN.md §9 *Packet RX* wide event once per frame — including for frames that fail to decode — with reception id, route type, payload type, hop count, path, size, SNR, RSSI, source hash and outcome; omit `dup`, `matched_entities` and `airtime_ms`, which have no meaning until milestones 3 and 4
- [x] 5.9 Keep the stage stateless: no dedup, no path learning, no contact accumulation, no persistence
- [x] 5.10 Tests for every `rx-decode` scenario, driven from the replay source rather than from hand-built events where a real frame exists

## 6. Monitor rendering (`monitor-cli`)

- [x] 6.1 Create `src/sighop/monitor/` with `render.py` holding pure functions from a decoded record to text — no I/O, no event loop (design D8)
- [x] 6.2 Render the per-frame line: timestamp, payload type, route type, hop count, path, SNR, RSSI, in fixed-width columns
- [x] 6.3 Render the detail line per payload kind: verified advert (mark, name, node type, flags); encrypted envelope (dest/src hash, MAC, ciphertext length, "not decrypted"); ACK; TRACE; uninterpreted payload
- [x] 6.4 **Never render the name or content of an advert whose signature failed** — show the failure and the public key instead (design D8, DESIGN.md §5 and §8)
- [x] 6.5 Render a decode failure line carrying the violated rule, the offset and the raw bytes
- [x] 6.6 Render the startup line from the probe result: device name, firmware version, confirmed radio parameters, TX power, each unanswered field shown as unknown; state a radio readback mismatch prominently
- [x] 6.7 Render the summary line: frames received, decode failures, adverts verified, adverts failed, distinct node hashes heard, reconnects
- [x] 6.8 Tests: string comparison over each render function, including the failed-advert case asserting the claimed name does **not** appear in the output

## 7. Monitor command (`monitor-cli`)

- [x] 7.1 Create `src/sighop/monitor/run.py` with `MonitorRun`: drives a source through the pipeline, renders, counts, and emits the periodic summary
- [x] 7.2 Add the `sighop monitor` subcommand with `--device` / `--replay` as mutually exclusive and required-one-of, plus `--radio-preset`, `--log-file` and `--capture`
- [x] 7.3 Live mode: open the modem, probe, print the startup line, then render each frame; install `SIGINT`/`SIGTERM` handlers that print a final summary and exit 0
- [x] 7.4 Replay mode: open no device, use the file's provenance for the startup line (or state it as absent), render every frame, print a final summary and exit
- [x] 7.5 `--capture`: write the monitored frames to a capture file in `sighop capture`'s format, header included, reusing `CaptureRun` rather than a second writer
- [x] 7.6 Keep the wide events flowing to the log stream independently of the rendered stdout output
- [x] 7.7 Tests: argument validation (neither source, both sources); end-to-end replay of a small fixture file through CLI to rendered output; graceful shutdown

## 8. Corpus replay verification

- [x] 8.1 Replay both milestone 0 capture files end-to-end through the live pipeline in the test suite, asserting every frame produces an outcome and the aggregate decode-failure count is zero
- [x] 8.2 Cross-check the replayed type/route/hop distribution against the counts `tests/protocol/` already asserts, so the pipeline and the offline corpus harness cannot silently diverge
- [x] 8.3 Assert the pipeline's advert verification results match the corpus harness's — same adverts verified, same names
- [x] 8.4 Keep the offline `tests/protocol/` corpus harness as it is; this is a second, higher-level check over the same files, not a replacement for it

## 9. Live run — the actual exit criterion

- [x] 9.1 Run `sighop monitor --device <by-id path> --capture captures/<date>.jsonl` against the live mesh for an evening, and confirm adverts render in real time with correct names, node types, paths and SNR
- [x] 9.2 Confirm the capture file carries a proper `capture_meta` header, and record the probe answers for the V3 — including the `GetSensors` shape, which DESIGN.md §4.1 leaves open
- [x] 9.3 Record what the run found: any frame the decoder rejected, any payload type or route type the corpus lacks, and the reconnect count
- [x] 9.4 Answer milestone 1's carried-over question where the data allows — whether any repeater on this mesh mixes path hash sizes across a packet's lifetime — and write the answer (or "still unanswered") into this change's design
- [x] 9.5 Append the session's capture to the regression corpus if it contains frame shapes the existing corpus lacks, with its sidecar provenance — all three 2026-09-04 files appended (91 frames, corpus now 442); they brought the first live `TRANSPORT_FLOOD` frame and the first CONTROL payloads. Provenance is the in-band `capture_meta` header, not a sidecar; the harness now requires one or the other per file

## 10. Documentation and design corrections

- [x] 10.1 Update DESIGN.md §11 to include `net/rx.py` and `monitor/`, which the repository-layout sketch currently omits
- [x] 10.2 Update DESIGN.md §12's milestone 2 entry to record what was done and the `capture_meta` header now being written by both `capture` and `monitor`
- [x] 10.3 Fold any wire-format or hardware finding from task 9.3 into DESIGN.md in this change, per its own rule that reality disagreeing with it is fixed in the same change
- [x] 10.4 Update `tests/protocol/CORPUS.md` if the corpus gained frames

## 11. Verification

- [x] 11.1 Full test suite green, including the unchanged milestone 0 and milestone 1 tests
- [x] 11.2 `tests/protocol/test_import_boundary.py` still passes — the dependency runs one way, `net/` down to `protocol/`
- [x] 11.3 Lint and typecheck clean
- [x] 11.4 `openspec validate milestone-2-live-decode --strict` passes and every task above is checked
