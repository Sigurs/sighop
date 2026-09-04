## 1. Time on air (`airtime`)

- [x] 1.1 Create `src/sighop/net/airtime.py` with a pure `time_on_air(payload_len, params) -> float` over the Semtech formula, taking the SF/BW/CR from a `RadioParams` value rather than from configuration (design D1)
- [x] 1.2 Use the firmware's SF-dependent preamble — 32 symbols at SF ≤ 8, 16 above (`RadioLibWrappers.h:56`) — explicit header, CRC on, and low data-rate optimisation exactly when symbol time ≥ 16 ms
- [x] 1.3 Tests: hand-computed known answers at the default preset (64 B ≈ 0.74 s, 255 B ≈ 2.31 s); the preamble step between SF8 and SF9; the LDRO threshold crossing at BW 62.5 kHz between SF9 and SF10; monotonicity in payload length
- [x] 1.4 Add the `GetAirtime` (`0x0F`/`0x8F`) sub-command to the probe surface, taking a packet length and parsing the 4-byte little-endian milliseconds answer, recorded as unavailable when rejected
- [x] 1.5 Cross-check at startup and after each reconnect over the ladder 16/64/128/255 B; emit an error-level wide event naming both figures when disagreement exceeds 1 ms **and** 2%, and continue on our own value (design D2)
- [x] 1.6 Refuse to admit any transmission when no radio readback is available, rather than falling back to configured parameters
- [x] 1.7 Tests: cross-check agreement; cross-check disagreement emits the event and does not change the value used; `GetAirtime` unsupported is recorded and non-fatal

## 2. Shared RX state (`rx-dedup`, `path-learning`)

