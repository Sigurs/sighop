# Spec Delta

## MODIFIED Requirements

### Requirement: Sending uses the platform's own send path and reports its outcome
The system SHALL send a composed message through the same composition, routing, retry and
acknowledgement path an operator-initiated send uses, and SHALL show that message's state as it
progresses: awaiting transmission, attempt in progress with the attempt number, acknowledged with
the number of attempts and the measured latency, or failed with the reason it failed. Each state
SHALL be drawn as a status glyph with its numbers, with the full statement of the state on hover, as
the `web-display` status convention requires.

#### Scenario: A message that is acknowledged
- **WHEN** a composed message is acknowledged by its peer after one attempt in 3010 ms
- **THEN** the conversation shows the delivered glyph, the attempt count 1 and the latency "3.0 s", and hovering states that it was delivered and acknowledged after 1 attempt

#### Scenario: A message that is never acknowledged
- **WHEN** every attempt goes unacknowledged
- **THEN** the conversation shows the unacknowledged glyph, distinct from the delivered glyph, with the attempt count, and hovering states that the platform cannot tell whether it arrived

#### Scenario: The message is composed on the same terms as any other
- **WHEN** a message is sent from the interface
- **THEN** its priority class, retry bounds and acknowledgement window are those the platform applies to any direct message

### Requirement: A channel conversation shows the channel's history and new messages as they arrive
The system SHALL show a channel's recorded history and SHALL show a received channel message without
the operator reloading, carrying the time it was received and, where it differs, the wire timestamp
its sender put on it, and for a received message a received glyph with its hop count.

#### Scenario: A message arrives while the channel is open
- **WHEN** a channel message is received for the open channel
- **THEN** it appears in the conversation without a reload

#### Scenario: A received message's hops
- **WHEN** a channel message received over 2 hops is displayed
- **THEN** its state shows the received glyph and 2, and hovering states that it was received over 2 hops

### Requirement: A channel post is composed as a chosen identity and reports what can be known
The system SHALL require an identity to be chosen before a channel post can be composed, SHALL send
it through the platform's channel post path, and SHALL show the post under the identity that posted
it with its state: awaiting transmission, transmitted, or not transmitted with the reason; and the
number of repeats heard, drawn as a repeat glyph and count whose hover states it is repeater
evidence rather than delivery. That no acknowledgement exists for channel messages SHALL be stated
once on the channel page and in the transmitted glyph's hover text, not repeated in every row. A post
refused by the platform SHALL be refused at composition with the reason stated, the author's text
preserved, and nothing recorded.

#### Scenario: Composing without an identity
- **WHEN** no identity is chosen in a channel conversation
- **THEN** no post can be composed, and the interface says an identity must be chosen

#### Scenario: A transmitted post
- **WHEN** a post is transmitted and 2 repeats are heard
- **THEN** the row shows the transmitted glyph and the repeat glyph with 2, hovering the transmitted glyph states that no acknowledgement exists for channel messages, and hovering the repeat glyph states that a repeater forwarded it

#### Scenario: A transmitted post with no repeat heard
- **WHEN** a post is transmitted and no repeat is heard
- **THEN** the row shows the transmitted glyph without a repeat count, and hovering states that no repeat heard does not mean it was not received

#### Scenario: A post over the limit
- **WHEN** the chosen identity's name, separator and text exceed 160 bytes
- **THEN** the post is refused stating the limit, that the name counts towards it, and how far over it is, and the text is preserved for editing

#### Scenario: Posting to Public
- **WHEN** a post is composed in the Public channel
- **THEN** the composer states that the post is flooded to the whole mesh and readable by anyone
