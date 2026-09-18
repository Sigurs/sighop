## MODIFIED Requirements

### Requirement: Time-on-air is computed from live radio parameters
The system SHALL compute the LoRa time on air for a given payload length from the spreading
factor, bandwidth and coding rate **read back from the modem**, and SHALL NOT compute it from
configured or assumed preset values.

A board that has not answered its readback **yet** SHALL be distinguished from one that has not
answered at all. Where a transmission is composed before the readback has arrived, the system SHALL
wait for it within a bounded budget rather than refusing immediately; the refusal SHALL apply once
that budget expires. Waiting SHALL NOT weaken the rule above — a transmission is still never priced
against configured values — and a wait that succeeds SHALL NOT be reported as a refusal.

#### Scenario: Radio parameters read back from the board
- **WHEN** time on air is computed for a payload length
- **THEN** the spreading factor, bandwidth and coding rate used are those the modem reported via its radio readback, not those held in configuration

#### Scenario: Radio parameters change on reconnect
- **WHEN** the modem reconnects and reports radio parameters differing from the previous readback
- **THEN** subsequent time-on-air computations use the new parameters and the change is recorded as a wide event

#### Scenario: No radio readback is available
- **WHEN** the board did not answer the radio readback
- **THEN** the system SHALL refuse to admit any transmission rather than computing time on air from configured values

#### Scenario: The readback has not arrived yet
- **WHEN** a transmission is composed before the board has answered its readback, and the readback arrives within the waiting budget
- **THEN** the transmission is priced against the parameters that arrived and is admitted, and nothing is refused or dropped

#### Scenario: The readback never arrives
- **WHEN** a transmission is composed and no readback arrives within the waiting budget
- **THEN** the transmission is refused as it is today, and the reason states that the wait expired rather than that the readback was simply absent

#### Scenario: A readback withdrawn by a reconnect
- **WHEN** a transmission is composed while a reconnect has left the system with no current readback
- **THEN** it waits for the new parameters rather than being priced against the previous ones

## ADDED Requirements

### Requirement: A reception is never delayed by a transmission waiting for the readback
The system SHALL keep decoding, reporting and recording received packets while any transmission
waits for a radio readback. A wait SHALL be local to the transmission that needs the parameters, so
that the receive path — which needs no airtime figure — is never held behind one.

A reception whose own acknowledgement is waiting SHALL be the only one this delays, and it is not an
exception to the rule above: its report follows its acknowledgement because `direct-messaging`
requires that ordering, not because the pipeline is blocked.

#### Scenario: Receptions during a wait
- **WHEN** a reply is waiting for the readback and further packets are received
- **THEN** those packets are decoded, reported and recorded without waiting for the readback

#### Scenario: The wait does not stall the run
- **WHEN** no readback ever arrives
- **THEN** the run continues receiving and reporting, and only the transmissions that needed a price are refused
