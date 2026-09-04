# modem-tx Specification

## Purpose
How a packet is handed to the KISS modem for transmission and how that transmission resolves —
against `TxDone`, a busy rejection or a timeout — with at most one outstanding at a time and
reception continuing throughout.
## Requirements
### Requirement: A packet is transmitted by submitting a Data frame
The system SHALL transmit a packet by sending it as a `Data` frame over the transport, and SHALL
reject a packet exceeding the modem's maximum frame size before sending rather than letting the
transport truncate it.

#### Scenario: Packet within the size limit
- **WHEN** a packet of at most 255 bytes is submitted for transmission
- **THEN** a `Data` frame carrying exactly those bytes is written to the transport

#### Scenario: Oversized packet
- **WHEN** a packet larger than the modem's maximum frame size is submitted
- **THEN** the submission is rejected with an error naming the limit and nothing is written to the transport

### Requirement: A transmission resolves against TxDone, TxBusy or a timeout
The system SHALL resolve each submitted transmission by the modem's `TxDone` response, by a busy
rejection, or by a timeout, and SHALL distinguish the three outcomes to the caller. The timeout
SHALL be longer than the modem's own transmission timeout for that packet length, so that a modem
still working on a packet is not abandoned early.

#### Scenario: Successful completion
- **WHEN** the modem answers `TxDone` with a success result
- **THEN** the submission resolves as transmitted

#### Scenario: Failed completion
- **WHEN** the modem answers `TxDone` with a failure result
- **THEN** the submission resolves as failed, distinctly from a timeout

#### Scenario: Modem reports busy
- **WHEN** the modem answers with the busy error rather than accepting the packet
- **THEN** the submission resolves as busy, and the packet is reported as not transmitted

#### Scenario: No answer at all
- **WHEN** neither `TxDone` nor an error arrives within the timeout
- **THEN** the submission resolves as timed out, and the modem is returned to a state where the next submission is accepted

### Requirement: At most one transmission is outstanding at the modem
The system SHALL enforce the one-in-flight invariant at the modem itself, such that a second
submission attempted while one is outstanding waits rather than being written to the transport.

#### Scenario: Concurrent submissions
- **WHEN** two callers submit transmissions concurrently
- **THEN** only one `Data` frame is written until the first resolves, and the second is written afterwards

#### Scenario: Reconnect with a transmission outstanding
- **WHEN** the transport disconnects while a transmission is outstanding
- **THEN** the outstanding submission resolves as failed rather than waiting for a `TxDone` that cannot arrive, and the invariant is released

### Requirement: Reception continues while a transmission is outstanding
The system SHALL continue emitting RX events, `RxMeta` correlation and `SetHardware` responses
while awaiting a `TxDone`, so that awaiting a transmission does not stall the receive path.

#### Scenario: Frame received while awaiting TxDone
- **WHEN** a `Data` frame arrives from the modem while a transmission is outstanding
- **THEN** it is emitted as an RX event as usual and the outstanding transmission remains outstanding

#### Scenario: RxMeta arrives between submission and TxDone
- **WHEN** an `RxMeta` frame arrives while a transmission is outstanding
- **THEN** it is correlated with the preceding `Data` frame as usual and is not mistaken for the transmission's completion

