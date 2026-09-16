## ADDED Requirements

### Requirement: Channels are listed beside direct conversations
The system SHALL list every channel loaded in this run in the chat interface, each with its name, its
marking as guessable where it is a hashtag or Public channel, and an indication that it has messages
received since it was last opened.

#### Scenario: Opening chat with channels loaded
- **WHEN** the chat interface is opened on a run with the Public channel and one hashtag channel loaded
- **THEN** both channels are listed, both carry the guessable marking, and neither requires an identity to be chosen to be read

#### Scenario: A message arrives in a channel that is not open
- **WHEN** a channel message is received for a channel that is not open
- **THEN** the channel list indicates that channel has something new

### Requirement: A channel conversation shows the channel's history and new messages as they arrive
The system SHALL show a channel's recorded history and SHALL show a received channel message without
the operator reloading, carrying the time it was received and, where it differs, the wire timestamp
its sender put on it, and for a received message its hop count.

#### Scenario: A message arrives while the channel is open
- **WHEN** a channel message is received for the open channel
- **THEN** it appears in the conversation without a reload

### Requirement: A received channel message's sender is shown as an unverified claim
The system SHALL show a received channel message's sender as the claimed name, visually distinct from
every verified identity presentation in the interface, SHALL NOT link it to a contact or identity, and
SHALL state in the conversation that anyone holding the channel key can claim any name.

#### Scenario: A claim that matches a contact
- **WHEN** a channel message claims the name of a verified contact
- **THEN** the name is shown with the unverified-claim presentation and without any link to that contact

#### Scenario: The rule is stated
- **WHEN** a channel conversation is displayed
- **THEN** the interface states that channel sender names are not authenticated

### Requirement: A channel post is composed as a chosen identity and reports what can be known
The system SHALL require an identity to be chosen before a channel post can be composed, SHALL send
it through the platform's channel post path, and SHALL show the post under the identity that posted
it with its state: awaiting transmission, transmitted, or not transmitted with the reason; and the
number of repeats heard, stated as repeater evidence rather than delivery. A post refused by the
platform SHALL be refused at composition with the reason stated, the author's text preserved, and
nothing recorded.

#### Scenario: Composing without an identity
- **WHEN** no identity is chosen in a channel conversation
- **THEN** no post can be composed, and the interface says an identity must be chosen

#### Scenario: A transmitted post
- **WHEN** a post is transmitted and repeats are heard
- **THEN** the post shows transmitted, the number of repeats heard, and that no acknowledgement exists for channel messages

#### Scenario: A post over the limit
- **WHEN** the chosen identity's name, separator and text exceed 160 bytes
- **THEN** the post is refused stating the limit, that the name counts towards it, and how far over it is, and the text is preserved for editing

#### Scenario: Posting to Public
- **WHEN** a post is composed in the Public channel
- **THEN** the composer states that the post is flooded to the whole mesh and readable by anyone

### Requirement: Opening or reading a channel transmits nothing
The system SHALL NOT transmit anything when a channel conversation is opened, scrolled or refreshed.

#### Scenario: Opening a channel
- **WHEN** a channel conversation is opened or refreshed repeatedly
- **THEN** nothing is transmitted

### Requirement: Channel chat is usable when history cannot be recorded, and says so
The system SHALL allow posting and receiving in loaded channels while the configured database is
degraded, SHALL show the channel messages this run has seen, and SHALL state that messages from that
point are not being recorded.

#### Scenario: The database degrades with a channel open
- **WHEN** the database becomes unreachable while a channel conversation is open
- **THEN** receiving and posting continue, and the interface states that messages from this point are not being recorded

## REMOVED Requirements

### Requirement: Channels are absent, and the interface says so and why
**Reason**: Channels are implemented by this change: a channel key store exists and group text is
decrypted, so the statement that they are unsupported would be false.
**Migration**: Channel conversations are listed in the chat interface (see "Channels are listed
beside direct conversations"). A group text payload on a channel that is not loaded still appears
only in the packet feed, as `channel-messaging` requires.
