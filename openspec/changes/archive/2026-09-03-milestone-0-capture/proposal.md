## Why

sighop's entire protocol stack (milestone 1 onward) is being designed against a written
reading of the MeshCore spec, not against real frames. Milestone 0 closes that gap cheaply:
open the KISS serial link, dump everything the modem hears — with `RxMeta` and timestamps —
to a file, and let it run overnight against the live mesh. Milestone 1 then gets built and
tested against real captured traffic and becomes the permanent regression corpus, turning
early protocol bugs from late surprises into fixtures.

## What Changes

- New `KissTransport`: FEND/FESC framing and escaping over the 115200 8N1 serial link.
  Framing and escaping only — no protocol semantics.
- New `Modem` layer above it: correlates each `RxMeta` (0xF9) frame with the `Data` (0x00)
  frame it follows, and does enough startup `SetHardware` handling to bring the link up.
  No TX path, no scheduler — this milestone never transmits.
- Reconnect handling: the Heltec V3's USB-serial link is the only channel (no spare debug
  UART), so a dropped/replugged adapter must reconnect with backoff rather than end the run.
- New `sighop capture` CLI command: opens the modem, writes every received frame (raw bytes,
  correlated RxMeta, wall-clock timestamp, and any frames that fail to parse) to an
  append-only capture file, and runs indefinitely (overnight) until stopped.
- Wide-event logging at the transport boundary for every frame, including malformed ones —
  per DESIGN.md §4.1, this is the only observability into the radio layer on this board.
- Device binding by stable path (`/dev/serial/by-id/usb-...`), not `/dev/ttyUSB0`.
- Receive-only: no TX code path exists yet. There is nothing to gate because there is
  nothing capable of transmitting.

## Capabilities

### New Capabilities
- `kiss-transport`: KISS framing/escaping over the serial link, plus the reconnect-with-backoff loop that keeps it alive unattended.
- `modem-rx`: modem-level receive semantics — decoding `Data` frames, correlating `RxMeta`, and the startup probe handshake needed to bring the link up. No TX.
- `capture-cli`: the `sighop capture` command that consumes modem-rx output and durably persists it (raw frame, RxMeta, timestamp) to a file, unattended, over long runs.

### Modified Capabilities
(none — this is the first change in the project; `openspec/specs/` is currently empty)

## Impact

- Reference: the authoritative KISS modem protocol doc for this change is vendored at
  `related-repos/MeshCore/docs/kiss_modem_protocol.md` — consult it directly for frame
  layouts, command bytes, and `SetHardware` semantics rather than re-deriving them from
  DESIGN.md's summary.
- New code: `src/sighop/radio/` (kiss transport, modem, framing), `src/sighop/logging.py`
  (structlog wide-event setup), `src/sighop/cli.py` (the `capture` subcommand).
- New dependency: a packaging/CLI entry point (`sighop capture`) per DESIGN.md's `uv`-based
  packaging decision.
- No database, no web server, no cryptography, no TX scheduler — all out of scope for this
  milestone per DESIGN.md §12.
- Operational: requires the Heltec V3 board attached over USB, KISS mode active, and a
  multi-hour unattended run against the live mesh to produce the capture file that milestone
  1 depends on.
