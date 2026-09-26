## Context

Two halves exist and have never been connected. `radio/` (milestone 0) turns a serial link
into a stream of `RxEvent`/`UnparsedEvent`; `protocol/` (milestone 1) turns bytes into packets,
payloads and verified adverts. Milestone 2 is the wire between them, plus the smallest possible
consumer that makes the result visible to a human.

Three constraints shape the decisions below.

1. **The proof has to be live.** An offline harness over the same 351 frames that produced the
   decoder cannot tell us the composition works — it shares every assumption with the code under
   test. Real air is the only thing that can surprise us, which is why DESIGN.md §12 calls this
   the milestone where "the design is proven or isn't".
2. **The link is a single point of failure with no side channel** (DESIGN.md §4.1). The KISS
   firmware is silent by construction; sighop's own logging is the only observability into the
   radio layer. That rule now extends upward: a frame that decodes to nothing must still produce
   an event.
3. **The modem's frame stream has exactly one reader.** Milestone 0 learned this concretely —
   `Modem._handshake` reads from the same iterator `events()` uses, because a second call to
   `transport.frames()` opens an independent generator racing it for the same bytes. Any
   request/response mechanism added here must live inside that single loop.

### What the milestone 1 corpus could not settle

| Question | Why offline cannot answer it |
|---|---|
| Do repeaters mix path hash sizes across a packet's lifetime? | A single frame carries one size code; only watching the same packet arrive by different routes shows it |
| Does live traffic contain shapes the corpus lacks? | The corpus is one location on two nights: no transport-coded packets, no CONTROL, no RAW_CUSTOM |
| Does the configured preset match what the board is actually running? | Nothing in a capture file records the hardware readback — the existing two captures state theirs as explicitly reconstructed, not observed |

The third is why the probe belongs in this change rather than a later one: an earlier version of
`EU868_NARROW` was wrong (869.525/SF7/CR5, guessed rather than checked) and the only symptom was
silence. A readback comparison turns that failure mode into a startup log line.

## Goals / Non-Goals

**Goals:**

- Decoded packets rendered live off the air, one line per reception, with adverts named and
  verified.
- One decode path, exercised identically by the live link and by replayed capture files.
- Every frame accounted for: decoded, or reported as a structured failure. No silent drops at
  any stage.
- The board describes itself at startup, and new captures carry that description as provenance.
- Receive-only preserved, and stated precisely enough to audit: no code path in this change
  sends `Data`, `SetTxPower` or `Reboot`.

**Non-Goals:**

- Dedup, path learning, the bus, the TX scheduler, time-on-air and airtime budgeting —
  milestone 3. The monitor deliberately shows raw reception counts, including duplicates, so
  that milestone 3's dedup has an unfiltered baseline to be measured against.
- Persistence of anything. The contact view is in-memory and dies with the process (milestone 5).
- Interpreting the `GetSensors` CayenneLPP payload, or rendering TX power as absolute dBm — §4.1
  warns against both without the device name beside them.
- A full-screen instrument panel. That is the WebUI's job at milestone 8; here, line-oriented
  output is greppable, pipe-friendly, dependency-free and testable by string comparison.

## Decisions

### D1. One pipeline, two sources — replay is the same code, not a parallel path

`net/rx.py` consumes an async iterable of `ModemEvent` and knows nothing about where the events
came from. The live source is `Modem.events()`; the replay source is `radio/replay.py`, which
re-hydrates `RxEvent`/`UnparsedEvent` from a capture JSONL file.

Alternative considered: a separate offline decoder for capture files, as `tests/protocol/corpus.py`
already effectively is. Rejected — a replay path that is not the live path proves nothing about the
live path, and the whole value of replay here is that a bug reproduced from a capture file is a bug
in the code that runs on air. It also gives every future field problem a workflow: capture it,
replay it, fix it offline.

Consequence: `replay.py` belongs beside `capture.py` in `radio/`, because it is the inverse of that
file's record format and must change whenever it does.

### D2. `SetHardware` responses resolve a pending slot inside the existing frame loop

`Modem.request(sub_command, data, timeout)` registers a single pending `(response_code, future)`
and returns an awaitable. The one frame loop resolves it when a matching response arrives. During
startup the modem drives that loop itself to await the handshake and probe; afterwards, requests
resolve for as long as a consumer is iterating `events()` — which both `capture` and `monitor` do
continuously.

Alternatives considered:

- *A second reader for responses.* Rejected outright: this is the exact race `_handshake`'s
  docstring already documents.
