## Context

DESIGN.md §4.1 and §12 already settled the shape of this milestone: a `KissTransport` that
does framing/escaping and nothing else, a `Modem` layer above it that owns semantics, and a
`sighop capture` command that runs unattended overnight against the live mesh. This document
fills in the pieces that document left as prose — file format, correlation mechanics,
reconnect parameters — since those need to be fixed before code, not discovered while it's
running unattended overnight with nobody watching.

The Heltec V3 constraint from DESIGN.md §4.1 dominates this design: KISS runs over the sole
USB-serial link, so there is no firmware debug output to fall back on. Whatever sighop logs
and captures *is* the entire observability surface for this run.

The authoritative protocol reference for every frame/command detail below (command bytes,
`RxMeta` encoding, `SetHardware` sub-commands) is vendored locally at
`related-repos/MeshCore/docs/kiss_modem_protocol.md` — treat it as source of truth over this
document's summaries.

## Goals / Non-Goals

**Goals:**
- Open and hold the KISS serial link unattended for a multi-hour run, surviving a USB
  adapter drop/replug without operator intervention.
- Correlate each `RxMeta` (0xF9) frame with the `Data` (0x00) frame it follows, per the
  modem protocol's ordering guarantee.
- Persist every received frame — parseable or not — with enough metadata (RxMeta, wall-clock
  timestamp, raw bytes) that milestone 1 can replay it as a fixture and this run can be
  audited afterward.
- Make a malformed or unparseable frame visible in the capture output, never silently
  dropped.

**Non-Goals:**
- No TX path. The scheduler, priority classes, and airtime budget (DESIGN.md §4.3) are
  milestone 3+ and do not exist yet — there is nothing to gate with a receive-only flag,
  because nothing here can transmit.
- No packet decoding beyond what `Modem` needs to frame RX (payload header, transport codes,
  path, crypto) — that is milestone 1, developed against this milestone's output.
- No database, no web UI, no multi-entity concepts. Single process, single file.
- No `SetHardware` telemetry probing (`GetBattery`, `GetMCUTemp`, etc.) beyond what is
  needed to confirm the link is alive — the full probe-and-record behavior belongs with
  whichever milestone first surfaces that telemetry (WebUI, per DESIGN.md §4.1).

## Decisions

### Capture file format: newline-delimited JSON (JSONL)
One JSON object per line: `{"ts": <iso8601 with tz>, "kind": "rx_frame" | "unparsed", "raw_hex": "...", "rx_meta": {"snr": ..., "rssi": ...} | null}`.
- Append-only, crash-safe (a partial last line is the only possible corruption, and it's
  detectable), and trivially streamable into milestone 1's fixture loader without a custom
  binary parser.
- `raw_hex` keeps the exact on-wire bytes (post-KISS-unescaping) so milestone 1 can re-derive
  anything sighop's own decoder gets wrong at capture time — the capture is ground truth, not
  sighop's interpretation of it.
- Alternative considered: raw KISS-framed bytes to a `.kiss` file. Rejected — it entangles
  timestamp/RxMeta correlation with the framing itself and is harder to inspect by hand
  during an overnight run.

### RxMeta correlation: strict next-frame pairing
The modem protocol guarantees `RxMeta` immediately follows the `Data` frame it describes.
`Modem` holds the most recently received `Data` frame pending and attaches the next `RxMeta`
to it before emitting the combined event; a `Data` frame that never gets a following `RxMeta`
(e.g. the run ends) is still emitted, with `rx_meta: null`, rather than held forever.
- Alternative considered: sequence-number or timestamp-window matching. Rejected — the
  protocol doesn't need it (ordering is guaranteed) and it adds failure modes (mismatched
  pairs under clock skew) for no benefit.

### Reconnect: exponential backoff, unbounded retries, capped interval
On serial read/write failure: log the disconnect as its own wide event, close the handle,
retry open with backoff (0.5s, 1s, 2s, 4s... capped at 30s), and keep retrying indefinitely.
Re-run whatever startup `SetHardware` handshake is needed on every successful reconnect —
DESIGN.md §4.1 is explicit that the device must never be assumed to still be configured.
- This is an unattended overnight run; a bounded retry count that gives up means silently
  losing the rest of the night's capture with nobody there to notice or restart it.

### Device binding
Resolve the device path once at startup from a configured `/dev/serial/by-id/usb-...` path
(or equivalent stable identifier), per DESIGN.md §4.1. Fail fast with a clear error if that
path doesn't exist at startup — do not fall back to scanning `/dev/ttyUSB*`.

### Concurrency model
Single asyncio task pair: one reads and frames bytes off the serial link (`KissTransport`),
one consumes decoded frames and does correlation + persistence (`Modem` + capture sink).
Matches DESIGN.md §2's overall single-process asyncio decision — nothing here needs more.

### CLI shape
`sighop capture --device <path> --out <file> [--radio-preset eu868-narrow]`. Runs until
SIGINT/SIGTERM, then flushes and exits cleanly. Radio preset selects `SetRadio` parameters
applied at startup (default: EU/UK 868 narrow per DESIGN.md §2) — this milestone needs *a*
working radio config to receive anything, even though the scheduler that reasons about
airtime doesn't exist yet.

### Testing without burning the live mesh
`KissTransport` is built against an injectable byte stream (an `asyncio.StreamReader`/
`StreamWriter` pair), so unit tests drive it with synthetic KISS-framed bytes rather than
real hardware. The overnight live-mesh run is a manual validation step (tracked in tasks.md),
not something CI can exercise — there is no test peer board committed until milestone 4.

## Risks / Trade-offs

- **[Risk]** An overnight run with a subtle framing bug produces a large capture file of
  garbage and nobody notices until morning.
  → **Mitigation**: log a periodic wide-event heartbeat (frame count, malformed count,
  reconnect count) at a fixed interval so a `tail -f` or morning log scan catches a
  degenerate run early; unit-test the framing/escaping and correlation logic against known
  KISS byte sequences before ever touching the real board.

- **[Risk]** JSONL is not the most compact format for a multi-hour capture at mesh traffic
  volumes.
  → **Mitigation**: accepted trade-off — at LoRa airtime rates, even an hour of continuous
  traffic is a small file; human-inspectability and streaming-append safety matter more here
  than size.

- **[Risk]** The `Data`-then-`RxMeta` ordering assumption is wrong for some frame type or
  firmware edge case, silently mispairing metadata.
  → **Mitigation**: if a second `Data` frame arrives before an `RxMeta` for the first, emit
  the first with `rx_meta: null` and log a wide event flagging the anomaly, rather than
  guessing — this is exactly the kind of odd real-world frame DESIGN.md §12 wants surfaced
  as a capture-time signal, not hidden.

## Open Questions

- Exact reconnect backoff constants (0.5s/30s cap above) are a starting point, not tuned —
  revisit if the overnight run shows either flapping bursts or slow recovery.
- Whether a single capture file or size/time-rotated files is better for a multi-hour run is
  left to implementation; JSONL append makes either trivial, so it doesn't need deciding now.
