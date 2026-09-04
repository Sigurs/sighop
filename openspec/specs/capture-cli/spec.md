## Requirements

> Reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md` is the authoritative KISS
> modem protocol doc underlying the RX events this command persists.

> Reference: DESIGN.md §12 "Capture format and provenance" is authoritative for what the header
> record must carry and for the rule that its values are recorded, never inferred from config.

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

### Requirement: Capture provenance header record
The system SHALL write a provenance header record as the first line of every capture file it
creates, of a kind distinguishable from frame records, carrying at minimum the device name,
radio parameters, transmit power and firmware version as reported by the board, the telemetry
probe results, and the sighop version and commit.

#### Scenario: A new capture begins
- **WHEN** the capture command opens a new output file and completes its startup probe
- **THEN** the first line written is a provenance header record carrying the probed board values and the sighop version and commit

#### Scenario: Header precedes any frame
- **WHEN** a frame is received before the provenance header has been written
- **THEN** the header is still the first line in the file, ahead of that frame's record

### Requirement: Provenance values are observed, never inferred
The system SHALL populate the header record only from values the board reported, and SHALL
record a value the board did not report as explicitly absent together with its reason, rather
than omitting the field or substituting the configured value.

#### Scenario: Board did not answer a telemetry sub-command
- **WHEN** a probed sub-command was rejected or timed out
- **THEN** the header record contains that field with an explicit null and a reason, and no configured or default value in its place

#### Scenario: Configured and read-back radio parameters differ
- **WHEN** the radio parameters read back from the board differ from those the operator configured
- **THEN** the header record carries the read-back values as the observed ones and records the configured values separately

### Requirement: Appending to an existing capture file
The system SHALL write the provenance header only when it creates a new capture file, and when
appending to a file that already contains records SHALL NOT write a second header, so that a
capture file has at most one header and it is the first line.

#### Scenario: Output file already contains records
- **WHEN** the capture command is pointed at an output file that is not empty
- **THEN** it appends frame records without writing a further header record