- *A background reader task pushing RX onto a bounded queue,* so requests resolve independently of
  consumer progress. This is likely the milestone 3 end state, since the TX scheduler will want
  `TxDone` and `IsChannelBusy` concurrently with RX. Deferred: it changes backpressure semantics and
  needs an overflow policy, and nothing in this milestone issues a request while RX is in flight.
  The pending-slot design is a step toward it, not away from it.
- *Handshake-scoped requests only,* with no general API. Rejected as a known dead end — milestone 3
  would delete it.

One outstanding request at a time. The modem answers serially, so a concurrent second request is a
programming error and raises rather than queueing.

### D3. A `SetHardware` response must not break `Data`/`RxMeta` correlation

This is the sharp edge of D2, and the reason it gets its own decision. Today any frame that is not
`Data` or `RxMeta` flushes a pending `Data` frame with `rx_meta=None`. Once the modem issues
requests, a probe response can legitimately land between a `Data` frame and its `RxMeta` — and under
the current rule that would silently cost the packet its SNR and RSSI, on a link where signal
quality is half of what we are trying to observe.

A recognized `SetHardware` response that resolves a pending request is therefore routed *without*
disturbing the correlation state. Only genuinely unrecognized frames flush the pending `Data`.
Milestone 0's correlation-anomaly event stays exactly as it is for the case it was written for: a
second `Data` frame arriving first.

### D4. Probe results are values, including the absences

