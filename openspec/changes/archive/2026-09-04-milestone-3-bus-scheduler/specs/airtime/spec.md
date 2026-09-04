## ADDED Requirements

> Reference: DESIGN.md §4.3 (airtime budget, deaf window). The firmware is the authority for
> the radio parameters this computation must match: `src/helpers/radiolib/RadioLibWrappers.h`
> (`preambleLengthForSF`) and `src/helpers/radiolib/CustomSX1262.h` (`setCRC`) in the vendored
> MeshCore submodule.

### Requirement: Time-on-air is computed from live radio parameters
The system SHALL compute the LoRa time on air for a given payload length from the spreading
factor, bandwidth and coding rate **read back from the modem**, and SHALL NOT compute it from
configured or assumed preset values.

#### Scenario: Radio parameters read back from the board
- **WHEN** time on air is computed for a payload length
- **THEN** the spreading factor, bandwidth and coding rate used are those the modem reported via its radio readback, not those held in configuration

#### Scenario: Radio parameters change on reconnect
- **WHEN** the modem reconnects and reports radio parameters differing from the previous readback
- **THEN** subsequent time-on-air computations use the new parameters and the change is recorded as a wide event

#### Scenario: No radio readback is available
- **WHEN** the board did not answer the radio readback
- **THEN** the system SHALL refuse to admit any transmission rather than computing time on air from configured values

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
