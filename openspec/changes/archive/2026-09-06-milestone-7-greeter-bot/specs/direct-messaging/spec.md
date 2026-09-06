## MODIFIED Requirements

> Reference: milestone 7 adds a consumer of received messages that must answer *as the entity that
> received one*. The report already names that entity for display; it must now identify it well
> enough to act as. Decryption, candidate trials, the claimed-sender rule and the acknowledgement
> itself are unchanged.

### Requirement: A decrypted message is acknowledged and reported
The system SHALL, on successfully decrypting and parsing an inbound direct message, report the
message with its sender, timestamp, text and reception metadata, and SHALL submit an
acknowledgement addressed to the sender at priority class 0 by the same routing rules as an
outbound message. The report SHALL identify the local entity that received the message, not only
its display name, so that a consumer of the report can reply as that entity without resolving a
name back to an identity. The acknowledgement SHALL be submitted before the report is delivered to
any consumer, so that no consumer can delay or prevent it.

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
