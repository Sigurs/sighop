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
importing and exporting one, and SHALL state what an exported keyfile is: an unencrypted seed
protected only by its file permissions.

#### Scenario: Listing identities
- **WHEN** the identities page is opened
- **THEN** every stored identity is listed with its public key and node hash in full

#### Scenario: Exporting an identity
- **WHEN** an identity is exported
- **THEN** the interface states that the exported material is an unencrypted seed and that it offers protection different from the store's

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
The system SHALL list the configured bots with their driver, bound identity, enabled state and
mode, SHALL allow creating one, enabling and disabling it, switching between observing and active,
and editing its driver configuration, and SHALL state that switching a bot to active means it will
transmit without further prompting.

#### Scenario: Switching a bot to active
- **WHEN** a bot's mode is changed to active
- **THEN** the interface states that the bot will transmit unprompted, and the change is confirmed explicitly

#### Scenario: Invalid driver configuration
- **WHEN** a driver configuration is submitted that the driver rejects
- **THEN** the change is refused with the driver's own reason and the stored configuration is unchanged

#### Scenario: A bot's durable state is visible
- **WHEN** a bot's page is opened
- **THEN** the state the bot has persisted is readable, including the records that decide whether it will act on a given contact again

### Requirement: Radio parameters are shown as the board reports them, and a change states its scope
The system SHALL display the radio parameters in force as the board's own readback reports them,
and SHALL, where it offers to change them, state that the change is not persisted by the board and
is lost on a board reset.

#### Scenario: Displaying the radio
- **WHEN** the radio page is opened
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
The system SHALL show the revision the database reports and the revision this code expects,
and SHALL say whether they agree. It SHALL NOT offer to apply migrations.

#### Scenario: A database at the expected revision
- **WHEN** the schema page is opened against a database at the revision the code expects
- **THEN** both revisions are shown and the interface says they agree

#### Scenario: A database at a different revision
- **WHEN** the revisions differ
- **THEN** both are named, the disagreement is stated, and the command that reconciles them is given

#### Scenario: Migrations are not applied from the interface
- **WHEN** the schema page is used
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
The system SHALL list the stored channels with their name, kind, channel hash, guessable marking and
recorded message count; SHALL allow adding a channel from a hashtag or from a pasted pre-shared key,
and re-adding the Public channel if it was removed; and SHALL allow removing a channel through an
explicit confirmation that states how many messages will be deleted. A stored pre-shared key SHALL NOT
be rendered anywhere in the interface, including in a form re-shown after a refused addition.

#### Scenario: Adding a channel
- **WHEN** a channel is added through the interface
- **THEN** its stored result is indistinguishable from the same channel added through the command line, and the running process decrypts on it without restart

#### Scenario: Adding a hashtag channel
- **WHEN** a hashtag channel is added through the interface
- **THEN** the result states that anyone who guesses the hashtag can read and post in it

#### Scenario: A refused pre-shared key
- **WHEN** a pasted pre-shared key is refused
- **THEN** the form is re-shown with the reason the command line gives and with the key field empty

#### Scenario: Viewing stored channels
- **WHEN** the channels page is opened
- **THEN** no pre-shared key, in any encoding, is present in the page source

#### Scenario: No database configured
- **WHEN** the channels page is opened on a run with no database
- **THEN** the page states that channels require durable storage and offers no controls
