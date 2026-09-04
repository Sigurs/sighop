## Why

Milestone 0 gave sighop ears; milestone 1 gave it a decoder, verified offline against 351
recorded frames. The two have never met. DESIGN.md §12 calls milestone 2 the point where
"the design is proven or isn't" — pointing the codec at the live link, in real time, is
the first end-to-end evidence that `radio/` and `protocol/` compose into a working receiver
rather than two separately-green test suites.

It is also the last milestone that is receive-only by construction. Everything after it
(bus, scheduler, transmit) is built on the assumption that we can read the mesh correctly;
if that assumption is wrong, this is the cheapest possible place to find out.

## What Changes

- **New `sighop monitor` command** — opens the live link (or replays a capture file) and
  prints one dense fixed-width line per received packet: time, payload type, route type, hop
  count, path, SNR/RSSI, and the decoded content. Adverts show their verified name, node type
  and flags; encrypted payloads show the envelope shape and are marked as un-openable. A
  periodic summary line reports counts. JSON wide events continue to go to the log file.
- **New RX decode pipeline** (`net/rx.py`) — the seam between a `ModemEvent` and a decoded
  record: structural decode, payload parse, advert signature verification, and the DESIGN.md
  §9 *Packet RX* wide event with `packet_id` minted at ingress. Stateless: no dedup, no path
  learning, no contacts (milestone 3 and 5). Every failure at every stage is an event, never
  a silent drop.
- **New replay source** (`radio/replay.py`) — re-hydrates modem events from a capture JSONL
  file, so the identical pipeline runs against recorded frames. This makes the live path
  testable offline and lets the milestone 0 corpus be replayed end-to-end through the code
  that will actually run on air.
- **New modem probe** (`radio/probe.py`) — a `SetHardware` request/response API on `Modem`
  (`response = request | 0x80`, per the protocol doc), and a startup probe that asks the
  board what it is: `GetDeviceName`, `GetRadio` readback, `GetTxPower`, `GetVersion`, plus the
  optional telemetry sub-commands `GetBattery`, `GetMCUTemp`, `GetSensors`. Per DESIGN.md §4.1
  every one of these is optional and probe-detected — an `Error`/`UnknownCmd` response records
  the sub-command as unavailable and is never fatal.
- **`sighop capture` writes the `capture_meta` header record** DESIGN.md §12 requires, built
  from the probe result plus the sighop version and commit. New captures therefore carry their
  own provenance; the two existing files keep their sidecar `.meta.json` and are untouched.
- **Receive-only remains true, and is now worth saying precisely.** The probe writes to the
  serial port but originates no radio frame: every sub-command it sends is a query or the
  existing `SetRadio`. `SetTxPower`, `Data` and `Reboot` are not sent by any code in this
  change.

## Capabilities

### New Capabilities
- `modem-probe`: the `SetHardware` request/response exchange on top of the RX frame stream —
  request/response correlation by the `| 0x80` convention, timeouts, and a startup probe that
  records what each board answers for device name, radio parameters, TX power, firmware
  version and the optional telemetry sub-commands, treating an unsupported sub-command as a
  recorded absence rather than an error.
- `rx-decode`: the stateless live decode stage — modem RX event to decoded packet, payload and
  verified advert, with a structured outcome for every frame including the ones that fail to
  decode, and the DESIGN.md §9 *Packet RX* wide event it emits.
- `capture-replay`: reading a capture JSONL file back into the same event stream the modem
  produces, so recorded frames can drive the live pipeline unchanged.
- `monitor-cli`: the `sighop monitor` command — live or replay source selection, the
  line-oriented rendering of each decoded frame, the periodic summary, and the rule that
  unverified content is never rendered as if verified.

### Modified Capabilities
- `capture-cli`: adds the `capture_meta` header record as the first line of every new capture
  file, sourced from the live probe and never from configuration.
- `modem-rx`: the startup handshake gains the probe step, and the frame loop must now route
  `SetHardware` responses to a waiting requester instead of reporting every one of them as an
  unparsed frame.

## Impact

- **New code:** `src/sighop/net/` (`rx.py`), `src/sighop/monitor/` (`run.py`, `render.py`),
  `src/sighop/radio/probe.py`, `src/sighop/radio/replay.py`.
- **Modified code:** `src/sighop/radio/modem.py` (request/response correlation),
  `src/sighop/radio/capture.py` (header record), `src/sighop/cli.py` (the `monitor`
  subcommand).
- **DESIGN.md §11** gains `monitor/` and the `net/rx.py` split; the repository-layout sketch
  currently shows neither. Updated in this change, per DESIGN.md's own rule.
- **No new dependencies.** Line-oriented output needs no rendering library; that was the point
  of choosing it over a full-screen panel.
- **`protocol/` is not touched.** Its import boundary test (`tests/protocol/test_import_boundary.py`)
  must keep passing — the dependency runs one way, from `net/` down to `protocol/`.
- **Two milestone 1 open questions become answerable here.** Whether any repeater on this mesh
  mixes path hash sizes across a packet's lifetime, and whether live traffic contains frame
  shapes the 351-frame corpus never held. Both are observations this milestone can make and
  neither is answerable offline.
- **The corpus grows.** `sighop monitor --capture <file>` records while it decodes, so a live
  session produces a properly-headered capture that can be appended to the regression corpus —
  which milestone 1's design explicitly anticipated.
- **Out of scope, deliberately:** dedup and path learning, the bus, the TX scheduler and
  time-on-air/airtime budgeting (milestone 3); any database write (milestone 5); anything that
  keys the transmitter (milestone 4). The monitor's contact view is in-memory and dies with
  the process — persisting it is milestone 5's job, not a shortcut to take here.
