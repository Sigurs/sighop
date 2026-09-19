## ADDED Requirements

### Requirement: The station's radio, schema and gate controls are on one system page
The system SHALL present, on a single system page, the radio parameters in force as the board's
readback reports them, the schema revision agreement, the capabilities deliberately left to the
terminal (applying migrations, managing accounts, generating the sealing secret), and a link to the
confirmation view for enabling transmission and to the confirmation view for raising the airtime
ceiling. The board's readback SHALL appear on that page once. No other page SHALL repeat the
readback table or the schema revision table.

#### Scenario: Opening the system page
- **WHEN** the system page is opened
- **THEN** it shows the board's readback, the applied and expected schema revisions and whether they agree, the terminal-only capabilities with their commands, and links to the enable-transmission and raise-ceiling confirmations

#### Scenario: Reaching the gate controls
- **WHEN** an operator follows the enable-transmission or raise-ceiling link on the system page
- **THEN** the confirmation view for that action is shown, and nothing is enabled or raised by following the link

#### Scenario: A replay has no readback
- **WHEN** the system page is opened on a run that took no startup probe
- **THEN** it states that there is no readback because there was no board to ask, and still shows the schema revision and the gate links

### Requirement: Each loaded identity's traffic is counted on the identities page
The system SHALL show, for every identity loaded by the running process, on the identities page,
how many frames it has transmitted, how many of its submissions were suppressed, and how many
received frames were addressed to its node hash, and SHALL state that the addressed count is by
one-byte node hash and so is not the same claim as frames for that identity.

#### Scenario: A loaded identity's counters
- **WHEN** the identities page is opened in a run with a loaded identity that has transmitted
- **THEN** its transmitted, suppressed and addressed counts are shown in its row of the loaded identities

#### Scenario: The addressed count's caveat
- **WHEN** the addressed counts are shown
- **THEN** the page states that they count frames by one-byte node hash, which collides

## MODIFIED Requirements

### Requirement: Bots are configured through the interface
The system SHALL present each configured bot on the page of the identity it runs on, with its
driver, enabled state and mode, SHALL allow creating one from the page of a bot-type identity that
carries none, enabling and disabling it, switching between observing and active, and editing its
driver configuration, and SHALL state that switching a bot to active means it will transmit
without further prompting. The identities list SHALL indicate which identities carry a bot, and a
write to a bot SHALL return the operator to that identity's page.

#### Scenario: Switching a bot to active
- **WHEN** a bot's mode is changed to active
- **THEN** the interface states that the bot will transmit unprompted, and the change is confirmed explicitly

#### Scenario: Invalid driver configuration
- **WHEN** a driver configuration is submitted that the driver rejects
- **THEN** the change is refused with the driver's own reason, the identity's page is re-shown with that reason, and the stored configuration is unchanged

#### Scenario: A bot's durable state is visible
- **WHEN** a bot's page is opened
- **THEN** the state the bot has persisted is readable, including the records that decide whether it will act on a given contact again

#### Scenario: Creating a bot from its identity
- **WHEN** the page of a bot-type identity that carries no bot is opened
- **THEN** it offers the form that creates a bot on that identity, choosing only the driver

#### Scenario: An identity that cannot carry a bot
- **WHEN** the page of an identity that is not bot-type is opened
- **THEN** no bot section and no create-a-bot form is shown

### Requirement: Radio parameters are shown as the board reports them, and a change states its scope
The system SHALL display the radio parameters in force as the board's own readback reports them,
on the system page, and SHALL, where it offers to change them, state that the change is not
persisted by the board and is lost on a board reset.

#### Scenario: Displaying the radio
- **WHEN** the system page is opened
- **THEN** the parameters shown are the board's readback, and any value the board did not answer is shown as absent

#### Scenario: Changing a parameter
- **WHEN** a radio parameter is changed
- **THEN** the interface states that the board does not persist it and that a reset reverts to the board's build defaults

### Requirement: The schema revision this run is against is visible
The system SHALL show, on the system page, the revision the database reports and the revision this
code expects, and SHALL say whether they agree. It SHALL NOT offer to apply migrations.

