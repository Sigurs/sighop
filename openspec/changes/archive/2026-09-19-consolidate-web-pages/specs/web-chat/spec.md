## ADDED Requirements

### Requirement: A conversation with a contact is started from the contact list
The system SHALL offer, on each row of the contact list, a link per identity loaded by this run
that opens the conversation between that identity and that contact. The chat page SHALL NOT repeat
a contacts-by-identities grid, and SHALL point to the contact list for starting a conversation.
Following such a link SHALL transmit nothing.

#### Scenario: Starting a conversation
- **WHEN** the contact list is opened on a run with two loaded identities and one contact
- **THEN** that contact's row links to a conversation as each of the two identities

#### Scenario: No identity loaded
- **WHEN** the contact list is opened on a run with no loaded identity
- **THEN** no conversation link is offered, and the page says a conversation needs an identity to send as

## MODIFIED Requirements

### Requirement: Channels are listed beside direct conversations
The system SHALL list every channel loaded in this run in the chat interface, each with its name, its
marking as guessable where it is a hashtag or Public channel, and an indication that it has messages
received since it was last opened. Where a database is configured, the same list SHALL carry each
channel's administration: its kind, hash and message count, links to rename and remove it, the
stored channels this run could not load, and the forms that add a hashtag channel, add a
pre-shared-key channel and re-add Public.

#### Scenario: Opening chat with channels loaded
- **WHEN** the chat interface is opened on a run with the Public channel and one hashtag channel loaded
- **THEN** both channels are listed, both carry the guessable marking, and neither requires an identity to be chosen to be read

#### Scenario: A message arrives in a channel that is not open
- **WHEN** a channel message is received for a channel that is not open
- **THEN** the channel list indicates that channel has something new

#### Scenario: Administering channels from chat
- **WHEN** the chat interface is opened on a run with a database
- **THEN** each listed channel links to its rename and remove confirmations, and the forms to add a channel are on the same page
