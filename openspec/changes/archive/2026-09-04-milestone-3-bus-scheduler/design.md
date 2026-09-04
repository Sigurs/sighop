## Context

Milestones 0–2 produced a receiver: transport, modem, decoder, monitor, replay. Every piece
of it is stateless above the serial link, and nothing in the codebase has ever put a byte on
the air. Milestone 3 adds the two things that change that shape — **shared state across
receptions** (dedup, learned paths) and **an outgoing path** (bus, scheduler, budget) — and
deliberately stops one step short of transmitting.

Three constraints shape everything below:

1. **One packet in flight.** The modem accepts a single pending transmission; `TxBusy` is the
   rejection. Everything in the scheduler follows from that (DESIGN.md §4.3).
2. **The duty-cycle ceiling is regulatory.** On EU 868's 869.4–869.65 MHz sub-band, 500 mW
   e.r.p. is conditional on ≤10% duty cycle — 360 s of transmit per hour. sighop enforces this
   itself; the stock firmware's 50% default does not satisfy it.
3. **Transmit stays off.** The whole outgoing path is built, exercised and measured behind a
   gate that is off by default. That is not a debug flag; it is how the milestone verifies
   budget accounting against real load without keying the transmitter.

The firmware is available as the vendored `related-repos/MeshCore` submodule, and it is the
authority for anything about the radio's own behaviour — as it was for §5's cryptography.
Reading it produced the correction in D3, which no amount of reasoning from the docs would
have found.

## Goals / Non-Goals

**Goals:**

- Shared dedup and path learning behind the decode stage, with the decode stage left stateless.
- A bus whose RX fan-out cannot be stalled by one slow subscriber.
- A scheduler that honours priority, keeps exactly one packet in flight, and provably holds a
  10% duty cycle under sustained overload.
- Time-on-air computed from the live radio configuration and independently cross-checked
  against the board's own estimate.
- The advert policy of DESIGN.md §4.3 and §13, driven by real signed adverts from in-memory
  entity stubs, so the budget sees representative load.
- A `sighop run` that can be pointed at the live link overnight and answer, from its own
  measurements, DESIGN.md §13's unknown #2.
- The modem TX path complete and tested, so milestone 4 is a flag and a watched packet.

**Non-Goals:**

- Transmitting. The gate stays off; opening it is milestone 4's exit criterion, not this one's.
- Persistence of anything — contacts, paths, entity keys, packet log (milestone 5).
- Per-entity fairness shares (DESIGN.md §4.3 defers them beyond 2–5 entities).
- Path *scoring* beyond most-recently-confirmed-wins (§13 unknown #3 needs multiple observed
  routes to the same peer, which we do not have yet).
- Retry/ACK-driven resend logic. The scheduler resolves a submission at `TxDone`; waiting for
  an ACK is an entity concern and there are no entities until milestone 6.
- CSMA. The modem does p-persistent CSMA itself; we do not reimplement or fight it.

## Decisions

### D1 — Time-on-air is computed in Python from the live radio readback, not asked per packet

The Semtech LoRa formula, with parameters taken from the `GetRadio` readback that milestone 2
already performs at startup, and re-read on every reconnect (the board reverts to build
defaults on reset and reports nothing about it).

The formula's inputs must match the firmware, not textbook defaults:

- **Preamble is spreading-factor-dependent**: `preambleLengthForSF(sf)` in
  `src/helpers/radiolib/RadioLibWrappers.h:56` returns **32 symbols at SF ≤ 8 and 16 above**,
  and `SetRadio` updates it. This is not the RadioLib default of 8.
- **Explicit header, CRC on** (`CustomSX1262.h:69` calls `setCRC(1)`; RadioLib's header type
  defaults to explicit).
- **Low-data-rate optimisation follows RadioLib's auto rule** — enabled when symbol time
  ≥ 16 ms, which at BW 62.5 kHz means SF ≥ 10. It is *off* at the default EU narrow preset.

