## ADDED Requirements

> Reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md` is authoritative for the
> `SetHardware` sub-command codes, the `response = request | 0x80` convention, and the error
> codes cited below. DESIGN.md §4.1 is authoritative for the probe-detection rule.

### Requirement: SetHardware request/response exchange
The system SHALL provide a request API that sends a `SetHardware` sub-command and resolves with
the modem's matching response, correlating request to response by the `response = request | 0x80`
convention, and SHALL do so from within the single frame-consuming loop rather than by opening a
second reader over the transport.

#### Scenario: Request resolves with its matching response
- **WHEN** a `SetHardware` sub-command is requested and the modem replies with the corresponding `request | 0x80` response code
- **THEN** the request resolves with that response's data bytes

#### Scenario: Request is rejected by the modem
- **WHEN** a `SetHardware` sub-command is requested and the modem replies with `Error` (`0xF1`)
- **THEN** the request resolves as a failure carrying the modem's error code rather than raising out of the frame loop

#### Scenario: Modem does not answer
- **WHEN** a `SetHardware` sub-command is requested and no matching response or `Error` arrives within the request timeout
- **THEN** the request resolves as a timeout failure and the frame loop continues processing subsequent frames

#### Scenario: A second request while one is outstanding
- **WHEN** a request is issued while another request is still awaiting its response
- **THEN** the system raises a programming error rather than queueing or silently discarding either request

### Requirement: Startup probe of board capabilities
The system SHALL, at startup and after every reconnect, query the modem for its device name,
radio parameters, transmit power and firmware version, and SHALL additionally attempt the
optional telemetry sub-commands `GetBattery`, `GetMCUTemp` and `GetSensors`, recording the
result of each.

#### Scenario: Board answers every probe
- **WHEN** the modem responds successfully to each probed sub-command
- **THEN** the probe result carries the parsed device name, radio parameters, transmit power, firmware version and telemetry values

#### Scenario: Board does not support a sub-command
- **WHEN** the modem answers a probed sub-command with `Error` and the `UnknownCmd` or `NoCallback` code
- **THEN** the probe result records that sub-command as unavailable together with the reported error code, and probing continues with the remaining sub-commands

#### Scenario: A probed sub-command times out
- **WHEN** the modem does not answer a probed sub-command within the request timeout
- **THEN** the probe result records that sub-command as timed out and probing continues with the remaining sub-commands

#### Scenario: Every probe fails
- **WHEN** no probed sub-command produces a response
- **THEN** startup completes anyway with a probe result recording every sub-command as absent, and the system proceeds to receive frames normally

### Requirement: Probe results distinguish observation from absence
The system SHALL represent an unanswered probe as a structured absence carrying the reason,
distinct from both a successful value and from a value the operator configured, and SHALL NOT
substitute configured or assumed values for values the board did not report.

#### Scenario: Device name was not answered
- **WHEN** the probe result is consumed by a caller after `GetDeviceName` went unanswered
- **THEN** the caller observes an explicit absence with its reason, and no configured or default board name is supplied in its place

### Requirement: Radio parameter readback comparison
The system SHALL compare the radio parameters read back via `GetRadio` against the parameters it
applied via `SetRadio`, and SHALL emit an error-level wide event naming both when they differ.

#### Scenario: Readback matches the applied configuration
- **WHEN** `GetRadio` returns the same frequency, bandwidth, spreading factor and coding rate that were applied
- **THEN** the system records the confirmed radio parameters and emits no mismatch event

#### Scenario: Readback differs from the applied configuration
- **WHEN** `GetRadio` returns any radio parameter differing from the applied value
- **THEN** the system emits an error-level wide event carrying both the applied and the read-back parameters, and continues receiving

### Requirement: Sensor payload is recorded uninterpreted
The system SHALL record the `GetSensors` response as raw bytes and SHALL NOT parse it against a
fixed schema, since its shape depends on the board's build flags.

#### Scenario: Sensors respond with a CayenneLPP buffer
- **WHEN** the modem answers `GetSensors` with a CayenneLPP payload
- **THEN** the probe result carries the raw payload bytes without decoding them into named sensor values

### Requirement: The probe originates no radio transmission
The system SHALL restrict probing to query sub-commands and the existing `SetRadio`
configuration command, and SHALL NOT send `Data`, `SetTxPower` or `Reboot`.

#### Scenario: Probing a live modem
- **WHEN** the startup probe runs against a connected modem
- **THEN** the frames sent to the modem consist only of `SetRadio` and query sub-commands, and no packet is queued for radio transmission