- [x] 2.1 Create `src/sighop/net/dedup.py`: key `blake2b(bytes([payload_type]) + payload, digest_size=16)`, path and transport codes excluded (design D10)
- [x] 2.2 Bound the cache by TTL (default 300 s) and by entry count with LRU eviction (default 4096), both configurable (design D11)
- [x] 2.3 Pass through every record with no parsed payload without consulting or populating the cache — corrupt frames are never deduped
- [x] 2.4 Track and expose hit rate, occupancy against the cap, and the largest observed interval between a reception and the copy it matched, so the sizing is measured
- [x] 2.5 Tests: same payload over different paths is a duplicate; different payload types with identical bytes are not; TTL expiry makes a repeat a first reception; entry cap evicts LRU; two identical corrupt frames both pass through
- [x] 2.6 Create `src/sighop/net/paths.py`: learn the reversed path from flood receptions with reception time, hop count and SNR; zero-hop receptions record a zero-hop route (design D13)
- [x] 2.7 Key on the sender's public key where the payload carries one, else on the source hash with the entry marked ambiguous; a lookup for an unknown destination reports "unknown", never an empty path
- [x] 2.8 Resolve lookups most-recently-confirmed-wins, retaining all candidates; record SNR without scoring (§13 unknown #3 stays open)
- [x] 2.9 Bound the store by destination count with least-recently-updated eviction; hold it in memory only
- [x] 2.10 Tests: reverse path learned from a corpus flood advert; zero-hop entry distinct from unknown; ambiguous hash-keyed entry not merged when the key is later seen; most-recent wins between two routes

## 3. Virtual network bus (`net-bus`)

- [x] 3.1 Create `src/sighop/net/bus.py`: subscribe/unsubscribe, and publish that never awaits a subscriber (design D14)
- [x] 3.2 Give each subscriber a bounded queue; on overflow drop for that subscriber only and emit a wide event naming it, its depth and the dropped reception; count drops per subscriber
- [x] 3.3 Contain subscriber exceptions: log against the subscriber, keep it attached, leave others unaffected
- [x] 3.4 Define the TX submission API — packet, priority class, originating entity, deadline, optional originating `packet_id` — returning an awaitable handle resolving to transmitted / failed / suppressed / dropped
- [x] 3.5 Wire dedup and path learning between the decode stage and fan-out, leaving `net/rx.py` untouched and stateless (design D12)
- [x] 3.6 Tests: fan-out to three subscribers; one subscriber stalling does not block others or the pipeline; a raising subscriber stays attached; a duplicate is not fanned out; handle resolution for each of the four outcomes

## 4. Modem transmit path (`modem-tx`, `modem-rx`)

- [x] 4.1 Add the `TxDone` response code `0xF8` (one-byte result: `0x01` success, `0x00` failure) and the `TxBusy` error code `0x07` to the modem constants, citing `examples/kiss_modem/KissModem.h`
- [x] 4.2 Add `Modem.send_packet(bytes)` writing a `Data` frame, rejecting anything above 255 bytes before it reaches the transport
- [x] 4.3 Hold an asyncio lock across submit-and-await so at most one transmission is outstanding at the modem, independent of the scheduler (design D8)
- [x] 4.4 Route `TxDone` and busy errors to the outstanding transmission from the single frame loop; report either as an unparsed frame when nothing is outstanding
- [x] 4.5 Keep `Data`/`RxMeta` correlation and `SetHardware` request resolution working while a transmission is outstanding — reception must not stall on an awaited `TxDone`
- [x] 4.6 Set the completion timeout longer than the firmware's own (`getEstAirtimeFor(len) × KISS_TX_TIMEOUT_FACTOR`), computed from the `airtime` capability plus a CSMA allowance; resolve as timed out and release the invariant
- [x] 4.7 Resolve an outstanding transmission as failed on disconnect rather than waiting for a `TxDone` that cannot arrive
- [x] 4.8 Tests: success; failure result; busy; timeout; oversized rejection; concurrent submissions serialize; a frame received mid-transmission is still emitted with its RxMeta; unsolicited `TxDone` reported unparsed; disconnect resolves the outstanding submission

## 5. TX scheduler (`tx-scheduler`)

- [x] 5.1 Create `src/sighop/net/tx.py` with an injectable clock used for every deadline, window boundary and jitter draw (design D17)
- [x] 5.2 Implement the four priority classes with strict class ordering and round-robin by originating entity within a class
- [x] 5.3 Implement the rolling-hour sliding window of `(charged_at, airtime_ms)` with admission `window_sum + this_airtime ≤ ceiling`, ceiling defaulting to 10% (360 s/h) and enforced independently of any modem setting (design D4)
- [x] 5.4 Stall classes 2 and 3 at the reserve threshold (default 90% of ceiling) while classes 0 and 1 continue to the full ceiling (design D7)
- [x] 5.5 Charge airtime at hand-off, including a gate-suppressed hand-off, and never refund on failure or timeout (design D5)
- [x] 5.6 Implement the receive-only gate as the single choke point before `Modem.send_packet`, closed by default, resolving the handle as suppressed (design D6)
- [x] 5.7 Requeue at the head of the class on busy with a short delay and a bounded attempt count; drop and log when exhausted; never busy-loop
- [x] 5.8 Enforce per-submission deadlines — checked while queued and at dequeue — and per-class queue caps evicting the oldest member; log every drop with its reason and queue wait
- [x] 5.9 Emit the DESIGN.md §9 *Packet TX* wide event per attempt with the full field set and a result of success / failure / busy / suppressed / dropped, threading the originating `packet_id`
- [x] 5.10 Emit a standing startup warning and a per-status warning when the ceiling is configured above 10%
- [x] 5.11 Tests: class ordering; round-robin within a class; **no 3600 s interval exceeds the ceiling across a simulated 24 h of continuous submission**; reserve stalls 2 and 3 but not 0 and 1; the full ceiling stalls everything; failure keeps its charge; deadline expiry drops with the handle resolved; queue cap evicts oldest; busy requeues at head and gives up bounded
- [x] 5.12 Test that **with the gate closed no `Data` frame reaches the transport**, under load and across every class — the milestone's safety property, asserted rather than argued

## 6. Advert stubs and policy (`advert-policy`)

- [x] 6.1 Create `src/sighop/net/adverts.py` with in-memory entity stubs holding a generated Ed25519 keypair, name, node type and advert config, rejecting a keypair whose node hash collides with an existing stub (DESIGN.md §3, design D15)
- [x] 6.2 Produce real signed adverts through the existing `protocol/` advert path — no new crypto, and no import from `net/` into `protocol/`
- [x] 6.3 Implement the schedule: 24 h flood floor, zero-hop disabled by default, ±25% per-entity jitter, minimum inter-entity flood gap (default 10 min) deferring the later advert with a log line, and startup stagger across the interval
- [x] 6.4 Reject a configured interval below the floor outside an override
- [x] 6.5 Implement the override with mandatory expiry: default 1 h, maximum 24 h, auto-revert to the floor with a logged reversion, surfaced in every status line while active (DESIGN.md §13)
- [x] 6.6 Submit due adverts to the scheduler in class 3 with a deadline; never hand one to the modem by any other route; a dropped advert waits for its next scheduled time rather than retrying
- [x] 6.7 Tests: floor rejection; default zero-hop off; jitter differs per entity; inter-entity gap defers; startup emits nothing immediately and staggers; override expiry reverts; override beyond 24 h rejected; advert signature verifies through the existing verifier

## 7. Runtime and CLI (`runtime-cli`)

- [x] 7.1 Create `src/sighop/runtime.py` composing modem/replay source → decode → dedup → paths → bus → scheduler → advert stubs, with one shutdown path
- [x] 7.2 Add the `run` subcommand: device or `--replay` source, `--enable-transmit` (absent means gate closed), status interval, and the dedup/budget/advert knobs
- [x] 7.3 State the gate at startup and in every status line; when transmit is enabled, say so prominently together with the ceiling
- [x] 7.4 Add status-line formatting to `monitor/render.py` as pure functions: gate, rolling-hour usage against ceiling with the reserve state marked, queue depth by class, suppressed and dropped counts, dedup hit rate and occupancy, learned path count, active overrides
- [x] 7.5 Mark stub entities as ephemeral wherever they are rendered, and keep unverified content visually distinct from verified (DESIGN.md §8)
- [x] 7.6 On interrupt, stop the scheduler, drain its queues logging each packet as dropped with a shutdown reason, and emit a final summary
- [x] 7.7 Tests: `run --replay` over a corpus file end to end; status line rendering by string comparison; a run without the flag writes no `Data` frame; graceful stop drains and logs

## 8. Verification runs and DESIGN.md

- [x] 8.1 Replay the full 442-frame corpus through `run --replay` and record the dedup hit rate, occupancy and largest inter-copy interval
- [x] 8.2 Run `sighop run` against the live link (V4) overnight, receive-only, with an advert override active so the scheduler carries real load; record duty-cycle usage, suppressed counts, queue behaviour and any dropped packets — **DONE, over 2 h 54 min rather than a full night** (2026-09-04 17:06–20:00 UTC, `captures/2026-09-05.jsonl` + `.log`), which is long enough for what the task was waiting on: the rolling hour. Two stub entities under a 15-minute advert override. Recorded: 555 receptions, 0 decode failures, 0 unparsed frames, 17 adverts all verifying; dedup 33.2% with peak occupancy 49/4096 and a widest inter-copy gap of **200.7 s**; 16 destinations learned; 16 adverts charged and suppressed at the closed gate, 0 dropped, 0 busy, every one admitted first attempt, longest queue wait 0.46 ms; 16 inter-entity gap deferrals of 80–530 s. Budget remaining fell 99.70% → 98.22% over the first six adverts and then **held at 98.22%** — the sliding window ageing charges off at the rate new ones arrived, which is the property a short run cannot show. Peak load 6.40 s in any 3600 s = **0.178% duty against the 10% ceiling**. Findings written into DESIGN.md §12; the 200.7 s gap corrects §13 unknown #2 and the `DEFAULT_TTL_SECONDS` docstring, whose stated margin was ten times and is really 1.5×. Status lines and the shutdown summary go to stdout rather than the wide-event log, so this capture retains the events, not the terminal
- [x] 8.3 Set the final dedup TTL and entry-cap defaults from 8.1 and 8.2 rather than from the design's starting values, and record the measurement in DESIGN.md §13 unknown #2 as resolved
- [x] 8.4 Correct DESIGN.md §4.3's deaf-window table to the 32-symbol-preamble figures (64 B ≈ 0.74 s, 255 B ≈ 2.31 s) and note the cause (design D3)
- [x] 8.5 Restate DESIGN.md §4.3's "token bucket over a rolling window" as the sliding window implemented, so the regulatory claim and the code assert the same thing (design D4)
- [x] 8.6 Update DESIGN.md §11's layout with `net/airtime.py`, `net/dedup.py`, `net/paths.py`, `net/bus.py`, `net/tx.py`, `net/adverts.py` and `runtime.py`
- [x] 8.7 Add the milestone 3 findings to DESIGN.md §12, in the form milestone 2's entry uses — what the live run showed that no offline test could have
- [x] 8.8 Append the overnight capture to the corpus if it carries a shape the corpus lacks, whole rather than filtered, per DESIGN.md §12 — **DONE.** `captures/2026-09-05.jsonl` appended whole (555 frames, corpus now **997 across six files**), provenance in-band as its `capture_meta` header. It earned the append three times over: a **located CHAT advert** (flags `0x91`; every chat advert before it was `0x81`, so the location bit had never been seen on a non-repeater), a **10-byte TRACE** against 13 and 21 previously, and the duplicate-timing tail that re-sizes the dedup TTL. It also reclassified CONTROL from curiosity to routine traffic — 162 frames against six in the whole corpus before it. Zero decode failures, zero re-encode mismatches, all 17 adverts verifying; the golden file diff is **purely additive, 555 lines added and none changed**, so no existing frame was reinterpreted. Loader, recorded composition, `CORPUS.md` and the `protocol-corpus` spec delta updated to match
- [x] 8.9 Confirm the `protocol/` import-boundary test and every milestone 0–2 test still pass unchanged
- [x] 8.10 Correct DESIGN.md §4.3's claim that the EU narrow and legacy 250 kHz/SF11 presets "differ by more than an order of magnitude per packet" — computed from the firmware's own parameters they are within ~7% across the payload range, and legacy is marginally faster above 32 B (`tests/test_airtime.py`). The reason for reading parameters back rather than assuming them survives; the arithmetic offered for it does not