`ProbeResult` holds, per sub-command, either the parsed value or a structured absence
(`unsupported` with the modem's error code, or `timeout`). Probing never raises and never aborts
startup: DESIGN.md §4.1 requires that no board is assumed to support any telemetry sub-command, and
a modem that answers nothing must still capture and monitor normally.

Alternative considered: raising on a failed probe, on the theory that a board which cannot answer
`GetDeviceName` is broken. Rejected — it converts an optional feature into a startup dependency and
would make a third board a code change, which §4.1 explicitly sets out to avoid.

`GetSensors` is sent with permissions `0x07` (all) and its CayenneLPP response is recorded as raw
hex, uninterpreted. Parsing it against a fixed schema is precisely what §4.1 forbids, and nothing in
this milestone consumes the values.

### D5. The radio readback is compared, not merely recorded

The probe asks `GetRadio` after applying `SetRadio` and compares the two. A mismatch is an `error`
wide event naming both, and it is shown in the monitor's startup line. Alternative considered:
recording the readback into the capture header and leaving interpretation to whoever reads it later.
Rejected — the failure this catches (a preset that is subtly wrong and receives nothing) is silent
by nature, and the operator watching a monitor scroll past is the person who can act on it.

DESIGN.md §4.3 requires the live readback rather than an assumed preset for time-on-air. That
computation is milestone 3's; establishing the readback it will consume is this milestone's.

### D6. `capture_meta` records observation, and records absence explicitly

The header record is built from the `ProbeResult` and the sighop version and commit — never from
configuration. A field the board did not answer is written as null *with its reason*, not omitted.
This mirrors the discipline the two existing sidecar `.meta.json` files already use, where
"observed" and "reconstructed" are separated and the hardware readback is recorded as absent. A
header that quietly omits what it could not learn decays into exactly the untrustworthy fixture
DESIGN.md §12 warns about.

The existing `captures/2026-09-02.jsonl` and `2026-09-03.jsonl` are not rewritten. Replay must
therefore tolerate a capture file with no header, which is also what any future partially-written
file will look like.

### D7. `packet_id` identifies a reception, not a packet

`packet_id` is minted per received frame at ingress and threaded through the decode, the wide event
and the rendered line, as DESIGN.md §9 requires. It is deliberately *not* the packet content hash:
milestone 3's dedup needs to say "this reception is a duplicate of that one", which requires the two
to have different ids and a separate content hash to join on. Conflating them now would make the
single most valuable field in the log schema ambiguous the moment dedup arrives.

### D8. Rendering is pure functions; unverified content is typed apart

`monitor/render.py` holds pure functions from a decoded record to a string; `monitor/run.py` does
orchestration, counters and the periodic summary. The split exists so the format is testable by
string comparison with no event loop and no device.

The renderer never prints unverified content in the same shape as verified content. A
signature-verified advert shows `✓` and its name; an advert whose signature fails shows the failure
and **no name at all** — not the name with a warning. DESIGN.md §5 makes discarding badly-signed
adverts the rule and §8 makes "never present unverified data as verified" a hard one; a name
rendered next to a caveat is how that rule dies three milestones later when someone reuses the
formatter.

### D9. The monitor shows unfiltered receptions

No dedup, and duplicates are visible as duplicates. This is partly scope (dedup is milestone 3) and
partly deliberate: milestone 1's corpus analysis measured 41.6% repeats offline, and milestone 3 is
told to re-measure rather than treat that as a constant. A monitor that silently collapsed
duplicates would remove the baseline that measurement needs.

### D10. Replay uses the record's timestamp and does not pace itself

Replay runs as fast as it can read, and renders each line with the timestamp from the record rather
than wall-clock now. Alternative considered: real-time pacing from the recorded inter-frame gaps.
Rejected for this milestone — replaying 9 hours of capture in real time to check a decoder is not a
workflow anyone wants, and the pacing has no consumer until the scheduler exists.

## Risks / Trade-offs

**[Changing the frame loop could regress milestone 0's correlation invariants]** → The existing
`tests/test_modem.py` scenarios stay unchanged and must keep passing; the new behaviour gets its own
scenario (D3), including the specific interleaving where a `SetHardware` response arrives between a
`Data` frame and its `RxMeta`. That case is the one a hand-written change is most likely to get
wrong, and it is invisible in production — it costs a packet its signal metadata without any error.

**[The probe writes to a link that has so far only been read]** → Every sub-command sent is a query
or the already-exercised `SetRadio`; no `Data`, no `SetTxPower`, no `Reboot`. The probe is bounded
by a timeout per request and its failure is non-fatal, so the worst case is a capture that starts a
few seconds later with an emptier header than we wanted.

**[Replay proves the pipeline, not the radio]** → Stated plainly and reflected in the exit criteria:
this milestone is not complete on green replay tests. It is complete when adverts from the live mesh
render with correct names and verified signatures in real time — the thing DESIGN.md §12 actually
asks for. Replay is how failures get diagnosed afterwards, not the evidence itself.

**[Live traffic may contain frames the decoder rejects]** → That is a finding, not a failure, and it
is the main reason to run this milestone. Every rejection is a structured event carrying the raw
bytes and the violated rule, and `--capture` records the session, so an unexpected frame becomes a
corpus entry and a milestone 1 follow-up rather than a lost observation.

**[Two-line-per-packet output can outrun a human on a busy mesh]** → Accepted for now. The observed
rate is a few frames a minute; if a burst makes it unreadable, the output is line-oriented precisely
so `grep` works, and a filter flag is a small addition. Designing a filter before seeing the problem
would be guessing at which dimension matters.

**[In-memory contact accumulation is one step from a persistence shortcut]** → The monitor tracks
node hashes and counts for its summary line and nothing else: no name-to-key store, no first-heard
history, no file. Milestone 5 owns the `contact` table, and a half-contact-store here would be the
thing it has to unpick.

## What the live runs found

Two short runs against the live mesh on 2026-09-03 (Heltec V3, `EU868_NARROW`), ahead of the
evening run the exit criterion asks for. Recorded here because three of these are things no
capture file could have told us.

**Adverts decode off live air with verified names — the exit criterion, demonstrated.** Against the
V4: `✓ advert 'Sigurs' CHAT flags=0x81 key=[redacted]`, arriving both DIRECT h0 and FLOOD h1
via `path=[redacted]`, with SNR and RSSI attached. Six frames, four adverts verified, zero decode
failures, zero reconnects, zero reboots. This is a three-minute run, not the evening one task 9.1
asks for, but it is the first end-to-end evidence that `radio/` and `protocol/` compose into a
working receiver on real air.

**The same code ran unmodified against both boards.** A Heltec V3 behind a CP2102 bridge and a
Heltec V4 on the ESP32-S3's native USB (`/dev/ttyACM0`). The V4 reported `Heltec V4 OLED` — the
runtime-chosen name DESIGN.md §4.1 predicted, and the reason device name is a probe result rather
than a per-firmware constant. `GetSensors` answered empty on both. Worth noting for milestone 3:
the two present different failure modes on reset, since a native-USB board leaves the bus entirely
while the CP2102 does not.

**The probe answers, and confirms the preset.** `GetDeviceName` → `Heltec V3`; `GetRadio` →
869.618 MHz / BW 62.5 kHz / SF8 / CR8, matching what was applied; `GetTxPower` → 0 dBm;
`GetVersion` → v1; `GetBattery` → ~4.0 V; `GetMCUTemp` → 36-37 °C. The readback comparison
D5 exists for came back clean, which is the first direct evidence that the corrected
`EU868_NARROW` is what the board is actually running rather than a preset that happens to
receive.

**`GetSensors` answers with an empty CayenneLPP payload** — zero bytes, not an error. This
settles the shape question in DESIGN.md §4.1 for the V3 as far as it can be settled: the
sub-command is supported, the payload is build-flag dependent, and this build carries no
sensors. A fixed schema would have parsed nothing here and would break on the V4.

**Opening the serial port resets the board.** The first run logged
`modem_handshake_unanswered` after 2 s and then completed every probe request in 19 ms — the
CP2102 asserts DTR on open, the ESP32-S3 reboots, and the `SetRadio` sent into that window is
lost. Milestone 0 never saw it because its handshake waited indefinitely. Fixed here: the
handshake retries inside a budget rather than asking once.

**The reconnect loop could spin.** The second run hit a device that was present but returned
EOF immediately, and reconnected seven times in 60 ms — `KissTransport` only backed off
*after* a failed connect, and connecting never failed. Fixed here: back off before every
attempt.

**One frame in eleven was damaged on the wire.** A frame arrived with type byte `0x80`
carrying 67 bytes, followed 1 ms later by an `RxMeta` with no preceding `Data` — a `Data`
frame that lost its leading `0x00` between the ESP32 and the CP2102, where 8N1 has no error
detection. It was reported as an unparsed frame with its raw bytes and did not disturb
anything else. Deliberately not worked around: MeshCore's own firmware masks the KISS port
nibble (`type_byte & 0x0F`) and doing the same would have accepted this frame, but a frame
that lost a byte is damaged, and accepting it would hand the decoder bytes we know to be
wrong. Whether this rate holds is a question for the evening run — one sample is not a rate.

**The V3 power-cycles on its own, and that is a hardware fault.** A 25-minute run
(`captures/2026-09-03-2.jsonl`) recorded seven `rst:0x1 (POWERON)` ROM banners in nine minutes,
after which reception stopped for 17 minutes. Two candidate causes were tested and one was
eliminated:

- *DTR/RTS auto-reset* — plausible, because those lines drive `EN` and `GPIO0` on an ESP32 board
  and an `EN` pull reports as POWERON rather than as a brownout. **Tested and refuted, in the
  direction opposite to the guess:** opening with `dtr`/`rts` deasserted reset the board 0.52 s
  after every open, twice out of two, while opening with pyserial's defaults reset it not at all.
  The circuit fires on a *difference* between the lines, so the transition is what matters. sighop
  keeps the defaults, and `serial_connector`'s docstring records the measurement so the "careful"
  version is not reintroduced.
- *Kernel USB power management* — ruled out from sysfs: the CP2102's `power/control` is `on`,
  `runtime_suspended_time` is `0`, and `active_duration` equals `connected_duration`. The device
  has never been suspended.
- *Another process opening the tty* — pyserial's own error text raises the possibility
  ("multiple access on port?"). Ruled out by polling `/proc/*/fd` throughout a run: nothing else
  ever held it.
- *The board itself* — **confirmed by swapping it.** The V4, USB-powered with no battery, ran
  three minutes with zero resets and decoded live traffic normally. The V3 is faulty hardware.
  What follows is the evidence that got there, kept because the elimination order is the reusable
  part.
- *The timing named it before the swap did.* The reset period is **75.07 s,
  measured repeatedly**, with the *phase* set by nothing the host does — it is invariant across
  all four DTR/RTS combinations, both USB ports, with and without sighop, and with and without
  another reader. A brownout under RF load would be irregular. `rst:0x1 (POWERON)` with no panic
  dump means the whole chip including the RTC domain lost power, so it is not a firmware crash
  either. Hardware or firmware image; no software change on this side fixes it.

*A correction that matters for reading the logs:* the `serial_disconnected` events are a
**consequence** of the reset, not an independent USB fault. `connected_duration` shows the CP2102
stayed enumerated for 32.6 minutes across many resets, so the bridge never went away — a read
merely returns no data for a moment while the ESP32 behind it restarts, and pyserial reports that
as a disconnect. `KissTransport` reconnecting there is still the right response; it just is not
evidence of a USB problem.

One consequence for sighop, implemented: a boot banner on the stream must be read as "the device
restarted" and trigger a fresh handshake and probe. The USB bridge stays enumerated across an
ESP32 reset, so the banner is the *only* signal that it happened — without it the board silently
runs the firmware's build defaults instead of what `SetRadio` applied.

*What the reboot does not cost, checked rather than assumed:* the KISS build defines `LORA_FREQ`,
`LORA_BW` and `LORA_SF` but no `LORA_CR`, so a rebooted board runs CR5 while `EU868_NARROW` applies
CR8. That mismatch does **not** deafen it — the bare reader received frames on the firmware default
CR5 and sighop receives them on CR8, so LoRa's explicit header is carrying the payload's coding
rate, as it is supposed to. The reboot still loses the configuration; it just does not lose it in
the way first suspected.

**No frame the decoder rejected.** Every structurally intact frame decoded: 11 receptions,
0 decode failures, payload types GRP_TXT (9), RESPONSE (1), TXT_MSG (1), routes FLOOD (9) and
DIRECT (2), all with 3-byte path hashes.

### The overnight V4 session (2026-09-04) — where the corpus grew

Three runs on the Heltec V4 spanning 2026-09-03 22:14 to 2026-09-04 13:18, split by device
restarts: `captures/2026-09-04.jsonl` (56 frames), `-02` (2), `-03` (33). **91 receptions,
0 decode failures, 0 unreadable lines, 22 adverts all verifying.** Every file carries the
`capture_meta` header, which is the first live evidence that the header record this change
added is written correctly by a real run rather than only by its tests.

Two shapes the 351-frame corpus lacked arrived here, which is why these files were appended to
it (task 9.5):

- **`ROUTE_TYPE_TRANSPORT_FLOOD`, once.** A repeater's advert, transport codes
  `0x0075`/`0x0000`, hop 0, 2-byte hash. Transport routing had been synthetic-only since
  milestone 1; the codec now decodes and re-encodes a real one byte-identically. The second
  code being zero is worth noting but not worth a rule — one frame is a sighting, not a
  distribution. `TRANSPORT_DIRECT` remains unsighted.
- **CONTROL, six times**, in three lengths (6 B ×4, 38 B ×2, 10 B ×1), all DIRECT, all hop 0,
  1-byte hash. Preserved uninterpreted, exactly as `parse_payload` is specified to do. The two
  38-byte ones each carry a repeater's public key inline, which is suggestive of a
  path/transport control exchange, but nothing here decides it and this milestone does not
  interpret it.

Also new to the corpus, though not a gap anyone had listed: **CHAT-type adverts** (flags
`0x81`, six frames, the operator's own node), where the corpus previously held only repeaters
and room servers.

**Mixed path hash sizes: still unanswered.** In the V4 session 81 distinct packets produced 91
receptions, and every one of the 10 repeated packets arrived twice with the *same* hash size —
so still no packet seen arriving by routes that disagree. Carried forward again; it needs a
run with heavier flood repetition than either night produced.

**Repetition is not a constant of this mesh.** Counted the firmware's way, the V4 session ran
11.0% repeats (81 distinct packets in 91 receptions, at most 2 copies each, max spread 4.6 s)
against milestone 1's 41.6% on the V3 nights. The direction holds — all 10 repeats were flood
receptions and not one of 57 direct receptions repeated — but milestone 3 must measure its own
dedup window rather than inherit either number.

## Open Questions

- **Does any repeater on this mesh mix path hash sizes within one packet's lifetime?** Carried over
  from milestone 1, which could not answer it from a corpus where each frame carries a single size
  code. Observable here by watching one packet arrive by multiple routes.
- **What does the V3 answer for `GetSensors`, and does its shape match the build-flag expectation in
  §4.1?** Recorded raw this milestone; the answer informs whether the WebUI needs a dynamic
  CayenneLPP parser or a fixed one.
- ~~**Does live traffic contain any `ROUTE_TYPE_TRANSPORT_*`, CONTROL or RAW_CUSTOM frame?**~~
  **Answered, partly.** The 2026-09-04 session caught one `TRANSPORT_FLOOD` frame and six CONTROL
  frames; both are now corpus evidence rather than synthetic-only. `TRANSPORT_DIRECT` and
  RAW_CUSTOM are still unsighted and stay on the list.
- **What is in a CONTROL payload?** Six live ones in three lengths, two of them carrying a
  repeater's public key inline. Uninterpreted by design this milestone; whoever needs them
  (milestone 3's path handling is the likely caller) reads the firmware rather than guessing
  from six frames.
- **Is the two-line render the right density, or does the detail line want to be opt-in?** Answerable
  only by watching it run for an evening.
