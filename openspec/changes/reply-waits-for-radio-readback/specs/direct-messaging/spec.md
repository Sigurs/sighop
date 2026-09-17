## MODIFIED Requirements

### Requirement: A decrypted message is acknowledged and reported
The system SHALL, on successfully decrypting and parsing an inbound direct message, report the
message with its sender, timestamp, text and reception metadata, and SHALL submit an
acknowledgement addressed to the sender at priority class 0 by the same routing rules as an
outbound message. The report SHALL identify the local entity that received the message, not only
its display name, so that a consumer of the report can reply as that entity without resolving a
name back to an identity. The acknowledgement SHALL be submitted before the report is delivered to
any consumer, so that no consumer can delay or prevent it.

An acknowledgement SHALL NOT be lost to a radio readback that has not arrived yet. Where a message
is decrypted before the board has answered its readback, the acknowledgement SHALL wait for those
parameters within a bounded budget and then be submitted, because a message that arrives in a run's
first moments is exactly as owed an answer as one that arrives an hour later, and a sender that gets
no acknowledgement retries into silence.

Such a wait SHALL NOT reorder the acknowledgement with respect to consumers. The acknowledgement is
committed — its outcome no longer reachable by any consumer — before the report is delivered, which
is what the ordering rule above exists to guarantee; only its submission to the scheduler completes
later. Waiting for the board is not a consumer delaying an acknowledgement, and the report SHALL NOT
be held behind it.

#### Scenario: Message received and acknowledged
- **WHEN** an inbound direct message decrypts and parses
- **THEN** the message is reported and an acknowledgement is queued to its sender

#### Scenario: Message decrypts but does not parse
- **WHEN** a candidate's MAC verifies but the plaintext does not parse as a text message body
- **THEN** the failure is reported with the parse reason, and no acknowledgement is sent

#### Scenario: The report identifies the receiving entity
- **WHEN** an inbound direct message is reported
- **THEN** the report identifies the local entity that received it, and two entities sharing a display name are still distinguishable

#### Scenario: Acknowledgement precedes consumers
- **WHEN** a consumer of the report is slow or raises
- **THEN** the acknowledgement has already been submitted and is unaffected

#### Scenario: A consumer cannot reach an acknowledgement that is waiting for the board
- **WHEN** a consumer of the report is slow or raises while an acknowledgement is waiting for the radio readback
- **THEN** the acknowledgement's outcome is unaffected by that consumer, exactly as when no wait was needed

#### Scenario: A message arriving before the radio readback
- **WHEN** an inbound direct message decrypts before the board has answered its radio readback, and the readback arrives within the waiting budget
- **THEN** the acknowledgement is submitted to its sender, and nothing reports it as unroutable

#### Scenario: A message arriving when no readback ever comes
- **WHEN** an inbound direct message decrypts and no readback arrives within the waiting budget
- **THEN** the message is still reported, the acknowledgement is refused with a reason naming the expired wait, and the run keeps receiving

#### Scenario: The report is not delayed by the wait
- **WHEN** an acknowledgement is waiting for the readback
- **THEN** the message has already been reported to consumers, and the wait delays no report
