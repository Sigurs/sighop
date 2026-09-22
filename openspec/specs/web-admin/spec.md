# web-admin Specification

## Purpose

Configuration through the browser: the identities the platform runs, the rooms and bots bound to
them, their passwords, retention and driver settings, and the radio the whole thing sits on. It is
also where the platform's most dangerous actions live — revealing a private key, opening the
transmit gate, raising the airtime ceiling — so it is where those actions are made deliberate and
recorded.

## Requirements

### Requirement: Configuration writes go through the same rules the command line uses
The system SHALL apply every configuration change made through the interface using the same
validation, uniqueness and refusal rules that the command line applies for the same change, so that
no rule exists in one surface and not the other.

#### Scenario: A refusal the command line would make
- **WHEN** a change is submitted that the command line would refuse
- **THEN** the interface refuses it for the same reason and states that reason

#### Scenario: A change made in either surface
- **WHEN** a change is made through the interface
- **THEN** its stored result is indistinguishable from the same change made through the command line

### Requirement: Identities are listed, created and managed
The system SHALL list the stored identities with their name, type, public key, node hash, enabled
state and creation time, SHALL allow creating a new identity, enabling and disabling one, and
importing and exporting one, and SHALL state what an exported keyfile is: an unencrypted private
key protected only by its file permissions. Creating an identity SHALL accept an optional private
key supplied by the operator and use it in place of generating one, and SHALL refuse a supplied
key for the same reasons and in the same words as the command line does, re-rendering the page
with the reason and creating nothing.

#### Scenario: Listing identities
- **WHEN** the identities page is opened
- **THEN** every stored identity is listed with its public key and node hash in full

#### Scenario: Creating an identity from a supplied private key
- **WHEN** an identity is created with a private key supplied in the form
- **THEN** the stored identity has the public key and node hash that key derives, no key is generated, and the identity is indistinguishable from one the command line created from the same key

#### Scenario: Creating an identity with the key field left empty
- **WHEN** an identity is created with the private key field left empty
- **THEN** a key is generated as before

#### Scenario: A supplied private key the system will not accept
- **WHEN** an identity is created with a private key of the wrong length, one that is not hexadecimal, one that is not clamped, or one deriving a reserved or colliding node hash
- **THEN** the page is re-rendered with the same reason the command line gives, the other fields the operator typed are preserved, and no identity is stored

#### Scenario: Exporting an identity
- **WHEN** an identity is exported
- **THEN** the interface states that the exported material is an unencrypted private key and that it offers protection different from the store's

#### Scenario: Disabling an identity in use
- **WHEN** an identity bound to a running room or bot is disabled
- **THEN** the interface states what that identity is serving before the change is applied

### Requirement: Revealing key material, enabling transmit, and raising the ceiling are re-confirmed and audited
The system SHALL treat revealing a private key, enabling transmission, and raising the airtime
ceiling above the regulatory default as distinct guarded actions. Each SHALL require an explicit
confirmation of that specific action carrying the signed-in user's password, SHALL NOT be
reachable by following a link or by a request that could be issued incidentally, and SHALL emit
its own structured event naming the action, its target, its outcome and the acting user.

#### Scenario: Revealing a private key
- **WHEN** an operator asks to see an identity's private key material
- **THEN** the action is confirmed explicitly with the operator's password, the reveal is recorded as its own event naming the identity and the operator, and the material is not present in any page served before that confirmation

#### Scenario: Enabling transmission
- **WHEN** transmission is enabled through the interface
- **THEN** the action is confirmed explicitly with the operator's password and recorded as its own event naming the operator, and the panel's gate indication changes to match

#### Scenario: Raising the ceiling
- **WHEN** the airtime ceiling is raised above the regulatory default
- **THEN** the action is confirmed explicitly with the operator's password, the confirmation states that the default is a regulatory limit, and the action is recorded as its own event carrying the old and new values and the operator

#### Scenario: A guarded action is never a bare link
- **WHEN** the interface is navigated
- **THEN** no guarded action is performed by a request that a browser could issue by following, prefetching or reloading a page

### Requirement: Rooms are configured through the interface
The system SHALL allow creating a room on an identity, listing rooms with their member and message
counts, setting and rotating the administrator and guest passwords, setting guest access and
read-only fallback, and setting or clearing the two retention bounds, and SHALL state the
consequence of a password rotation and of a member revocation before either is applied.

