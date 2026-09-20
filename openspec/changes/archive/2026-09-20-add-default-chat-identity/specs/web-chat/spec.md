# Spec Delta

## ADDED Requirements

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

## MODIFIED Requirements

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
