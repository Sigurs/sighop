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
messages of two local identities with the same contact into one conversation. The conversation's
composer SHALL offer the identity to send as, preselected with the operator's default where this run
holds it, and sending as another identity SHALL place the message in that identity's conversation
with the same contact.

#### Scenario: Two identities talking to one contact
- **WHEN** two local identities have each exchanged messages with the same contact
- **THEN** the interface shows two conversations, and each shows only its own identity's messages

#### Scenario: Composing without a chosen identity
- **WHEN** no local identity is selected
- **THEN** no message can be composed, and the interface says an identity must be chosen

#### Scenario: Sending as another identity
- **WHEN** an operator opens a conversation, selects an identity other than the one shown, and sends
- **THEN** the message is sent as the selected identity and appears in that identity's conversation with the contact

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
The system SHALL require an identity to be chosen before a channel post can be composed, SHALL
preselect the operator's default identity where this run holds it while leaving every other loaded
identity selectable for that post, SHALL send it through the platform's channel post path, and SHALL
show the post under the identity that posted it with its state: awaiting transmission, transmitted,
or not transmitted with the reason; and the number of repeats heard, drawn as a repeat glyph and
count whose hover states it is repeater evidence rather than delivery. That no acknowledgement
exists for channel messages SHALL be stated once on the channel page and in the transmitted glyph's
hover text, not repeated in every row. A post refused by the platform SHALL be refused at
composition with the reason stated, the author's text preserved, and nothing recorded.

#### Scenario: Composing without an identity
- **WHEN** no identity is chosen in a channel conversation and the operator holds no usable default
- **THEN** no post can be composed, and the interface says an identity must be chosen

#### Scenario: Composing with a default identity
- **WHEN** a channel conversation is opened by an operator whose default identity this run holds
- **THEN** the composer opens with that identity selected, and a post made without touching the selection is sent as it

#### Scenario: Overriding the default for one post
- **WHEN** an operator selects an identity other than their default and posts
- **THEN** the post is sent as the selected identity, and the operator's default is unchanged

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

### Requirement: A conversation with a contact is started from the contact list
The system SHALL offer, on each row of the contact list, one link that opens a conversation with
that contact as the operator's default identity where this run holds it, and as the run's only
identity where exactly one is loaded. Where neither applies — several identities are loaded and
none is the operator's default — the row SHALL offer no conversation link and SHALL point to where
an identity to chat as is chosen, because a conversation cannot be opened without one. The contact
list SHALL NOT draw one link per loaded identity, the chat page SHALL NOT repeat a
contacts-by-identities grid, and the chat page SHALL point to the contact list for starting a
conversation. Following such a link SHALL transmit nothing.

#### Scenario: Starting a conversation
- **WHEN** the contact list is opened on a run with two loaded identities and one contact, by an operator whose default is one of them
- **THEN** that contact's row offers one conversation link, and it opens the conversation as the default identity

#### Scenario: One identity loaded and no default
- **WHEN** the contact list is opened on a run holding exactly one identity by an operator with no default
- **THEN** that contact's row offers one conversation link, and it opens the conversation as that identity

#### Scenario: Several identities and no default
- **WHEN** the contact list is opened on a run holding two identities by an operator with no default
- **THEN** no conversation link is offered on the row, and it points to where an identity to chat as is chosen

#### Scenario: No identity loaded
- **WHEN** the contact list is opened on a run with no loaded identity
- **THEN** no conversation link is offered, and the page says a conversation needs an identity to send as

### Requirement: An operator holds a default identity to chat as
The system SHALL let each signed-in operator hold one default local identity, SHALL preselect that
identity in every chat composer, and SHALL keep it across sign-out and restart. The default SHALL
belong to the operator rather than to the run: two operators signed into the same run SHALL be able
to hold different defaults, and one operator's choice SHALL NOT change another's. The default SHALL
be settable and clearable from the chat interface, choosing among the identities this run holds,
and SHALL transmit nothing when set, cleared or applied.

#### Scenario: Setting a default
- **WHEN** an operator sets one of the run's identities as their default
- **THEN** the chat interface reports that identity as their default, and the channel and conversation composers open with it selected

#### Scenario: Two operators
- **WHEN** two operators are signed in and each sets a different default identity
- **THEN** each operator's composers open with their own default, and neither sees the other's

#### Scenario: The default survives a sign-out
- **WHEN** an operator who has set a default signs out and signs back in
- **THEN** their composers open with the same identity selected

#### Scenario: Clearing the default
- **WHEN** an operator clears their default identity
- **THEN** no identity is preselected, and the composers ask for one to be chosen as they do for an operator who has never set one

#### Scenario: Setting a default while the database is degraded
- **WHEN** an operator sets or clears their default identity while the configured database is degraded
- **THEN** the change is refused with the reason stated, and the default in force is left as it was

#### Scenario: Setting a default transmits nothing
- **WHEN** a default identity is set, cleared or applied to a composer
- **THEN** nothing is transmitted

### Requirement: A default identity that this run cannot use is not silently substituted
The system SHALL apply an operator's default only while this run holds that identity. Where the
default names an identity this run has not loaded, or one that has been removed, the system SHALL
preselect nothing, SHALL state that no identity is chosen, and SHALL NOT compose as a different
identity. An identity's removal SHALL clear every default that names it.

#### Scenario: The default identity is not loaded by this run
- **WHEN** an operator whose default names an identity this run does not hold opens a composer
- **THEN** no identity is preselected, the interface says an identity must be chosen, and no post or message is composed as another identity

#### Scenario: The default identity is removed
- **WHEN** the identity an operator holds as their default is removed from the platform
- **THEN** that operator no longer holds a default, and their composers ask for an identity to be chosen

#### Scenario: The database is degraded
- **WHEN** a composer is opened while the configured database is degraded
- **THEN** the default this session already knows is still preselected, every identity this run holds is still offered, and posting and sending continue to be accepted

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