#### Scenario: A database at the expected revision
- **WHEN** the system page is opened against a database at the revision the code expects
- **THEN** both revisions are shown and the interface says they agree

#### Scenario: A database at a different revision
- **WHEN** the revisions differ
- **THEN** both are named, the disagreement is stated, and the command that reconciles them is given

#### Scenario: Migrations are not applied from the interface
- **WHEN** the system page is used
- **THEN** it offers no action that applies a migration

### Requirement: Channels are configured through the interface
The system SHALL administer channels from the chat page: it SHALL list the stored channels there
with their name, kind, channel hash, guessable marking and recorded message count, including a
stored channel this run could not load; SHALL allow adding a channel from a hashtag or from a
pasted pre-shared key, and re-adding the Public channel if it was removed; and SHALL allow removing
a channel through an explicit confirmation that states how many messages will be deleted. A write
to a channel SHALL return the operator to the chat page. A stored pre-shared key SHALL NOT be
rendered anywhere in the interface, including in a form re-shown after a refused addition.

#### Scenario: Adding a channel
- **WHEN** a channel is added through the interface
- **THEN** its stored result is indistinguishable from the same channel added through the command line, and the running process decrypts on it without restart

#### Scenario: Adding a hashtag channel
- **WHEN** a hashtag channel is added through the interface
- **THEN** the result states that anyone who guesses the hashtag can read and post in it

#### Scenario: A refused pre-shared key
- **WHEN** a pasted pre-shared key is refused
- **THEN** the chat page is re-shown with the reason the command line gives and with the key field empty

#### Scenario: Viewing stored channels
- **WHEN** the chat page is opened
- **THEN** no pre-shared key, in any encoding, is present in the page source

#### Scenario: No database configured
- **WHEN** the chat page is opened on a run with no database
- **THEN** the page states that channels require durable storage and offers no channel administration controls

### Requirement: Rooms and bots are deleted through the interface, and the cost is counted first
The system SHALL allow deleting a room and deleting a bot through the interface. Each SHALL be a
distinct guarded action per target: performed only by a submission from a confirmation view that
states what that deletion destroys and mints a confirmation for that action and that target alone,
never reachable by following, prefetching or reloading a page, and recorded as its own structured
event naming the action, the target, the outcome and the acting user, whether it succeeds or is
refused.

The confirmation for a room SHALL state the number of members and the number of stored messages it
will delete. The confirmation for a bot SHALL state what the bot's durable state records and how
many stored keys it will delete. Each SHALL state that the deletion is irreversible, and SHALL
state that the bound identity survives the deletion and becomes unbound rather than being removed.
A deleted room SHALL return the operator to the rooms page; a deleted bot SHALL return the operator
to the page of the identity it ran on.

Neither SHALL require the acting user's password, on the same grounds as removing a channel: they
destroy stored content, not key material and not what the station may do.

#### Scenario: Deleting a room
- **WHEN** a room deletion is confirmed
- **THEN** the room, its members and its stored messages are deleted, the identity it was bound to is not, and the deletion is recorded as its own event naming the room and the operator

#### Scenario: A room confirmation states the cost
- **WHEN** the deletion confirmation for a room is opened
- **THEN** it states how many members and how many stored messages will be deleted, that the deletion is irreversible, and that the bound identity survives unbound

#### Scenario: Deleting a bot
- **WHEN** a bot deletion is confirmed
- **THEN** the bot and its durable state are deleted, the identity it was bound to is not, and the deletion is recorded as its own event naming the bot and the operator

#### Scenario: A bot confirmation states the cost
- **WHEN** the deletion confirmation for a bot is opened
- **THEN** it states what the durable state records, how many keys will be deleted, that the deletion is irreversible, and that the bound identity survives unbound

#### Scenario: A confirmation minted for another target
- **WHEN** a deletion is submitted carrying a confirmation minted for a different room or bot, one already used, or none
- **THEN** nothing is deleted and the refusal is recorded as its own event

#### Scenario: Deletion is never a bare link
- **WHEN** the rooms page or an identity page carrying a bot is navigated, prefetched or reloaded
- **THEN** nothing is deleted
