# web-chat Specification

## Purpose

The companion made usable: a human picks one of the platform's identities, picks a contact, and
holds a conversation over the mesh from a browser. It is the first surface from which a person
drives a transmission without a command line, and the place where the protocol's delivery
semantics — an acknowledgement, a retry, a route that is not known — have to be shown as they
actually are.

## Requirements

### Requirement: A conversation is between one local identity and one contact
The system SHALL present conversations keyed by the pair of a local identity and a contact, SHALL
require the operator to have chosen both before a message can be composed, and SHALL NOT merge the
messages of two local identities with the same contact into one conversation.

#### Scenario: Two identities talking to one contact
- **WHEN** two local identities have each exchanged messages with the same contact
- **THEN** the interface shows two conversations, and each shows only its own identity's messages

#### Scenario: Composing without a chosen identity
- **WHEN** no local identity is selected
- **THEN** no message can be composed, and the interface says an identity must be chosen

### Requirement: Sending uses the platform's own send path and reports its outcome
The system SHALL send a composed message through the same composition, routing, retry and
acknowledgement path an operator-initiated send uses, and SHALL show that message's state as it
progresses: awaiting transmission, attempt in progress with the attempt number, acknowledged with
the measured latency, or failed with the reason it failed.

#### Scenario: A message that is acknowledged
- **WHEN** a composed message is acknowledged by its peer
- **THEN** the conversation shows it as delivered, with the number of attempts it took and the acknowledgement latency

#### Scenario: A message that is never acknowledged
- **WHEN** every attempt goes unacknowledged
- **THEN** the conversation shows it as unacknowledged rather than as delivered, and states that the platform cannot tell whether it arrived

#### Scenario: The message is composed on the same terms as any other
- **WHEN** a message is sent from the interface
- **THEN** its priority class, retry bounds and acknowledgement window are those the platform applies to any direct message

### Requirement: A send that the platform would refuse is refused before it is composed
The system SHALL refuse, at composition time and with the reason stated, a message that cannot be
sent: one whose text exceeds what a single direct message can carry, one to a contact with no known
route where flooding has not been permitted, and one submitted while transmission is disabled. A
refusal SHALL NOT be recorded as a message, and text SHALL NOT be truncated to fit.

#### Scenario: Text too long
- **WHEN** composed text exceeds what one direct message can carry
- **THEN** the interface refuses it, says the limit and how far over it is, and the author's text is preserved for editing

#### Scenario: No route and flooding not permitted
- **WHEN** the chosen contact has no known route and flooding has not been permitted for the send
- **THEN** the send is refused with that reason, and the interface offers flooding as an explicit choice rather than performing it

#### Scenario: The transmit gate is closed
- **WHEN** a send is submitted while transmission is disabled
- **THEN** it is refused with that reason and nothing is queued, so that no message is delivered later by a change of gate

#### Scenario: A refusal is not history
- **WHEN** a send is refused
- **THEN** no message appears in the conversation's history

### Requirement: Received messages appear in their conversation as they arrive
The system SHALL show a received direct message in its conversation as the platform receives it,
without the operator reloading, carrying the time it was received and the wire timestamp its sender
put on it where those differ.

#### Scenario: A message arrives while a conversation is open
- **WHEN** a direct message for the open conversation is received
- **THEN** it appears in that conversation without a reload

#### Scenario: A message arrives for another conversation
- **WHEN** a direct message is received for a conversation that is not open
- **THEN** the interface indicates that conversation has something new, and the message is in it when opened

### Requirement: A received message's sender is identified by the key that decrypted it, and is not presented as an authenticated identity
The system SHALL identify a received message by the contact whose key decrypted it, and SHALL state
that the protocol authenticates possession of that key rather than the identity of the person
holding it. A message decrypted under a contact whose advert has not been verified SHALL be marked
accordingly.

#### Scenario: A message from a verified contact
- **WHEN** a received message decrypts under a contact whose advert signature was verified
- **THEN** the contact's name is shown with the verified marking the rest of the interface uses

#### Scenario: A message from an unverified contact
- **WHEN** a received message decrypts under a contact whose advert was not verified
- **THEN** the sender is marked unverified, distinctly from a verified one

#### Scenario: The distinction is stated, not implied
- **WHEN** a conversation is displayed
- **THEN** the interface states what the verification marking means, rather than relying on the operator's inference

### Requirement: Opening or reading a conversation transmits nothing
The system SHALL treat reading as read-only with respect to the mesh: opening, scrolling or
refreshing a conversation SHALL NOT transmit anything, including any acknowledgement, receipt or
presence indication.

#### Scenario: Opening a conversation
- **WHEN** a conversation is opened or refreshed repeatedly
- **THEN** nothing is transmitted
### Requirement: Chat is usable when history cannot be recorded, and says so
The system SHALL allow sending and receiving when the configured database is degraded, and SHALL
state in that case that the conversation is not being recorded from that point and will not
survive the run.

#### Scenario: No database configured
- **WHEN** a run with no database configured is asked to serve the interface
- **THEN** the run refuses to start the interface, so there is no chat surface whose history could silently go unrecorded

#### Scenario: The database degrades mid-conversation
- **WHEN** the database becomes unreachable during a conversation
- **THEN** sending and receiving continue, and the interface states that messages from this point are not being recorded

#### Scenario: The database is unreachable when a conversation is opened
- **WHEN** a conversation is opened while the database is degraded
- **THEN** the messages this run has seen are shown, sending is available, and the interface states that stored history cannot be read and new messages are not being recorded

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
