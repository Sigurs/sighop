# airtime Specification

## Purpose
How a packet's LoRa time on air is computed — from the radio parameters read back from the
modem rather than from assumed preset values, matching the firmware's own configuration, and
cross-checked against the board's estimate. Every duty-cycle decision rests on this number.
## Requirements
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

### Requirement: The computation matches the firmware's radio configuration
The system SHALL use the preamble length the firmware uses — **32 symbols for spreading factor
8 and below, 16 symbols above** — together with an explicit header, CRC enabled, and low
data-rate optimisation enabled exactly when the symbol time is at least 16 ms.

#### Scenario: Preamble length follows the spreading factor
- **WHEN** time on air is computed at spreading factor 8 and again at spreading factor 9, all else equal
- **THEN** the first uses a 32-symbol preamble and the second a 16-symbol preamble

#### Scenario: Low data-rate optimisation threshold
- **WHEN** the symbol time under the live parameters is at least 16 ms
- **THEN** low data-rate optimisation is applied to the computation, and it is not applied below that threshold

#### Scenario: Known-answer values at the default preset
- **WHEN** time on air is computed at 869.618 MHz, BW 62.5 kHz, SF8, CR 4/8
- **THEN** a 64-byte payload yields approximately 0.74 s and a 255-byte payload approximately 2.31 s

### Requirement: The board's own estimate cross-checks the computation
The system SHALL, at startup and after each reconnect, request the modem's own airtime estimate
for a ladder of payload lengths and compare each against its own computation. A disagreement
exceeding both 1 ms and 2% SHALL be reported as an error-level wide event naming both figures;
the system SHALL continue using its own computed value.

#### Scenario: Board estimate agrees
- **WHEN** the modem's airtime estimate for each probed length is within 1 ms or 2% of the computed value
- **THEN** the agreement is recorded in the startup wide event and the run proceeds

#### Scenario: Board estimate disagrees
- **WHEN** the modem's airtime estimate for any probed length differs by more than 1 ms and more than 2%
- **THEN** an error-level wide event records the payload length, the computed value and the board's value, and the run proceeds using the computed value

#### Scenario: Board does not support the airtime query
- **WHEN** the modem rejects or does not answer the airtime query
- **THEN** the sub-command is recorded as unavailable, no error is raised, and the computed value is used unchecked

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