#### Scenario: Rotating a password
- **WHEN** a room password is rotated
- **THEN** the interface states, before applying it, that existing members must log in again with the new password

#### Scenario: Setting retention
- **WHEN** a retention bound is set
- **THEN** the interface states how many stored messages the bound would remove before it is applied

#### Scenario: A password is never placed in a URL
- **WHEN** a password is submitted
- **THEN** it is not carried in a query string, not reflected in any served page, and not recorded in the request event

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

### Requirement: Exporting an identity is a guarded action
The system SHALL treat writing a stored identity's key material out of the platform as a
guarded action of the same kind as revealing it: confirmed explicitly for that specific
identity with the signed-in user's password, produced in exactly one response, not reachable by
following a link, and recorded as its own structured event naming the identity and the acting
user. The exported material SHALL be the same unencrypted seed the command line writes, so that
a file produced here and a file produced by the command line are interchangeable. The interface
SHALL state how the two differ in protection: the command line writes the file with owner-only
permissions in the same call that creates it, and a file delivered to a browser has whatever
protection the browser's download location gives it, which is usually none.

#### Scenario: Exporting an identity
- **WHEN** an operator asks to export a stored identity
- **THEN** the action is confirmed explicitly with the operator's password, the file is produced in that one response, and the export is recorded as its own event naming the identity and the operator

#### Scenario: An export that was not confirmed
- **WHEN** an export is requested without the confirmation that view issued, or without the operator's correct password
- **THEN** nothing is produced, and the refusal is recorded as its own event

#### Scenario: The file is the command line's file
- **WHEN** an identity is exported through the interface and through the command line
- **THEN** the two files carry the same identity and are usable interchangeably

#### Scenario: The difference in protection is stated
- **WHEN** an export is offered
- **THEN** the interface states that the command line's file is created owner-only and a downloaded one is not

### Requirement: A bot's greeting records can be changed, and each change states what it releases
The system SHALL allow an operator to change the records that decide whether a bot will act
on a given contact again: clearing one, setting one, and seeding them for every contact
already known. It SHALL state, before a record is cleared, that clearing it makes that
contact eligible to be acted on again — including transmitted at, when the bot is active.

#### Scenario: Clearing a record
- **WHEN** a greeting record is cleared for a contact
- **THEN** the interface states beforehand that the bot may act on that contact again, and afterwards the record is gone

#### Scenario: Recording a contact as already acted on
- **WHEN** a contact is recorded as already greeted
- **THEN** the bot will not act on that contact, and the record shows that an operator set it

#### Scenario: Seeding an existing store
- **WHEN** records are seeded for every contact already known
- **THEN** contacts that already had a record keep the one they had

### Requirement: A bot's durable state can be cleared, and what that forgets is stated
The system SHALL allow an operator to clear everything a bot has persisted, and SHALL state
before doing so what that forgets — for a greeter, every record of who has been greeted,
which makes every known contact eligible again.

#### Scenario: Clearing a bot's state
- **WHEN** a bot's durable state is cleared
- **THEN** the interface states what is forgotten before the change is applied, and reports how many keys were removed

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

### Requirement: A loaded identity can be told to advert now, zero-hop or flood
The system SHALL allow an operator to request one zero-hop advert or one flood advert for any
identity loaded by the running process, whether it serves a room, a bot or nothing, and whether its
key is stored, loaded from a keyfile or generated for this run. Each is a distinct guarded action
per identity: it SHALL be performed only by a submission from a confirmation view that states what
that action does and mints a confirmation for that action and that identity alone. It SHALL NOT be
reachable by following, prefetching or reloading a page. It SHALL NOT require the operator's
password. It SHALL emit its own structured event naming the action, the identity, the outcome and
the acting user, whether it succeeds or is refused. A successful request SHALL also be stated in
the run's own output, naming the operator.

The zero-hop confirmation SHALL state that the advert reaches direct neighbours only and leaves the
flood schedule unchanged. The flood confirmation SHALL state that the advert is repeated by every
repeater in the mesh, and that it takes the place of the identity's next scheduled flood, which
moves one full interval out. Each confirmation SHALL show the identity's next scheduled flood as
it stands.

