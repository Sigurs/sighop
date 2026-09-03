## 1. Project scaffolding

- [x] 1.1 Initialize `pyproject.toml` with `uv`, Python 3.13, and a `sighop` CLI entry point (per DESIGN.md §11 layout: `src/sighop/`)
- [x] 1.2 Add `src/sighop/radio/`, `src/sighop/logging.py`, `src/sighop/cli.py` module skeletons
- [x] 1.3 Add `structlog` dependency and wire up JSON wide-event logging config per `logging-best-practices` (service, version, commit_hash, instance_id, event_type, duration_ms, outcome context)
- [x] 1.4 Set up `tests/` with pytest and a `uv run pytest` target

## 2. KISS transport (`kiss-transport` capability)

- [x] 2.1 Implement KISS frame decoding: FEND-delimited scanning with FESC/TFEND/TFESC unescaping
- [x] 2.2 Implement KISS frame encoding (needed for the startup `SetHardware`/`SetRadio` handshake, even though this milestone has no user-facing TX)
- [x] 2.3 Implement malformed-frame detection (dangling escape byte, etc.) that reports rather than raises or drops
- [x] 2.4 Build `KissTransport` against an injectable `asyncio.StreamReader`/`StreamWriter` pair so it's testable without hardware
- [x] 2.5 Implement stable device path resolution (`/dev/serial/by-id/...`) with fail-fast startup error on a missing path
- [x] 2.6 Implement reconnect loop: detect read/write failure, close handle, exponential backoff (0.5s start, 30s cap), retry indefinitely, log disconnect/reconnect as wide events
- [x] 2.7 Unit tests: well-formed frames, escaped FEND/FESC bytes, empty frames between delimiters, dangling-escape malformed frames, encode round-trip

## 3. Modem RX layer (`modem-rx` capability)

- [x] 3.1 Implement `Data` (0x00) frame recognition and raw payload extraction
- [x] 3.2 Implement `RxMeta` (0xF9) parsing: signed SNR (×0.25 dB) and signed RSSI (dBm)
- [x] 3.3 Implement Data/RxMeta correlation: hold the pending `Data` frame, attach the next `RxMeta`, emit
- [x] 3.4 Handle the second-Data-before-RxMeta anomaly: emit the first frame with `rx_meta: null`, log a wide event flagging it, track the second
- [x] 3.5 Handle end-of-run with a frame still pending correlation: emit it with `rx_meta: null` rather than dropping it
- [x] 3.6 Implement startup handshake: apply configured radio params via `SetRadio`, confirm response (OK/Error, per the actual protocol doc — `Set*` commands ack via generic `OK`/`Error`, not a `request|0x80` response code) before signaling ready
- [x] 3.7 Re-run the startup handshake on every transport reconnect (never assume prior config survived)
- [x] 3.8 Implement unparsed-frame reporting for unrecognized command bytes and transport-reported malformed frames
- [x] 3.9 Unit tests: Data/RxMeta correlation ordering, negative SNR/RSSI parsing, anomaly path, startup handshake response matching, unparsed frame passthrough

## 4. `sighop capture` CLI (`capture-cli` capability)

- [x] 4.1 Implement `sighop capture --device <path> --out <file> [--radio-preset eu868-narrow]` argument parsing with clear errors on missing required args
- [x] 4.2 Implement JSONL record writer: ISO-8601 timestamp, kind (`rx_frame`/`unparsed`), raw bytes as hex, RxMeta (or explicit `null`)
- [x] 4.3 Implement append + flush per record for crash-safety (verify: kill -9 mid-run leaves only complete trailing lines plus at most one partial)
- [x] 4.4 Implement graceful shutdown on SIGINT/SIGTERM: stop accepting frames, flush, close file, exit 0
- [x] 4.5 Implement periodic heartbeat wide-event log (RX count, unparsed count, reconnect count) at a fixed interval
- [x] 4.6 Wire the CLI command into the entry point defined in 1.1
- [x] 4.7 Integration test: drive the full pipeline (fake transport → modem → capture writer) against a synthetic byte stream and assert the resulting JSONL matches expected records

## 5. Live validation

- [x] 5.1 Confirm the default EU/UK 868 narrow radio preset applies cleanly via `SetRadio` against the real Heltec V3 board — SetRadio accepted the first (guessed) values, 869.525 MHz/SF7/CR5, but a live smoke test received nothing; corrected to the community-confirmed 869.618 MHz/BW62.5/SF8/CR8 per [meshcore.ch/settings](https://www.meshcore.ch/settings/)
- [x] 5.2 Run `sighop capture` against the live mesh for at least several hours (target: overnight) — two runs: `2026-09-02.jsonl` (9h16m, overnight) and `2026-09-03.jsonl` (~7h37m)
- [x] 5.3 Review the run's heartbeat logs and capture file for reconnect events, malformed/unparsed frame rates, and any Data/RxMeta correlation anomalies — both runs clean: 0 reconnects, 0 unparsed/malformed, 0 correlation anomalies
- [x] 5.4 Confirm the capture file parses cleanly end-to-end (every complete line is valid JSON) and spot-check a sample of decoded RxMeta values against expectations — all 351 records across both files parse as valid JSON; SNR values are clean 0.25 dB multiples, RSSI values plausible negative dBm
- [x] 5.5 Archive the resulting capture file as the regression corpus milestone 1 will build against — kept as two separate captures with sidecar `.meta.json` provenance files (`captures/2026-09-02.meta.json`, `captures/2026-09-03.meta.json`) per DESIGN.md §12
