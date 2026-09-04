## MODIFIED Requirements

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
