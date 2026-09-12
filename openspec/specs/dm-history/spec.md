# dm-history Specification

## Purpose

The durable record of direct messages in both directions, so a conversation survives a restart and
a reload. Room history has been durable since milestone 6 because a room server's whole purpose is
to hold it; a person's own conversation had no store at all, which made every direct message a
thing that existed only in the process that saw it.

## Requirements

### Requirement: Direct messages are recorded durably in both directions
The system SHALL record each direct message it sends and each it receives, keyed by the local
identity and the peer, carrying the direction, the message text as bytes, the wire timestamp, the
time the platform handled it, the identifier that joins it to its packets, and — for a sent
message — the number of attempts, the route used and the final delivery outcome.

#### Scenario: A sent message is recorded
- **WHEN** a direct message is sent
- **THEN** a record exists for it carrying its route, attempts and outcome

#### Scenario: A received message is recorded
- **WHEN** a direct message is received and decrypted
- **THEN** a record exists for it under the local identity it was addressed to and the contact whose key decrypted it

#### Scenario: A conversation survives a restart
- **WHEN** the platform is restarted
- **THEN** the messages recorded before the restart are readable afterwards, in the same order

### Requirement: A message's record is written once and updated in place
The system SHALL record a sent message when it is submitted and SHALL update that same record as
its attempts and final outcome become known, so that a message in flight is visible and a resolved
message does not appear twice. A repeated write for the same message SHALL update rather than
duplicate.

#### Scenario: A message in flight
- **WHEN** a send has been submitted and has not yet resolved
- **THEN** its record exists and states that it is in flight

#### Scenario: The send resolves
- **WHEN** the send resolves as acknowledged, unacknowledged or failed
- **THEN** the same record carries that outcome, and no second record was created

#### Scenario: A restart during a send
- **WHEN** the platform restarts while a send is in flight
- **THEN** the record remains, stating that its outcome is unknown, rather than claiming delivery or disappearing

### Requirement: Recording never delays an acknowledgement or a transmission
The system SHALL NOT make the acknowledgement of a received direct message, or the transmission of
a sent one, wait on a durable write. Recording SHALL happen behind those decisions, on the same
never-awaits, never-raises contract the contact and path stores use.

#### Scenario: A received message with a slow database
- **WHEN** a direct message is received while the database is slow
- **THEN** the acknowledgement is sent within its window and the record is written behind it

#### Scenario: A database that does not answer
- **WHEN** the database is unreachable
- **THEN** sending and receiving are unaffected and nothing in the reception path raises

### Requirement: A record that could not be written is counted and reported, not dropped silently
The system SHALL treat a direct message record as costly to lose, retaining it for retry rather
than discarding it freely, and SHALL count and report records it could not write. A conversation
whose records were lost SHALL be reported as incomplete rather than presented as complete.

#### Scenario: The database is degraded and then recovers
- **WHEN** the database is unreachable while messages are sent and received, and then becomes reachable
- **THEN** the records observed during the outage are written with their latest known state

#### Scenario: Records were discarded
- **WHEN** message records have been discarded
- **THEN** the count is reported with the platform's other discarded-write counts

#### Scenario: A gap is visible
- **WHEN** a conversation is read whose records include a known gap
- **THEN** the gap is stated rather than closed over

### Requirement: Message text is stored as bytes and its exposure is stated
The system SHALL store message text as the bytes that were on the wire, without transcoding, and
SHALL state — in the schema's own documentation — that stored direct message text is not encrypted
at rest, so that a database dump exposes conversation content.

#### Scenario: Text that is not valid displayable text
- **WHEN** a received message's bytes are not valid displayable text
- **THEN** they are stored unchanged and can be read back byte-for-byte

#### Scenario: The exposure is documented
- **WHEN** the schema is read
- **THEN** it states that message text is stored unencrypted and what that means for a dump

### Requirement: A conversation is readable in a bounded, ordered way
The system SHALL make a conversation readable newest-first in bounded pages, and SHALL order
messages by the time the platform handled them so that a peer's clock cannot reorder a
conversation.

#### Scenario: Reading a long conversation
- **WHEN** a conversation holds more messages than one page
- **THEN** the most recent are returned first and older ones are reachable, in a stable order

#### Scenario: A peer with a wrong clock
- **WHEN** a received message carries a wire timestamp far from the platform's own
- **THEN** the conversation's order is unaffected and both times are available

### Requirement: Direct message history is not pruned by default
The system SHALL keep recorded direct messages indefinitely unless an operator asks otherwise, and
SHALL NOT prune them as part of the packet feed's pruning, because a conversation is content and
the feed is a sample.

#### Scenario: The packet log is pruned
- **WHEN** the packet log is pruned to its bound
- **THEN** no direct message record is removed

#### Scenario: No retention configured
- **WHEN** no retention has been asked for
- **THEN** every recorded message is kept