The system SHALL refuse the request and submit nothing when any of these holds, and the refusal
SHALL state which:
- the transmit gate is closed
- the run has no radio readback to compute airtime from
- the identity is not loaded by this run
- for a flood only, a flood advert from any identity of this run was submitted less than the
  inter-entity gap ago; the refusal SHALL state the time remaining

The system SHALL NOT impose any other limit on how often an operator may request either advert.

#### Scenario: Requesting a zero-hop advert
- **WHEN** an operator confirms a zero-hop advert for a loaded identity while the gate is open and a radio readback exists
- **THEN** exactly one zero-hop advert is submitted for that identity, its next scheduled flood is unchanged, the action is recorded as its own event naming the identity and the operator with outcome success, and the run's output states the request

#### Scenario: Requesting a flood advert
- **WHEN** an operator confirms a flood advert for a loaded identity while the gate is open, a radio readback exists, and no flood from this run is inside the inter-entity gap
- **THEN** exactly one flood advert is submitted for that identity, its next scheduled flood moves one interval out, and the action is recorded as its own event with outcome success

#### Scenario: The confirmations state the cost
- **WHEN** the flood confirmation for an identity is opened
- **THEN** it states that every repeater in the mesh repeats the advert and that the identity's next scheduled flood moves out, and shows when that flood is currently due; and the zero-hop confirmation states that the advert stops at direct neighbours

#### Scenario: No password is asked
- **WHEN** either confirmation is opened
- **THEN** it carries no password field, and a submission is decided without one

#### Scenario: The transmit gate is closed
- **WHEN** either advert is confirmed while the transmit gate is closed
- **THEN** nothing is submitted, the identity's next scheduled flood is unchanged, no airtime is charged, the refusal states that transmission is disabled, and the refusal is recorded as its own event

#### Scenario: No radio readback yet
- **WHEN** either advert is confirmed before the run has a radio readback
- **THEN** nothing is submitted, the refusal states that airtime cannot be computed until the board has answered, and the refusal is recorded

#### Scenario: A flood inside the inter-entity gap
- **WHEN** a flood advert is confirmed less than the inter-entity gap after any flood advert from this run
- **THEN** nothing is submitted, the refusal states the seconds remaining before another flood is accepted, and the refusal is recorded

#### Scenario: A zero-hop advert inside the inter-entity gap
- **WHEN** a zero-hop advert is confirmed less than the inter-entity gap after a flood advert from this run
- **THEN** it is accepted as if no flood had been sent

#### Scenario: A confirmation minted for another identity or the other kind
- **WHEN** a submission carries a confirmation minted for a different identity, for the other advert kind, one already used, or none
- **THEN** nothing is submitted and the refusal is recorded as its own event

#### Scenario: An identity this run does not hold
- **WHEN** an advert is requested for an identity this run has not loaded
- **THEN** nothing is submitted and the refusal states that this run does not hold that identity

#### Scenario: Repeated requests are not throttled beyond the gap
- **WHEN** an operator confirms a second zero-hop advert for the same identity immediately after the first
- **THEN** it is submitted like the first

#### Scenario: Reaching the actions from a room or a bot
- **WHEN** the rooms or bots administration list shows a room this run serves or a bot this run runs
- **THEN** it links to the advert actions for the identity that room or bot speaks as, and a room or bot this run does not serve offers no such link

### Requirement: Each loaded identity's advert schedule is visible
The system SHALL show, for every identity loaded by the running process, when its next flood
advert is scheduled, when it last flooded in this run or that it has not, how many adverts it has
sent in this run, and, while one is active, the override's interval and expiry.

#### Scenario: An identity that has not yet flooded
- **WHEN** the identities page is opened in a run where an identity has sent no flood advert
- **THEN** its next scheduled flood time is shown, and its last flood is shown as none this run

#### Scenario: An identity with an active override
- **WHEN** an identity has an active advert override
- **THEN** the override's interval and expiry are shown beside its schedule

#### Scenario: The schedule after a requested flood
- **WHEN** a flood advert has just been requested for an identity through the interface
- **THEN** the identities page shows its last flood as that request and its next scheduled flood one interval later

### Requirement: Webhooks are configured through the interface
The system SHALL list the configured webhooks with their name, format, triggers, hop limit, enabled
state, target shown as scheme and host only, and last successful and last failed delivery with the
failure reason; SHALL allow adding one, enabling and disabling it, changing its triggers, format and
hop limit, replacing its URL, removing it, and sending it a sample event of a chosen trigger with
the outcome shown. A stored URL SHALL NOT be rendered in full anywhere in the interface, including
in a form re-shown after a refused change. Removing a webhook SHALL be confirmed explicitly.

