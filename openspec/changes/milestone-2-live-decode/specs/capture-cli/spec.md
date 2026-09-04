## ADDED Requirements

> Reference: DESIGN.md §12 "Capture format and provenance" is authoritative for what the header
> record must carry and for the rule that its values are recorded, never inferred from config.

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
