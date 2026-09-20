# Spec Delta

## MODIFIED Requirements

### Requirement: Received messages appear in their conversation as they arrive
The system SHALL show a received direct message in its conversation as the platform receives it,
without the operator reloading, carrying the time it was received and the wire timestamp its sender
put on it where those differ. Bringing a conversation up to date SHALL NOT disturb what the
operator is doing in it: text the operator has selected SHALL stay selected, the scroll position
SHALL be kept, and a message already on screen SHALL NOT be redrawn unless what it states has
changed.

#### Scenario: A message arrives while a conversation is open
- **WHEN** a direct message for the open conversation is received
- **THEN** it appears in that conversation without a reload

#### Scenario: A message arrives for another conversation
- **WHEN** a direct message is received for a conversation that is not open
- **THEN** the interface indicates that conversation has something new, and the message is in it when opened

#### Scenario: Reading while the conversation updates
- **WHEN** the operator has selected the text of a message and the conversation is brought up to date
- **THEN** the selection survives and the view does not jump

#### Scenario: A state that changed
- **WHEN** a sent message's delivery state changes from awaiting acknowledgement to acknowledged
- **THEN** that message's state is redrawn to state it

### Requirement: Channels are listed beside direct conversations
The system SHALL list every channel loaded in this run in the chat interface, each with its name, its
marking as guessable where it is a hashtag or Public channel, and an indication that it has messages
received since it was last opened. Where a database is configured, the same list SHALL carry each
channel's administration: its kind, hash and message count, links to rename and remove it, the
stored channels this run could not load, and the forms that add a hashtag channel, add a
pre-shared-key channel and re-add Public. Those add-a-channel forms MAY be collapsed behind a
disclosure that is closed when the page is first opened, provided the disclosure names what it
holds; where a submission was refused, or a channel was just added or removed, the forms SHALL be
expanded and the reason or result visible without the operator opening anything.

#### Scenario: Opening chat with channels loaded
- **WHEN** the chat interface is opened on a run with the Public channel and one hashtag channel loaded
- **THEN** both channels are listed, both carry the guessable marking, and neither requires an identity to be chosen to be read

#### Scenario: A message arrives in a channel that is not open
- **WHEN** a channel message is received for a channel that is not open
- **THEN** the channel list indicates that channel has something new

#### Scenario: Administering channels from chat
- **WHEN** the chat interface is opened on a run with a database
- **THEN** each listed channel links to its rename and remove confirmations, and the forms to add a channel are on the same page

#### Scenario: A refused addition is not hidden
- **WHEN** the chat page is re-shown after a pre-shared key was refused
- **THEN** the reason and the form it was submitted from are both visible without the operator expanding anything

## ADDED Requirements

### Requirement: Composed length is counted against the limit as it is typed
The system SHALL show, beside a direct-message or channel composer, how much of what a single
message can carry the composed text uses, updated as it is typed, counting the same bytes the
refusal counts rather than characters. Where the text exceeds the limit the composer SHALL say so
before it is submitted. This SHALL NOT replace the refusal at submission, SHALL NOT truncate or
alter the operator's text, and SHALL NOT prevent submission — where the page's script does not run,
the composer behaves exactly as it does today and the refusal at submission still applies.

#### Scenario: Typing within the limit
- **WHEN** the operator types text that fits in one message
- **THEN** the composer shows how many of the available bytes are used, updating as they type

#### Scenario: Typing past the limit
- **WHEN** the composed text exceeds what one message can carry
- **THEN** the composer says so before submission, and the text is neither truncated nor altered

#### Scenario: Multi-byte text
- **WHEN** the composed text contains characters that encode to more than one byte
- **THEN** the count reflects the bytes the refusal would count, not the number of characters

#### Scenario: No script
- **WHEN** the page's script does not run
- **THEN** the composer still submits and a message over the limit is still refused with its reason at submission

### Requirement: A composed message can be sent from the keyboard
The system SHALL send a composed direct message or channel post when the operator presses
Ctrl+Enter (or the platform's equivalent modifier with Enter) in the composer, submitting exactly
what the send control submits, including the chosen identity and whether flooding was permitted. A
plain Enter SHALL insert a newline rather than send, so a multi-line message can be written.

#### Scenario: Sending from the keyboard
- **WHEN** the operator presses Ctrl+Enter in a composer holding text
- **THEN** the message is submitted exactly as pressing the send control would submit it

#### Scenario: A newline
- **WHEN** the operator presses Enter alone in a composer
- **THEN** a newline is inserted and nothing is submitted

#### Scenario: A keyboard send that is refused
- **WHEN** a send submitted from the keyboard is one the platform would refuse
- **THEN** it is refused with the same reason and the text preserved, exactly as a send from the control is