*Alternative rejected:* asking the modem `GetAirtime` for every packet. It costs a serial
round-trip on the critical path, its answer is truncated to whole milliseconds
(`getEstAirtimeFor` divides by 1000), and it is unavailable in replay — where most of the
scheduler's tests run.

### D2 — But the board's estimate is used as a startup cross-check

`GetAirtime` takes a packet length and returns the firmware's own estimate. At startup, and
after each reconnect, `run` asks for a short ladder of lengths (16, 64, 128, 255 B) and
compares each against D1's calculation. Disagreement beyond 1 ms *and* 2% is an `error`-level
wide event naming both figures; the run continues on our own number.

This is close to free and it checks the single number the entire duty-cycle ceiling rests on
against an independent implementation. It is also the only cheap way to notice that the board
is not running the radio configuration we think it is.

### D3 — DESIGN.md's deaf-window table is wrong and is corrected here

§4.3 gives 64 B ≈ 0.64 s and 255 B ≈ 2.2 s at the default preset. Those numbers assume an
8-symbol preamble. With the firmware's actual 32 symbols at SF8:

| Payload | §4.3 as written | Actual (32-symbol preamble) |
|---|---|---|
| 64 B | ~0.64 s | **~0.74 s** |
| 255 B | ~2.2 s | **~2.31 s** |

A ~15% understatement of every transmission's cost, in the direction that matters. DESIGN.md
§4.3 is updated in this change, per its own rule about reality disagreeing with the document.

### D4 — The budget is a sliding window of completed transmissions, not a token bucket

A deque of `(charged_at, airtime_ms)` covering the last 3600 s. Admission requires
`window_sum + this_packet_airtime ≤ ceiling`.

§4.3 calls this a token bucket over a rolling window; a bucket only approximates the
regulatory statement, while the window *is* it — the test asserts literally "no 3600 s
interval contains more than 360 s of transmission", which is the sentence the regulation
uses. Memory is bounded by the ceiling itself: at minimum airtime a full hour cannot hold
more than a few thousand entries, and an entry cap backstops it. DESIGN.md §4.3's wording is
updated to match what is implemented.

### D5 — Charge at submission to the modem, never refund on failure

Airtime is charged when the packet is handed to the modem, not when `TxDone` arrives. A
failed transmission still occupied the channel; refunding it would let a failure loop exceed
the ceiling. The only packets never charged are those that never reach the hand-off (dropped
on deadline, or rejected at admission).

The error is therefore always in the conservative direction — sighop believes it has used
slightly more airtime than it has.

### D6 — Gate-suppressed packets consume the budget and are logged as `suppressed`

When the receive-only gate is closed, the packet is scheduled, charged, counted and logged
exactly as if sent, then dropped at the final hand-off with `tx_result: "suppressed"`.

*Alternative rejected:* not charging suppressed packets. It would make a gated run
unrepresentative of on-air behaviour — and "verify the budget accounting against what would
have been sent" is precisely this milestone's exit criterion. A gated run whose budget reads
zero proves nothing.

### D7 — Class reserve is a fraction of the ceiling, not a separate bucket

Classes 2 and 3 stall once the window reaches 90% of the ceiling; classes 0 and 1 may use the
full 100%. One window, one number, a threshold per class — rather than two budgets that can
disagree about what hour it is. Defaults (90%, i.e. 36 s/h reserved) are configurable.

### D8 — One-in-flight is enforced in the modem, not only in the scheduler

The invariant belongs to the resource, not to its current sole user. `Modem.send_packet()`
holds an asyncio lock across submit-and-await-`TxDone`, so a second caller — a future
milestone's code, a test, a bug — cannot violate it.

- `TxBusy` (`Error` 0xF1 with `0x07`) resolves the submission as a lost race; the scheduler
  requeues **at the head** of its class with a short delay and a bounded attempt count. Never
  a busy-loop (DESIGN.md §4.3).
