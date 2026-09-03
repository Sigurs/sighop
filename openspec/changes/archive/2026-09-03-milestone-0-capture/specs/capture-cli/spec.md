## ADDED Requirements

> Reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md` is the authoritative KISS
> modem protocol doc underlying the RX events this command persists.

### Requirement: `sighop capture` command
The system SHALL provide a `sighop capture` CLI command that opens the configured modem,
persists every RX and unparsed-frame event to an output file, and runs continuously until
stopped by the operator.

#### Scenario: Command starts and begins capturing
- **WHEN** the operator runs `sighop capture --device <path> --out <file>`
- **THEN** the command opens the modem at the given device path and begins writing captured events to the given output file

#### Scenario: Missing required arguments
- **WHEN** the operator runs `sighop capture` without a device path or output file
- **THEN** the command exits immediately with a clear error describing the missing argument, without attempting to open any device

### Requirement: Capture record format
The system SHALL write one JSON object per line (JSONL) to the output file for each received
event, including a wall-clock timestamp, the event kind, the raw frame bytes, and the
correlated RxMeta values when present.

#### Scenario: RX frame with RxMeta
- **WHEN** the modem emits an RX event with correlated SNR and RSSI
- **THEN** the capture command appends one JSON line containing an ISO-8601 timestamp with timezone, the raw packet bytes, and the SNR/RSSI values

#### Scenario: RX frame without RxMeta
- **WHEN** the modem emits an RX event with no correlated RxMeta
- **THEN** the capture command appends one JSON line with the RxMeta field explicitly null rather than omitting it

#### Scenario: Unparsed frame
- **WHEN** the modem emits an unparsed-frame event
- **THEN** the capture command appends one JSON line marked with a kind distinguishing it from a decoded RX frame, including the raw bytes available

### Requirement: Append-only, crash-safe writes
The system SHALL append each capture record to the output file and flush it before processing
the next event, so that a process interruption loses at most the in-flight record and never
corrupts previously written lines.

#### Scenario: Process is killed mid-run
- **WHEN** the capture process is terminated abruptly after writing N complete records
- **THEN** the output file contains exactly those N complete, valid JSON lines, with at most one additional incomplete trailing line

### Requirement: Graceful shutdown
The system SHALL, on receiving `SIGINT` or `SIGTERM`, stop accepting new frames, flush and
close the output file, and exit with a success status.

#### Scenario: Operator stops the capture with Ctrl-C
- **WHEN** the operator sends `SIGINT` to a running capture process
- **THEN** the process flushes any buffered output, closes the file cleanly, and exits with status 0

### Requirement: Periodic heartbeat logging
The system SHALL log a wide-event heartbeat at a fixed interval while capturing, including the
running counts of RX frames, unparsed frames, and reconnects observed so far.

#### Scenario: Heartbeat during a long run
- **WHEN** the capture command has been running for longer than the heartbeat interval
- **THEN** a heartbeat log event has been emitted containing the current RX, unparsed, and reconnect counts