#### Scenario: Adding a webhook
- **WHEN** a webhook is added through the interface
- **THEN** its stored result is indistinguishable from the same webhook added through the command line

#### Scenario: A refused URL
- **WHEN** a webhook is submitted with a URL whose scheme is not `http` or `https`
- **THEN** the change is refused for the same reason the command line gives, and nothing is stored

#### Scenario: Viewing a stored webhook
- **WHEN** the webhooks page is opened
- **THEN** each target appears as scheme and host, and no URL path or query is present in the page source

#### Scenario: Testing from the interface
- **WHEN** an operator sends a sample event to a webhook
- **THEN** the page shows whether it was delivered, with the HTTP status or the failure reason

#### Scenario: No database configured
- **WHEN** the webhooks page is opened on a run with no database
- **THEN** the page states that webhooks require durable storage and offers no controls

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

### Requirement: Identity, room and bot writes reach the running process without a restart

The system SHALL apply a write made through the interface to the running process that served the
page, without a restart and without waiting for any periodic re-read, for every write that changes
what that process holds: creating, importing, enabling, disabling and removing an identity,
changing an identity's advert configuration, creating a room, and creating, enabling and disabling
a bot. This is the behaviour the interface already gives for adding a channel and for renaming an
identity, and it SHALL be the behaviour of these surfaces too.

The interface SHALL state the effect on the running process in the result of each such write: that
the identity is now held by this run and advertising, that it is no longer held, that the room is
now served, or that the bot is now running — and, where the write did not reach this run, why.

Where the write is stored but the running process does not take it up, the system SHALL state that
distinction plainly rather than reporting success: the store took the write and this run did not,
and an operator must be able to tell those apart without reading the event stream.

#### Scenario: Creating an identity

- **WHEN** an identity is created through the interface
- **THEN** the running process holds it and adverts for it without a restart, and the result states that it is now held by this run

#### Scenario: Disabling an identity

- **WHEN** a loaded identity is disabled through the interface
- **THEN** the running process stops advertising for it without a restart, and the result states that this run no longer holds it

#### Scenario: Removing an identity

- **WHEN** a stored identity is removed through the interface under the rules the command line applies
- **THEN** the running process withdraws it without a restart, and the result states that this run no longer holds it

#### Scenario: Creating a room

- **WHEN** a room is created through the interface on an identity this run holds
- **THEN** the running process serves it without a restart, and the result states that the room is now served

#### Scenario: Creating a bot

- **WHEN** a bot is created through the interface on an enabled entity this run holds
- **THEN** the running process runs it without a restart, and the result states that the bot is now running

#### Scenario: Stored but not taken up

- **WHEN** a write is stored and the running process does not take it up, because the identity is not enabled or its node hash collides with one this run holds
- **THEN** the result states that the store took the write, that this run did not take it up, and the reason

#### Scenario: The identities page reflects the change at once

- **WHEN** the identities page is reloaded after any of these writes
- **THEN** it shows the identities this run now holds, with their advert schedules, matching what the run is actually doing

### Requirement: A disabling that stops a room or a bot states that before it is applied

The system SHALL state, before disabling or removing an identity that a served room or a running
bot is bound to, that the running process will stop serving that room or running that bot, naming
it. The interface already states what such an identity is serving; it SHALL also state that the
consequence is immediate and does not wait for a restart, so that an operator is not told about a
binding while being left to assume the effect is deferred.

The system SHALL state that a room's membership and stored messages, and a bot's durable state,
survive the disabling and return if the identity is enabled again.

#### Scenario: Disabling an identity that serves a room

- **WHEN** disabling is offered for an identity a served room is bound to
- **THEN** the interface names the room, states that this run will stop serving it immediately, and states that the room's members and messages survive

#### Scenario: Disabling an identity that runs a bot

- **WHEN** disabling is offered for an identity a running bot is bound to
- **THEN** the interface names the bot, states that this run will stop running it immediately, and states that the bot's durable state survives

#### Scenario: Disabling an identity held as a default

- **WHEN** disabling is offered for an identity an operator holds as their default chat identity
- **THEN** the interface states that this run will stop holding it and that no identity will be preselected in that operator's composers
