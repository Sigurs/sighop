# monitor-cli Specification

## Purpose
The `sighop monitor` command that renders live mesh traffic — adverts, names, paths, SNR — as it
arrives, with rendering kept separable from I/O, wide events continuing underneath, and the hard
rule that unverified content is never presented as verified.
## Requirements

> Reference: DESIGN.md §8's hard rule that unverified data is never presented as verified, and
> §12's statement of what milestone 2 must demonstrate — "adverts, names, paths and SNR printing
> in real time".

### Requirement: `sighop monitor` command
The system SHALL provide a `sighop monitor` CLI command that decodes received frames and prints
them as they arrive, reading either from a live modem or from a capture file, and running until
stopped by the operator.

#### Scenario: Monitoring a live link
- **WHEN** the operator runs `sighop monitor --device <path>`
- **THEN** the command opens the modem, probes it, and prints a rendered line for each frame received thereafter

#### Scenario: Replaying a capture file
- **WHEN** the operator runs `sighop monitor --replay <capture file>`
- **THEN** the command renders every frame in the file through the same decode path used for a live link, without opening any device

#### Scenario: Neither or both sources given
- **WHEN** the operator supplies neither `--device` nor `--replay`, or supplies both
- **THEN** the command exits immediately with an error naming the conflict, without opening any device or file

#### Scenario: Operator stops a live monitor
- **WHEN** the operator sends `SIGINT` or `SIGTERM` to a running monitor
- **THEN** the command prints a final summary, closes cleanly and exits with status 0

### Requirement: Startup line reports what the board is
The system SHALL print, before the first frame, a line identifying the probed device name,
firmware version, the confirmed radio parameters and transmit power, showing any unanswered
probe as explicitly unknown.

#### Scenario: Board answered the probe
- **WHEN** the monitor starts against a modem that answered the startup probe
- **THEN** the startup line names the device, firmware version, frequency, bandwidth, spreading factor, coding rate and transmit power as reported by the board

#### Scenario: Radio readback disagrees with the applied configuration
- **WHEN** the probe found the read-back radio parameters differ from those applied
- **THEN** the startup line states the mismatch prominently, showing both the applied and the read-back values

### Requirement: Per-frame rendering
The system SHALL render each decoded frame as fixed-width, line-oriented text carrying the
timestamp, payload type, route type, hop count, path, SNR and RSSI, followed by the decoded
content of that payload type. An encrypted payload's line SHALL state that decoding did not
decrypt it and that keys are held elsewhere, rather than reporting that no key is held: the
decode stage holds no keys of any kind, and the layers that do — direct messages and channels —
report their own outcome on their own line.

#### Scenario: A verified advert is received
- **WHEN** an ADVERT with a valid signature is decoded
- **THEN** the rendered output shows a verification mark, the advert's name, its node type and its flags

#### Scenario: An encrypted payload is received
- **WHEN** an encrypted payload is decoded
- **THEN** the rendered output shows the destination or channel hash, the MAC and the ciphertext length, and states that it was not decrypted at decode because keys are tried by the layer that holds them

#### Scenario: A payload another layer goes on to decrypt
- **WHEN** a group text frame on a loaded channel is decoded and then decrypted by the channel layer
- **THEN** the frame's own line does not report a missing key, and the channel layer's line carries the decrypted message

#### Scenario: A frame fails to decode
- **WHEN** a frame fails structural decode
- **THEN** the rendered output shows the violated rule, the offset and the raw bytes, rather than omitting the frame

### Requirement: Unverified content is never rendered as verified
The system SHALL NOT render the name or other content of an advert whose signature failed to
verify, and SHALL NOT use the presentation reserved for verified identities for any unverified
value.

#### Scenario: Advert with an invalid signature
- **WHEN** an ADVERT is decoded whose signature does not verify
- **THEN** the rendered output reports the verification failure and the public key, and does not print the advert's claimed name

### Requirement: Periodic summary
The system SHALL print a summary line at a fixed interval and once more at shutdown, reporting
at minimum the counts of frames received, frames that failed to decode, adverts verified, adverts
that failed verification, and reconnects observed.

#### Scenario: A live session runs past the summary interval
- **WHEN** the monitor has been running longer than the summary interval
- **THEN** a summary line has been printed carrying the current counts

#### Scenario: Replay completes
- **WHEN** a replay reaches the end of its capture file
- **THEN** a final summary line is printed covering the whole file, and the command exits

### Requirement: Rendering is separable from I/O
The system SHALL produce each rendered line by a pure function of a decoded record, so that
rendering can be exercised without a device, a file or an event loop.

#### Scenario: Rendering a decoded record in isolation
- **WHEN** a decoded record is passed to the renderer directly
- **THEN** the rendered text is produced with no device access, no file access and no asynchronous execution

### Requirement: Optional simultaneous capture
The system SHALL, when asked, write the frames it monitors to a capture file in the same format
`sighop capture` produces, including its provenance header.

#### Scenario: Monitoring while capturing
- **WHEN** the operator runs `sighop monitor --device <path> --capture <file>`
- **THEN** each received frame is both rendered and appended to the capture file, and the file begins with a provenance header built from the startup probe

### Requirement: Wide events continue alongside rendered output
The system SHALL emit the structured wide events for received frames to the log stream
independently of the rendered output, so that the human-readable rendering is not the only
record of a session.

#### Scenario: A frame is rendered
- **WHEN** the monitor renders a received frame to standard output
- **THEN** the corresponding *Packet RX* wide event is also emitted to the configured log stream
