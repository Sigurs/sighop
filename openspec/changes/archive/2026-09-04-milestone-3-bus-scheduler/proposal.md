## Why

Milestone 2 proved sighop can read the mesh. Everything it built is stateless and
single-threaded through one decoder: no component yet holds state across frames, and nothing
has ever wanted to transmit. Milestone 3 is where both change — the shared RX state (dedup,
learned paths) that entities will read, and the TX scheduler that stands between N entities
and one half-duplex radio operating under a legal duty-cycle ceiling.

The ceiling is why this milestone exists as its own step rather than as part of "first
transmit". On EU 868 the 10% limit is a regulatory obligation, not a tuning knob (DESIGN.md
§4.3); a scheduler bug here is a compliance problem. Building and instrumenting the whole
outgoing path **while transmit is still disabled** means the budget accounting, the priority
ordering and the advert policy get verified against a full night of real load before the
first byte is ever keyed. Milestone 4 should then be a flag flip and a watched packet, not
new code written under live-fire conditions.

## What Changes

- **New `sighop run` — the first sighop *runtime*.** Opens the live link (or a capture
  replay), and wires the pieces together for the first time: modem → RX decode → dedup →
  path learning → bus fan-out → TX scheduler. Transmit is disabled; the scheduler runs fully
  and reports what it *would* have sent. Rendering reuses `monitor/render.py`.
- **New shared RX state.** A dedup cache keyed on a hash of (payload type ‖ payload bytes) —
  deliberately excluding the path, which mutates per hop — and an in-memory reverse-path
  store learned from flood receptions. Both are platform-wide, not per-entity (DESIGN.md
  §4.2). Neither is persisted: milestone 5 owns the database.
- **New virtual network bus.** In-process asyncio pub/sub: RX fans out to every subscriber,
  TX is a submission API returning a handle the caller awaits for completion.
- **New TX scheduler**, the risky part. One packet in flight; four priority classes; a
  rolling-hour airtime budget computed from LoRa time-on-air; per-submission deadlines with
  an explicit drop-and-log on expiry; and the **receive-only gate**, off by default, which
  suppresses the final hand-off to the modem while everything upstream of it — scheduling,
  budget consumption, logging, counters — behaves exactly as it will on air.
- **New airtime calculation.** Time-on-air from the *live* `GetRadio` readback, never an
  assumed preset, matching the firmware's own parameters (notably its SF-dependent preamble:
  32 symbols at SF ≤ 8, 16 above). Cross-checked at startup against the board's own
  `GetAirtime` answer, because the modem will tell us its estimate if we ask — a free
  independent check on the number the whole duty-cycle ceiling rests on.
- **New advert policy.** The 24 h flood floor, zero-hop adverts off, per-entity jitter, a
  minimum gap between flood adverts from any two local entities, startup stagger, and the
  override-with-mandatory-expiry from DESIGN.md §13. Driven by in-memory entity stubs that
  generate and sign real adverts, so the budget sees real load with real time-on-air.
- **New modem TX path.** `Data` frame submission plus `TxDone` (0xF8) and `TxBusy` (0x07)
  correlation, with the one-packet-in-flight invariant enforced at the modem rather than
  only in the scheduler. Implemented and unit-tested now, unreachable while the gate is off —
  and a test asserts that: with transmit disabled, **zero `Data` frames reach the transport**.
- **DESIGN.md §13 unknown #2 gets answered** — whether the dedup cache is sized by entries or
  by time — from measurement over the 442-frame corpus and the live run, not from a guess.

## Capabilities

### New Capabilities
- `airtime`: LoRa time-on-air for a given payload length under the live radio parameters,
  the SF-dependent preamble the firmware uses, and the startup cross-check against the
  board's `GetAirtime` — including what happens when the two disagree.
- `rx-dedup`: the shared duplicate cache — its key (payload type and payload bytes, path
  excluded), its bounding by both entry count and age, and the requirement that a duplicate
  is reported and counted, never silently dropped.
- `path-learning`: the shared in-memory reverse-path store — what is learned from which
  receptions, how a path is keyed when the sender's public key is unknown, and the
  most-recently-confirmed-wins resolution rule.
- `net-bus`: the in-process pub/sub — RX fan-out to all subscribers with per-subscriber
  isolation (one slow or failing subscriber must not stall the pipeline), and the TX
  submission API and its awaitable handle.
- `tx-scheduler`: one packet in flight, the four priority classes and their ordering,
  round-robin within a class, the rolling-hour airtime budget and its reserve for classes 0
  and 1, deadline expiry as an explicit drop, `TxBusy` requeue-at-head, the receive-only gate,
  and the DESIGN.md §9 *Packet TX* wide event.
- `advert-policy`: the advert scheduling rules — 24 h flood floor, zero-hop disabled by
  default, `base ± 25%` jitter, the minimum inter-entity flood gap, startup stagger, and the
  override that requires an expiry (default 1 h, maximum 24 h) and auto-reverts.
- `modem-tx`: submitting a `Data` frame to the modem and resolving it against `TxDone`,
  `TxBusy` or a timeout, with at most one transmission outstanding.
- `runtime-cli`: the `sighop run` command — source selection (live or replay), the
  transmit-enable flag that is off unless explicitly given, and the periodic status line
  covering duty-cycle usage against the ceiling, queue depth by class, dedup hit rate and
  suppressed-transmission counts.

### Modified Capabilities
- `modem-rx`: the frame loop must route `TxDone` (0xF8) responses and `TxBusy` errors to the
  waiting transmission instead of reporting them as unparsed frames, and must serialize
  outbound `Data` frames against the one-in-flight invariant.

## Impact

- **New code:** `src/sighop/net/` gains `bus.py`, `dedup.py`, `paths.py`, `airtime.py`,
  `tx.py` (scheduler) and `adverts.py`; `src/sighop/runtime.py` wires them; `src/sighop/cli.py`
  gains the `run` subcommand.
- **Modified code:** `src/sighop/radio/modem.py` (Data send, `TxDone`/`TxBusy` correlation),
  `src/sighop/monitor/render.py` (status-line formatting reused by `run`).
- **`net/rx.py` stays stateless.** Dedup does not move into the decode stage; it sits behind
  it on the bus. That keeps milestone 2's property that replaying a capture reproduces every
  reception, duplicates included, and keeps the decode stage a pure function.
- **`protocol/` is not touched** beyond calling its existing advert-signing path. Its import
  boundary test must keep passing.
- **No new dependencies.**
- **DESIGN.md** is updated in this change, per its own rule: §11 gains the new `net/` modules
  and `runtime.py`; §4.3's "token bucket over a rolling window" is restated as the sliding
  window actually implemented, so the regulatory claim and the code assert the same thing;
  §13 unknown #2 is answered.
- **Regulatory surface.** This is the first milestone whose bugs could produce an unlawful
  transmission — later, when the gate opens. The duty-cycle ceiling therefore gets tests that
  assert it holds under sustained overload, and the budget is enforced in sighop independent
  of any modem setting (the stock firmware ships a 50% duty-cycle default, which does not
  satisfy EU 868).
- **Out of scope, deliberately:** any actual transmission (milestone 4 — the gate stays off,
  and enabling it is not part of this change's exit criteria); any database write (milestone
  5); real entities with persisted identities (the advert stubs are in-memory and their keys
  die with the process); per-entity fairness shares (DESIGN.md §4.3 defers them past 2–5
  entities); `IsChannelBusy`/`GetNoiseFloor` telemetry beyond what the scheduler needs as a
  hint (the WebUI at milestone 8 is what consumes it).
