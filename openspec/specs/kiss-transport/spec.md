# kiss-transport Specification

## Purpose
The serial link to the modem: KISS frame decoding and encoding with their escaping rules, what
happens to a frame that will not decode, and reconnect with backoff against a device path that
stays stable across replug.
## Requirements

> Reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md` is the authoritative KISS
> modem protocol doc for the frame/escaping rules below.

### Requirement: KISS frame decoding
The system SHALL decode KISS-framed bytes read from a serial stream into discrete frames by
locating `FEND` (0xC0) delimiters and reversing `FESC`/`TFEND`/`TFESC` (0xDB/0xDC/0xDD)
escaping, per the KISS protocol.

#### Scenario: Well-formed frame
- **WHEN** a byte stream containing a single `FEND`-delimited frame with no escaped bytes is read
- **THEN** the transport emits one decoded frame containing exactly the unescaped payload bytes

#### Scenario: Frame containing an escaped FEND byte
- **WHEN** the incoming stream contains `FESC TFEND` inside a frame's payload
- **THEN** the decoded frame contains a literal `0xC0` byte at that position, not a frame boundary

#### Scenario: Frame containing an escaped FESC byte
- **WHEN** the incoming stream contains `FESC TFESC` inside a frame's payload
- **THEN** the decoded frame contains a literal `0xDB` byte at that position

#### Scenario: Empty frame between consecutive FEND bytes
- **WHEN** two `FEND` bytes appear with no payload bytes between them
- **THEN** the transport emits no frame for that gap and continues scanning

### Requirement: KISS frame encoding
The system SHALL encode outbound frame bytes into KISS format, applying `FESC` escaping to
any literal `FEND` or `FESC` byte in the payload and delimiting the result with `FEND` bytes.

#### Scenario: Encoding a payload containing a literal FEND byte
- **WHEN** a frame payload contains a `0xC0` byte
- **THEN** the encoded output replaces it with `FESC TFEND` and wraps the whole frame in `FEND` delimiters

### Requirement: Malformed frame handling
The system SHALL treat a frame that fails to decode (e.g. a dangling escape byte at frame end)
as a distinct, reportable event rather than silently discarding it or raising an unhandled
exception that stops the transport.

#### Scenario: Dangling escape byte at end of frame
- **WHEN** a frame ends with `FESC` not followed by `TFEND` or `TFESC`
- **THEN** the transport reports the frame as malformed, including its raw bytes, and continues reading subsequent frames

### Requirement: Serial link reconnect with backoff
The system SHALL detect a serial read or write failure, close the affected handle, and retry
opening the configured device path with exponential backoff, retrying indefinitely rather than
giving up after a bounded number of attempts.

#### Scenario: USB adapter disconnects mid-run
- **WHEN** a read or write on the serial handle fails because the device has been unplugged
- **THEN** the transport logs the disconnect as a wide event, closes the handle, and begins retrying the open with increasing backoff intervals up to a capped maximum

#### Scenario: Device reappears after backoff
- **WHEN** the configured device path becomes openable again during a backoff retry
- **THEN** the transport successfully reconnects and resumes emitting decoded frames, and logs the reconnect as a wide event

### Requirement: Stable device path binding
The system SHALL open the serial device using an operator-configured stable path (e.g. a
`/dev/serial/by-id/...` identifier) and SHALL fail fast with a clear error at startup if that
exact path does not exist, rather than falling back to scanning for other serial devices.

#### Scenario: Configured device path does not exist at startup
- **WHEN** the transport is started with a device path that does not exist on the filesystem
- **THEN** the transport raises a clear, immediately-visible startup error and does not attempt to scan for alternative serial devices
