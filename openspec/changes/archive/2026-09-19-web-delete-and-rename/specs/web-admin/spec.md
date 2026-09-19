# Spec Delta

## ADDED Requirements

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
- **WHEN** the rooms or bots administration page is navigated, prefetched or reloaded
- **THEN** nothing is deleted

### Requirement: A stored identity is removed through the interface under the rules the command line applies
The system SHALL allow removing a stored identity through the interface, applying the refusals the
command line applies for the same removal: an identity a room or a bot is bound to SHALL be
refused, naming what it serves, and nothing SHALL be removed.

Removal SHALL be a guarded action per identity, carrying the acting user's password — it destroys
key material irrecoverably, which is the tier reveal and export are already in — and SHALL in
addition require the identity's name to be typed, as the command line requires. The confirmation
SHALL state what the removal costs in the same terms the command line states it, including
whether the stored key material can still be read from here, and that disabling is the reversible
action offered alongside it. The removal SHALL be recorded as its own structured event naming the
identity, the outcome and the acting user, whether it succeeds or is refused.

#### Scenario: Removing an identity nothing is bound to
- **WHEN** an identity nothing is bound to is removed with the operator's password and its name typed
- **THEN** the row is deleted, the removal is recorded as its own event naming the identity and the operator, and the identity is gone from the listing

#### Scenario: Removing an identity in use
- **WHEN** removal is asked for an identity a room or a bot is bound to
- **THEN** it is refused naming what it serves, nothing is removed, and the refusal is recorded

#### Scenario: Removal without the password
- **WHEN** a removal is submitted without the operator's correct password
- **THEN** nothing is removed and the refusal is recorded as its own event

#### Scenario: Removal with the name typed wrongly
- **WHEN** a removal is submitted with text that is not the identity's name
- **THEN** nothing is removed and the interface says it was not confirmed

#### Scenario: The cost is stated
- **WHEN** the removal confirmation for an identity is opened
- **THEN** it states that removal is irreversible, what is lost, and that disabling is the reversible action offered instead

### Requirement: Identities, rooms, channels and webhooks are renamed through the interface
The system SHALL allow renaming a stored identity, a room, a channel and a webhook through the
interface, applying the same validation and refusal rules the command line applies for the same
rename. A refused rename SHALL re-render the page with that reason and change nothing.

A rename SHALL NOT be a guarded action and SHALL NOT require the acting user's password: it is
reversible by renaming back and destroys nothing. A rename SHALL be recorded in the request's
event carrying the old and the new name.

The interface SHALL NOT offer a rename control for a bot. Where a bot is presented, it SHALL state
that a bot is named by the identity it is bound to, and link to that identity's rename.

A rename of a channel SHALL render no stored pre-shared key, and a rename of a webhook SHALL
render no stored URL beyond its scheme and host, including in a form re-shown after a refusal.

#### Scenario: Renaming a room, a channel or a webhook
- **WHEN** one of these is renamed through the interface
- **THEN** its stored result is indistinguishable from the same rename made through the command line, and nothing else about it is changed

#### Scenario: A refused rename
- **WHEN** a rename is submitted with an empty name or a name already in use
- **THEN** the page is re-rendered with the same reason the command line gives and nothing is renamed

#### Scenario: No password is asked
- **WHEN** a rename form is opened
- **THEN** it carries no password field, and a submission is decided without one

#### Scenario: Looking for a bot rename
- **WHEN** an operator looks at a bot for a way to rename it
- **THEN** the interface states that a bot is named by the identity it is bound to and links to that identity's rename

#### Scenario: A refused channel rename discloses nothing
- **WHEN** a rename of a pre-shared-key channel is refused
- **THEN** no pre-shared key, in any encoding, is present in the re-rendered page

### Requirement: Renaming an identity states its mesh consequence and offers to advert the new name
The system SHALL state, on the rename control for a stored identity, that the name travels in that
identity's adverts and that neighbours will keep showing the old name until the identity adverts
again.

Where the identity is loaded by the running process, the rename SHALL offer, in the same
submission, a zero-hop advert or a flood advert so that the new name propagates at once, with
neither chosen by default. Each offer SHALL state what that advert does in the same terms the
standalone advert confirmations state it, and SHALL be decided by exactly the rules that already
govern a requested advert — the transmit gate, the radio readback, whether the identity is loaded,
and for a flood the inter-entity gap — and SHALL be recorded as its own structured event as those
actions already are.

The rename and the advert SHALL be independent in their outcome: the rename SHALL be applied
before the advert is attempted, and a refused advert SHALL NOT undo it. The interface SHALL report
both outcomes separately, and SHALL give the refused advert's reason.

Where the identity is not loaded by the running process, the rename SHALL offer no advert and
SHALL state that this run does not hold that identity.

#### Scenario: Renaming with no advert chosen
- **WHEN** a loaded identity is renamed with neither advert chosen
- **THEN** the identity is renamed, nothing is transmitted, and the interface states that neighbours keep the old name until it adverts again

#### Scenario: Renaming and advertising the new name
- **WHEN** a loaded identity is renamed with a flood advert chosen while the gate is open, a readback exists and no flood is inside the inter-entity gap
- **THEN** the identity is renamed, exactly one flood advert carrying the new name is submitted, and the rename and the advert are each reported and each recorded as their own event

#### Scenario: The advert is refused and the rename is not
- **WHEN** a loaded identity is renamed with an advert chosen while the transmit gate is closed
- **THEN** the identity is renamed, nothing is submitted, and the interface reports the rename as applied and states that the advert was refused because transmission is disabled

#### Scenario: The rename is refused and nothing is advertised
- **WHEN** a rename is submitted with a name already in use and an advert chosen
- **THEN** nothing is renamed, nothing is submitted, and the page is re-rendered with the refusal

#### Scenario: The consequence is stated
- **WHEN** the rename control for a stored identity is opened
- **THEN** it states that the name travels in adverts and that neighbours keep the old name until the next one

#### Scenario: An identity this run does not hold
- **WHEN** the rename control is opened for a stored identity this run has not loaded
- **THEN** it offers no advert and states that this run does not hold that identity

#### Scenario: The schedule is not disturbed
- **WHEN** a loaded identity is renamed without an advert chosen
- **THEN** the identities page shows its next scheduled flood and its advert count for this run unchanged

## MODIFIED Requirements

### Requirement: Capabilities this build deliberately does not offer are named where they would be looked for
The system SHALL state, in the interface itself, which command-line capabilities it does not
expose and why, rather than leaving their absence to be discovered. This covers at minimum
applying migrations, generating the secret that seals stored identities, managing the
accounts that sign in to the interface, and revealing a stored channel pre-shared key.

#### Scenario: Looking for a migration control
- **WHEN** an operator looks at the schema information
- **THEN** the interface states that migrations are applied deliberately from a terminal and are not offered here, and why

#### Scenario: Looking for secret generation
- **WHEN** an operator looks at identity administration
- **THEN** the interface states that the sealing secret is generated from a terminal and not here, and why

#### Scenario: Looking for account management
- **WHEN** a signed-in operator looks for a way to add an account, change a password or disable an account
- **THEN** the interface names the terminal command that does it and states why it is not offered in the browser

#### Scenario: Looking for a channel's pre-shared key
- **WHEN** an operator looks at a pre-shared-key channel to share its key with someone
- **THEN** the interface names the terminal command that prints it and states why it is not shown in the browser

#### Scenario: Looking for a way to remove an identity
- **WHEN** an operator looks at identity administration for a way to remove a stored identity
- **THEN** removal is offered there as a guarded action, and the interface states that it is irreversible and that disabling is the reversible action offered alongside it