- The `TxDone` timeout must be strictly longer than the firmware's own: the modem's CSMA adds
  `txdelay`/`slottime` waits before transmitting, and it self-resolves with a failed `TxDone`
  after `getEstAirtimeFor(len) × KISS_TX_TIMEOUT_FACTOR`. Timing out earlier than the board
  does would desynchronise us from a modem that is still going to answer.

### D9 — Every submission carries a deadline; expiry is a logged drop

Checked both while queued and at dequeue. A packet that cannot be sent within its deadline is
dropped and logged with the reason and its queue wait — silent unbounded queueing is the worse
failure (DESIGN.md §4.3). Queue depth is additionally capped per class; hitting the cap drops
the *oldest* member of that class, since in every class the freshest packet is the useful one.

### D10 — Dedup keys on payload type and payload bytes only

`blake2b(bytes([payload_type]) + payload, digest_size=16)`. Path and transport codes are
excluded because they mutate at each hop — that is the whole point (DESIGN.md §4.2).

Frames that fail to decode structurally have no payload and are **never deduped**: they pass
through every time. A corrupt frame is evidence, and two corrupt frames are two pieces of it.

### D11 — The cache is bounded by both entries and age, which answers §13 unknown #2

The unknown asks "entries or time". The measurements say the question is a false choice:

- **Time governs correctness.** Repeats arrive within seconds — the milestone 2 night showed a
  maximum spread of 4.6 s between copies. TTL is what makes the cache *right*.
- **Entries govern memory.** Repetition rate is not a property of the mesh: the same duplicate
  measure gave 41.6% over the milestone 0 nights and 11.0% over milestone 2's. Nothing about
  observed traffic bounds how many distinct packets an hour can hold, so the entry cap is what
  makes the cache *safe*.

Defaults: TTL 300 s, 4096 entries — both configurable, both reported in the status line with
the observed hit rate and the largest inter-copy spread seen, so the next sizing decision is
made from data rather than from these defaults. DESIGN.md §13 unknown #2 is marked resolved.

### D12 — Dedup and path learning live on the bus, not in `net/rx.py`

The decode stage stays a pure function of one frame. That preserves milestone 2's property
that replaying a capture reproduces every reception — duplicates included — which is exactly
what makes the corpus usable for measuring the dedup cache in the first place. A stateful
decode stage would hide the thing being measured.

### D13 — Path learning is in-memory, keyed by public key where one exists

Learn from flood receptions: the reverse of the received path is a route back to the sender.
Key by the sender's Ed25519 public key when the payload carries one (adverts do), otherwise by
source hash — with the 1-byte-hash ambiguity of DESIGN.md §3 recorded on the entry rather than
resolved, since resolving it needs contacts (milestone 5).

Resolution is most-recently-confirmed-wins. SNR per hop is *recorded* but not scored; §13
unknown #3 stays open until multiple routes to one peer are actually observed. Nothing is
persisted — milestone 5 owns the `path` table.

### D14 — RX fan-out isolates subscribers with bounded queues

Each subscriber gets its own bounded queue; a full queue drops for that subscriber and emits
an event naming it. The pipeline never awaits a subscriber.

*Alternative rejected:* awaiting every subscriber in turn. The radio keeps receiving whether
or not a room server is busy writing to a database; back-pressuring the decode stage would
turn one slow entity into lost frames for all of them, and lose them silently — at the modem's
buffer, where nothing can log it.

### D15 — Advert load comes from in-memory entity stubs with real keys

Generated Ed25519 keypairs (rejecting node-hash collisions with existing stubs, per DESIGN.md
§3), real `createAdvert`-shaped signed adverts, real payload lengths. This exercises the
signing path and gives the budget honest time-on-air figures.

They are stubs, not entities: keys are generated per process and die with it, nothing is
written anywhere, and the status line marks them ephemeral. Milestone 5 introduces the real
thing; a stub that quietly persisted would be a milestone-5 design decision made by accident.

### D16 — Advert policy defaults, and the override that makes an overnight test possible

