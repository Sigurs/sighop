## Requirements

> Reference: the `capture-cli` capability defines the record format this capability reads back.
> The two are inverses and must be changed together.

### Requirement: Capture files replay as modem events
The system SHALL read a capture JSONL file and produce the same event types the modem produces
from a live link, in file order, so that a consumer of modem events can be driven by a capture
file without modification.

#### Scenario: Replaying a recorded RX frame
- **WHEN** a capture record of kind `rx_frame` is read
- **THEN** the replay source emits an RX event carrying the recorded packet bytes and, when present, the recorded SNR and RSSI

#### Scenario: Replaying a recorded unparsed frame
- **WHEN** a capture record of kind `unparsed` is read
- **THEN** the replay source emits an unparsed-frame event carrying the recorded raw bytes and reason

#### Scenario: Recorded RxMeta was absent
- **WHEN** a capture record of kind `rx_frame` has a null RxMeta field
- **THEN** the emitted RX event carries no SNR or RSSI, matching a live frame that arrived without correlated metadata

### Requirement: Provenance header is surfaced, not decoded
The system SHALL treat a leading `capture_meta` record as the file's provenance and make it
available to the caller, and SHALL NOT attempt to decode it as a frame.

#### Scenario: File begins with a provenance header
- **WHEN** a capture file's first line is a `capture_meta` record
- **THEN** the replay source exposes it as the file's provenance and emits no frame event for it

#### Scenario: File has no provenance header
- **WHEN** a capture file contains no `capture_meta` record, as the pre-existing capture files do
- **THEN** replay proceeds over its frames and reports the provenance as absent

### Requirement: Malformed capture lines are reported, not skipped
The system SHALL report any line it cannot read — invalid JSON, an unknown `kind`, or a record
missing required fields — identifying the line, and SHALL NOT silently omit it from the replay.

#### Scenario: Truncated trailing line
- **WHEN** a capture file's final line is incomplete because the capturing process was killed mid-write
- **THEN** replay reports that line as unreadable, identifying its position, and completes the replay of the preceding records

#### Scenario: Unrecognized record kind
- **WHEN** a capture record carries a `kind` value the replay source does not recognize
- **THEN** replay reports it as unreadable rather than skipping it silently

### Requirement: Replay uses recorded timestamps and does not pace itself
The system SHALL attribute each replayed event the timestamp recorded in its capture record
rather than the current wall-clock time, and SHALL replay records as fast as they can be read
rather than reproducing the original inter-frame intervals.

#### Scenario: Replaying an overnight capture
- **WHEN** a capture file spanning several hours is replayed
- **THEN** every event carries its originally recorded timestamp and the replay completes without waiting out the recorded gaps
