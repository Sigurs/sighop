# modem-rx Specification

## Purpose
How the KISS modem surfaces what the radio receives: decoding `Data` frames, correlating the
`RxMeta` that describes them, the startup handshake that establishes radio parameters, and
reporting any frame it cannot account for rather than dropping it.
## Requirements

> Reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md` is the authoritative KISS
> modem protocol doc for `Data`/`RxMeta` command bytes and `SetHardware` semantics below.

### Requirement: Data frame decoding
The system SHALL recognize a KISS `Data` frame (command byte `0x00`) delivered by the
transport and extract its payload as a raw MeshCore packet's bytes, without interpreting the
MeshCore packet header, transport codes, path, or payload contents.

#### Scenario: Data frame received
- **WHEN** the transport emits a decoded frame whose first byte is the `Data` command (`0x00`)
- **THEN** the modem layer emits an RX event carrying the remaining bytes as the raw packet, unparsed beyond the KISS command byte

### Requirement: RxMeta correlation
The system SHALL correlate an `RxMeta` (command byte `0xF9`) frame with the `Data` frame that
immediately preceded it, attaching the parsed SNR and RSSI values to that frame's RX event
before emitting it.

#### Scenario: RxMeta immediately follows its Data frame
- **WHEN** a `Data` frame is received and the next frame from the transport is an `RxMeta` frame
- **THEN** the emitted RX event for that `Data` frame includes the SNR and RSSI parsed from the `RxMeta` frame

#### Scenario: A second Data frame arrives before RxMeta for the first
- **WHEN** a `Data` frame is received and another `Data` frame arrives before any `RxMeta` frame
- **THEN** the modem emits the first `Data` frame's RX event with no RxMeta attached, logs a wide event flagging the anomaly, and begins tracking the second `Data` frame for correlation

#### Scenario: Run ends with a Data frame still pending correlation
- **WHEN** the modem is stopped while a `Data` frame is held pending a following `RxMeta`
- **THEN** the modem emits that frame's RX event with no RxMeta attached rather than discarding it

#### Scenario: A SetHardware response arrives between a Data frame and its RxMeta
- **WHEN** a `Data` frame is being held for correlation and a `SetHardware` response that resolves an outstanding request arrives before the `RxMeta`
- **THEN** the modem resolves that request and continues holding the `Data` frame, so the subsequent `RxMeta` is still attached to it

### Requirement: SNR and RSSI value parsing
The system SHALL parse `RxMeta` SNR as a signed value scaled by 0.25 dB per unit and RSSI as a
signed dBm value, per the modem protocol's `RxMeta` encoding.

#### Scenario: Negative SNR and RSSI values
- **WHEN** an `RxMeta` frame encodes a negative signed SNR count and a negative signed RSSI byte
- **THEN** the parsed values reflect the correct negative dB/dBm figures, not their unsigned byte interpretation

### Requirement: Startup handshake
The system SHALL perform the `SetHardware` request/response exchange needed to confirm the modem
link is alive, apply the configured radio parameters (via `SetRadio`), and probe the board for
its identity, radio readback and available telemetry sub-commands before treating the link as
ready to receive, and SHALL redo this exchange on every reconnect.

#### Scenario: Successful startup
- **WHEN** the modem opens a serial connection for the first time
- **THEN** it applies the configured radio parameters via `SetRadio`, confirms a response, runs the startup probe, and signals the link is ready together with the probe result

#### Scenario: Reconnect after a transport-level disconnect
- **WHEN** the underlying transport reconnects after a disconnect
- **THEN** the modem re-applies the configured radio parameters rather than assuming the device retained its prior configuration, and re-runs the startup probe

#### Scenario: Probe fails but the link is alive
- **WHEN** `SetRadio` is accepted but one or more probe sub-commands go unanswered or are rejected
- **THEN** the link is still treated as ready and frames are received normally, with the unanswered sub-commands recorded as absent in the probe result

### Requirement: Unparseable frame reporting
The system SHALL emit a distinct event for any frame from the transport that is not a recognized
`Data`, `RxMeta`, `TxDone`, or `SetHardware` response — including transport-reported malformed
frames — rather than discarding it silently. A `SetHardware` response that resolves an outstanding
request SHALL be routed to that request instead of being reported as unparsed; a `TxDone` response
or a busy error that resolves an outstanding transmission SHALL likewise be routed to that
transmission; a `SetHardware` response, `TxDone` or busy error matching no outstanding request or
transmission SHALL still be reported as unparsed.

#### Scenario: Unrecognized command byte
- **WHEN** the transport emits a decoded frame whose command byte does not match `Data`, `RxMeta`, or a `SetHardware` response code
- **THEN** the modem emits an "unparsed frame" event carrying the raw frame bytes

#### Scenario: Transport reports a malformed frame
- **WHEN** the transport reports a frame it could not decode
- **THEN** the modem forwards this as an "unparsed frame" event carrying whatever raw bytes were available

#### Scenario: SetHardware response for an outstanding request
- **WHEN** a `SetHardware` response arrives whose code matches an outstanding request
- **THEN** the modem resolves that request with the response data and emits no unparsed-frame event

#### Scenario: Unsolicited SetHardware response
- **WHEN** a `SetHardware` response arrives that matches no outstanding request and is not `RxMeta`
- **THEN** the modem emits an "unparsed frame" event carrying the raw frame bytes

#### Scenario: TxDone for an outstanding transmission
- **WHEN** a `TxDone` response arrives while a transmission is outstanding
- **THEN** the modem resolves that transmission with the reported result and emits no unparsed-frame event

#### Scenario: Busy error for an outstanding transmission
- **WHEN** an error frame carrying the busy code arrives while a transmission is outstanding
- **THEN** the modem resolves that transmission as busy and emits no unparsed-frame event

#### Scenario: Unsolicited TxDone
- **WHEN** a `TxDone` response arrives with no transmission outstanding
- **THEN** the modem emits an "unparsed frame" event carrying the raw frame bytes, because a completion we did not initiate is evidence of a state we did not expect