Per DESIGN.md §4.3 and §13: 24 h flood floor, zero-hop adverts off (interval 0), `base ± 25%`
jitter per entity, a minimum gap between flood adverts from any two local entities (default
10 min), and initial adverts staggered uniformly across the interval rather than fired at
startup.

The override — faster interval, **mandatory expiry**, default 1 h, maximum 24 h, auto-revert to
the floor — is what lets an overnight dry run generate more than one advert per entity. That is
its intended use here, and its auto-expiry is what stops this milestone from establishing a
habit the floor exists to prevent. There is no permanent override.

### D17 — The scheduler takes an injectable clock

Every deadline, jitter draw, window boundary and advert schedule reads a clock passed in at
construction. A simulated hour then runs in milliseconds, so the duty-cycle assertion of D4
can be tested under sustained overload in the normal test suite rather than in an overnight
run. Given the ceiling is a legal limit, its test must be cheap enough to run on every commit.

### D18 — `sighop run` is a new command, not a flag on `monitor`

`monitor` is an operator's read-only view of the air; `run` is the platform — bus, dedup,
paths, scheduler, stubs. They share `monitor/render.py`, which stays pure formatting functions
testable by string comparison. Merging them would put the scheduler behind a flag on a command
whose documented promise is that it only listens.

`run` prints a periodic status line (default 60 s): duty-cycle usage against the ceiling,
queue depth by class, dedup hit rate, suppressed count, and the gate state — stated on every
line, because "transmit is off" is the fact an operator most needs never to be uncertain about.

## Risks / Trade-offs

- **Our time-on-air could be wrong, and the ceiling rests on it** → D2's cross-check against
  the board's own estimate, plus unit tests against the firmware's formula and hand-computed
  Semtech values. D3 is evidence this risk is real, not theoretical.
- **Charging at submission overstates usage when a TX fails early** (D5) → accepted: the error
  is conservative, and refunding is what would let a failure loop breach the ceiling.
- **A gated run cannot prove the modem TX path works** → true. D8's code is unit-tested against
  a fake transport only; milestone 4 is what confirms it against a radio. What this milestone
  *can* prove is that nothing reaches the transport while gated, and that is asserted.
- **Someone bypasses the gate by calling the modem directly** → the modem's send path is the
  single choke point, the gate is checked inside it, and a test asserts zero `Data` frames on
  the transport with transmit disabled.
- **Sliding-window memory grows if a bug submits at high rate** (D4) → entry cap on the window;
  and admission control means an over-submitting caller is rejected, not recorded.
- **Bus queue drops lose frames for one subscriber** (D14) → each drop is an event naming the
  subscriber and its depth; a subscriber that drops anything is visible in the status line.
- **Stub adverts could be mistaken for a real identity by a listening mesh node.** They are
  real signed adverts with real names — but nothing transmits them this milestone, and the
  names are marked as stubs. Milestone 4's first transmission is a DM to our own peer board,
  not an advert, precisely so the mesh is not told about identities that vanish at process exit.
- **Scope.** This is the largest milestone so far: eight new capabilities. The sequencing in
  tasks.md is airtime → dedup/paths → bus → scheduler → adverts → runtime, so that each layer
  is tested before the one above it exists, and the milestone has a usable stopping point after
  the scheduler even if the advert stubs slip.

## Open Questions

1. **Final dedup TTL and entry cap** — the D11 defaults are starting points; the overnight
   `sighop run` is what sets them. The status line exists to make that a measurement.
2. **Path scoring** (§13 unknown #3) stays open — it needs two observed routes to one peer.
3. **Whether the D8 `TxDone` timeout factor is right** cannot be settled while gated: the
   firmware's CSMA delay before transmission is only observable once we actually transmit
   (milestone 4).
4. **Minimum inter-entity advert gap** is set at 10 min by judgement, not measurement. With a
   24 h floor and 2–5 entities it is nearly unbindable; it matters only if the entity count
   ever grows, which is the scale failure §4.3 designs against.
