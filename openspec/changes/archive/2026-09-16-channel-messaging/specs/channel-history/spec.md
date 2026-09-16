## Purpose

Durable record of what was said in the station's channels, in both directions, so that a channel's
conversation survives a restart and can be read back in order.

## ADDED Requirements

### Requirement: Channel messages are recorded durably in both directions
The system SHALL record every received channel message it presents and every channel post it
submits, with the channel, the direction, the text, the wire timestamp, the time the station handled
it, and: for a received message the claimed sender name, hop count, SNR and RSSI of the first copy;
for a post the identity that posted it, its outcome and its repeat count.

#### Scenario: A received message survives restart
- **WHEN** a channel message is received and the run is restarted
- **THEN** the message is in that channel's history with its claimed sender name still marked unverified

#### Scenario: A post survives restart
- **WHEN** a post is transmitted, two repeats are heard, and the run is restarted
- **THEN** the post is in the channel's history naming the identity that posted it, transmitted, with two repeats heard

### Requirement: A channel message's record is written once and updated in place
The system SHALL write a post's record at submission and update that same record as its outcome
resolves and as repeats are heard, and SHALL write a received message once, so that no message
appears twice in a channel's history.

#### Scenario: A post observed mid-flight
- **WHEN** a channel's history is read after a post is submitted and before it transmits
- **THEN** the post appears once, awaiting transmission, and after transmission the same entry shows transmitted

#### Scenario: A restart before transmission
- **WHEN** the run stops after a post was recorded and before its transmission resolved
- **THEN** its recorded outcome after restart is unknown, not transmitted

### Requirement: Recording never delays reception or transmission
The system SHALL record channel messages behind the reception and transmission paths, SHALL NOT make
decryption or submission wait for a database write, and SHALL count and report a record that could
not be written rather than dropping it silently.

#### Scenario: The database degrades
- **WHEN** the database is unreachable while channel messages are received and posted
- **THEN** reception and posting continue, and the unwritten records are counted and reported

### Requirement: A channel's history is read in a bounded, handled-time order
The system SHALL read a channel's history newest-first up to a bounded count, ordered by the time the
station handled each message and never by the wire timestamp, because a sender's clock is not
trusted to order a conversation.

#### Scenario: A sender with a wrong clock
- **WHEN** a message whose wire timestamp is a day in the past is received after another message
- **THEN** it is shown after that message

### Requirement: Channel message text is stored unencrypted, and that is stated
The system SHALL store channel message text as bytes without encryption at rest and SHALL state in the
schema migration that a database dump exposes channel content in the clear.

#### Scenario: Reading the migration
- **WHEN** the migration that creates the channel message table is read
- **THEN** it states that stored channel text is not encrypted at rest

### Requirement: Channel history is not pruned by default
The system SHALL NOT delete channel messages except when their channel is removed or the schema is
downgraded.

#### Scenario: A long-running station
- **WHEN** a channel accumulates messages over weeks
- **THEN** none are deleted by the running platform
