# runtime-cli Specification

## Purpose
The `sighop run` command that wires the modem, bus, scheduler and policies into a running
node: transmission stays off without an explicit flag, status reports the operating limits, and
rendered output never presents unverified content as verified.
## Requirements
### Requirement: The run command wires the platform together
The system SHALL provide a `sighop run` command that opens a modem source, decodes receptions,
deduplicates them, learns paths, fans out on the bus, and runs the transmit scheduler with its
advert stubs, in one process.

#### Scenario: Live run
- **WHEN** `sighop run` is invoked against a serial device
- **THEN** it performs the startup handshake and probe, then processes receptions and runs the scheduler until stopped

#### Scenario: Replay run
- **WHEN** `sighop run` is invoked with a capture file instead of a device
- **THEN** the same pipeline runs against the recorded events, so scheduler and dedup behaviour can be exercised without hardware

#### Scenario: Graceful stop
- **WHEN** the command is interrupted
- **THEN** it stops the scheduler, drains and logs its queued packets as dropped with a shutdown reason, and emits a final summary

### Requirement: Transmission requires an explicit flag
The system SHALL keep the transmit gate closed unless an explicit transmit-enable flag is given
on the command, and SHALL state the gate's state at startup and in every periodic status line.
Because the flag now keys a real transmitter rather than reaching a suppressed hand-off, the
startup output SHALL state that packets will be transmitted on air, name the identity or
identities that will originate them, and state the duty-cycle ceiling in force.

#### Scenario: Invoked without the flag
- **WHEN** `sighop run` is invoked with no transmit-enable flag
- **THEN** the startup output states that transmit is disabled and no `Data` frame is written for the run's lifetime

#### Scenario: Invoked with the flag
- **WHEN** `sighop run` is invoked with the transmit-enable flag
- **THEN** the startup output states prominently that packets will be transmitted on air, names each loaded entity identity with its public key and node hash, and states the configured duty-cycle ceiling

### Requirement: Periodic status reports the operating limits
The system SHALL print a periodic status line, at a configurable interval defaulting to 60
seconds, carrying at least: transmit gate state, duty-cycle usage over the rolling hour against
the ceiling, queue depth by priority class, packets suppressed and dropped, dedup hit rate and
cache occupancy, count of learned paths, and — because durability is now a property an operator
relies on — the persistence state, the count of packet-log rows discarded because logging fell
behind, and the count of routes discarded unwritten.

#### Scenario: Status during a receive-only run
- **WHEN** the status interval elapses during a gated run
- **THEN** the line reports the gate as closed, the duty-cycle usage that would have been consumed, and the remaining fields

#### Scenario: Duty-cycle usage approaching the ceiling
- **WHEN** the rolling-hour usage passes the reserve threshold
- **THEN** the status line marks the budget state distinctly so an operator can see that classes 2 and 3 are stalled

#### Scenario: Status with no database configured
- **WHEN** the status interval elapses on a run with no database configured
- **THEN** the line reports persistence as off, and the discard counters read zero rather than being omitted

#### Scenario: Status while the database is failing
- **WHEN** the status interval elapses while database writes are failing
- **THEN** the line marks persistence as degraded distinctly from off, and reports what has been discarded since the run began

### Requirement: Rendered output never presents unverified content as verified
The system SHALL render advert content, channel sender names and any other unauthenticated field
visually distinctly from cryptographically verified identities, in the run command's output as in
every other surface.

#### Scenario: Advert with an unverified signature
- **WHEN** a reception carries an advert whose signature does not verify
- **THEN** the rendered line marks it unverified and does not present its name in the form used for verified identities

#### Scenario: Stub entity is rendered
- **WHEN** the run's in-memory advert stubs are listed in the output
- **THEN** they are marked as ephemeral stubs, so they are not mistaken for persisted identities

### Requirement: Entity identities come from the entity store or from keyfiles
The system SHALL load local entities from the entity store when a database is configured, SHALL
accept one or more entity keyfiles on the `run` command in either case, SHALL report at startup
the name, public key, node hash and source of each, and SHALL apply the node-hash collision rule
across every entity loaded in the run whatever its source. Where no database is configured and no
keyfile is supplied, the existing ephemeral advert stubs remain available and SHALL continue to be
marked ephemeral.

#### Scenario: Run with an entity keyfile
- **WHEN** `sighop run` is given an entity keyfile
- **THEN** that identity is loaded, reported at startup with its public key, node hash and its keyfile as the source, and used for both advert origination and inbound message matching

#### Scenario: Run with persisted entities
- **WHEN** `sighop run` starts with a database holding enabled entities and no keyfile supplied
- **THEN** each enabled entity is loaded, reported at startup with its public key, node hash and the entity store as the source, and used for advert origination and inbound message matching

#### Scenario: Run with two colliding keyfiles
- **WHEN** two supplied keyfiles have public keys sharing their first byte
- **THEN** startup fails naming both files and their shared node hash, and nothing is transmitted

#### Scenario: A supplied keyfile collides with a persisted entity
- **WHEN** a supplied keyfile's public key shares its first byte with an enabled persisted entity
- **THEN** startup fails naming both the file and the stored entity and their shared node hash, and nothing is transmitted

### Requirement: The database is configured on the command line or from the environment
The system SHALL accept a database configuration from the environment and SHALL allow it to be
overridden on the command line, SHALL run with none supplied, and SHALL NOT print the
configuration's password in any output, log event or error message.

#### Scenario: Configured from the environment
- **WHEN** the runtime starts with a database configured in the environment and no command-line override
- **THEN** it connects to that database

#### Scenario: Overridden on the command line
- **WHEN** a database is given on the command line and another is present in the environment
- **THEN** the command-line value is used and the startup output names the host and database in force

#### Scenario: Configuration echoed in output
- **WHEN** the database configuration is named in startup output, a log event or a connection error
- **THEN** the password does not appear in it

### Requirement: Startup reports what persistence restored
The system SHALL report at startup whether it is running persistently or in memory, and when
persistent SHALL report the applied schema version, the number of entities, contacts and paths
restored, and the number of stored messages held — direct and channel alike — so the difference
between "nothing was heard yet" and "nothing was restored" is visible before any traffic arrives.
Posts whose outcome the last stop left unresolved SHALL be reported when there are any.

#### Scenario: Persistent start
- **WHEN** the runtime starts against a configured database
- **THEN** the startup output names the database in force, the applied schema version, and the counts of entities, contacts and paths restored

#### Scenario: Channel history held
- **WHEN** the runtime starts against a database holding channel messages
- **THEN** the same startup line states how many channel messages are held, beside the conversations and direct messages

#### Scenario: In-memory start
- **WHEN** the runtime starts with no database configured
- **THEN** the startup output states that state will not survive the process, in the same place a persistent run reports its counts

### Requirement: A database command applies and reports migrations
The system SHALL provide a command surface that applies outstanding migrations and reports the
database's current and expected schema versions, separate from the command that runs the node.
Applying migrations SHALL NOT be a side effect of running the node unless the run is given the
option that asks for it by name, which the container deployment passes.

#### Scenario: Applying migrations
- **WHEN** the migration command is run against a database behind the code
- **THEN** the outstanding migrations are applied in order and the resulting version is printed

#### Scenario: Reporting version
- **WHEN** the version command is run
- **THEN** the applied version and the version the code expects are printed, and whether they agree

#### Scenario: Running the node against an unmigrated database
- **WHEN** `sighop run` starts against a database that is not at the expected version, without `--migrate`
- **THEN** it fails naming both versions and the command that reconciles them, and applies nothing

#### Scenario: Running the node with `--migrate`
- **WHEN** `sighop run --migrate` starts against a database behind the code
- **THEN** the outstanding migrations are applied before the schema-version check and the run starts

#### Scenario: `--migrate` with no database
- **WHEN** `sighop run --migrate` starts with no database configured
- **THEN** it fails at startup naming `DATABASE_URL` and `--database-url`

### Requirement: A replay run does not write to the database by default
The system SHALL NOT persist contacts, paths or packet log rows learned from a replayed capture
unless persistence for replay is explicitly requested, and SHALL say at startup that a replay run
is not writing. Replayed receptions carry the timestamps of an earlier session, and writing them
as though they had just been heard would make a recorded contact indistinguishable from a live
one.

#### Scenario: Replay with a database configured
- **WHEN** `sighop run --replay` is invoked with a database configured and no explicit request to persist
- **THEN** the pipeline runs against the recorded events, nothing is written, and the startup output states that the replay is not persisting

#### Scenario: Replay with persistence explicitly requested
- **WHEN** a replay run is explicitly asked to persist
- **THEN** it writes as a live run would, and the startup output states that recorded state is being written

#### Scenario: Replay with no database configured
- **WHEN** `sighop run --replay` is invoked with no database configured
- **THEN** it behaves exactly as it does today

### Requirement: Identity management commands cover the entity store
The system SHALL extend its key management command surface with actions to list stored
identities, import a keyfile into the store, and export a stored identity to a keyfile, and SHALL
generate an encryption secret on request. None of these SHALL print a seed except the export,
which writes it to a file rather than to the terminal.

#### Scenario: Listing stored identities
- **WHEN** the list command is run against a configured database
- **THEN** each stored entity's name, type, public key, node hash and enabled state is printed, and no seed or ciphertext appears

#### Scenario: Importing a keyfile
- **WHEN** the import command is run with a keyfile
- **THEN** the identity is stored with its seed encrypted and the stored public key is printed

#### Scenario: Generating an encryption secret
- **WHEN** the secret generation command is run
- **THEN** a correctly-formed secret is printed once, with the statement that losing it makes every stored identity unrecoverable

#### Scenario: A store command run with no database configured
- **WHEN** a command that requires the entity store is run with no database configured
- **THEN** it fails saying a database is required for that action, and the keyfile commands remain usable

### Requirement: A key management command creates and inspects identities
The system SHALL provide a command that creates an entity keyfile and prints its public key, and
a command that prints an existing keyfile's name, node type, public key and node hash without
printing its seed.

#### Scenario: Creating an identity
- **WHEN** the key creation command is run with a name and an output path
- **THEN** a keyfile is written and the public key is printed in hex, suitable for entry into another node's contact list

#### Scenario: Inspecting an identity
- **WHEN** the key inspection command is run against a keyfile
- **THEN** the name, node type, public key and node hash are printed, and the seed is not

### Requirement: A message can be sent to a selected peer
The system SHALL accept a peer reference and a message text on the `run` command, SHALL send
the message once the peer resolves to a known contact, and SHALL continue running and receiving
afterwards. Where the peer cannot be resolved, the system SHALL say so and keep running rather
than exiting, since the peer's advert may not have been heard yet.

#### Scenario: Peer already known
- **WHEN** `sighop run` is given a peer reference and a message, and that peer is a known contact
- **THEN** the message is sent and its outcome — acknowledged, unacknowledged after its attempts, or dropped — is reported

#### Scenario: Peer not yet heard
- **WHEN** the supplied peer reference resolves to no contact
- **THEN** the output says the peer is unknown, no packet is queued, and the run continues receiving

### Requirement: Direct message activity is rendered as run output
The system SHALL render, as ordinary run output lines, each direct message sent with its
routing and attempt, each acknowledgement matched or unmatched, each inbound direct message
decrypted with its claimed sender and text, and each inbound direct message that no candidate
key could decrypt.

#### Scenario: A message exchange is rendered
- **WHEN** a message is sent, acknowledged, and a message is received and decrypted
- **THEN** each of those four events appears as a distinct output line carrying its packet identifier, and the decrypted message's sender is marked as claimed rather than verified

#### Scenario: An undecryptable direct message arrives
- **WHEN** a `TXT_MSG` addressed to a matching destination hash cannot be decrypted by any candidate
- **THEN** a line reports it with the number of candidate keys tried

### Requirement: A room command surface manages rooms, passwords and members
The system SHALL provide commands to create a room on a stored identity, to show a room's
configuration, to list rooms, to change a room's admin or guest password, to list and revoke
members, to set or clear a retention policy, to post to a room as the server itself, and to read a
room's stored history.

#### Scenario: Creating a room
- **WHEN** a room is created on a stored room-server identity
- **THEN** the room exists with the given name, its admin password stored as a hash, guest access refused unless configured, and retention unlimited, and the output states each of those

#### Scenario: Showing a room
- **WHEN** a room is shown
- **THEN** the output names its identity, its membership count, its message count, its guest-access setting and its retention policy, and never any password or hash

#### Scenario: Listing members
- **WHEN** a room's members are listed
- **THEN** each member's public key, permission level, sync position and last activity are reported

#### Scenario: Reading history
- **WHEN** a room's history is read
- **THEN** the stored messages are reported with author, ordering value and text, with text that is not valid UTF-8 marked as a rendering rather than presented as the author's text

#### Scenario: Posting as the server
- **WHEN** an operator posts to a room
- **THEN** the post is stored authored by the room's own identity and becomes deliverable to every member

### Requirement: A password is never accepted as a command-line argument
The system SHALL read every room password and every web account password from an interactive
prompt or from standard input, and SHALL NOT accept one as a command-line argument, because
process arguments are readable by other users on the host.

#### Scenario: A password is required
- **WHEN** a command that needs a password is run without one available on standard input
- **THEN** it prompts for the password without echoing it, rather than reading one from its arguments

#### Scenario: A password is supplied on the command line
- **WHEN** a password is passed as a command-line argument
- **THEN** the command refuses and says why, rather than accepting it

#### Scenario: A web account password is prompted twice
- **WHEN** a web account password is entered at an interactive prompt
- **THEN** it is asked for twice and refused if the two entries differ

### Requirement: Rotating a password and revoking a member each state their consequence
The system SHALL state, when a password is rotated, that existing members keep their access and
only new logins are gated, and SHALL state, when a member is revoked, that the member must log in
again and that its sync position and replay guard are discarded with it.

#### Scenario: Rotating the guest password
- **WHEN** a room's guest password is changed
- **THEN** the output says that existing members are unaffected and that removing a member requires revoking it

#### Scenario: Revoking a member
- **WHEN** a member is revoked
- **THEN** the output names the member and states that it must log in again and that its sync position is gone

### Requirement: A run serves the rooms that are configured and says what it is serving
The system SHALL serve every room bound to an enabled entity when it starts, SHALL report each of
them before any traffic is handled, and SHALL state plainly when no database is configured that no
rooms exist.

#### Scenario: A run with rooms configured
- **WHEN** the runtime starts with rooms in the database
- **THEN** each room, its identity, membership count, message count and retention policy are reported before the first frame is handled

#### Scenario: A run without a database
- **WHEN** the runtime starts with no database configured
- **THEN** the output states that no rooms are served because rooms require durable storage, rather than omitting the subject

#### Scenario: A room whose entity is disabled
- **WHEN** a room is bound to an entity that is not enabled
- **THEN** it is not served and is reported as not served, with the reason

### Requirement: A bot command surface manages bots, their mode and their configuration
The system SHALL provide commands to create a bot on a stored identity with a named driver, to list
bots, to show one, to enable and disable one, to switch one between observe and active mode, to set
its driver configuration, and to inspect its durable state. Creating a bot SHALL name the driver
explicitly and SHALL refuse an unknown driver, listing the drivers that exist.

#### Scenario: Creating a bot
- **WHEN** a bot is created on a stored identity with a named driver
- **THEN** the bot exists, enabled, in observe mode, with the driver's default configuration, and the output states each of those including that it will transmit nothing until it is made active

#### Scenario: Creating a bot with an unknown driver
- **WHEN** a bot is created naming a driver that does not exist
- **THEN** the command refuses and lists the available drivers

#### Scenario: Showing a bot
- **WHEN** a bot is shown
- **THEN** the output names its identity, its driver, its mode, its enablement, its configuration, its limits and its counters

#### Scenario: Switching a bot to active
- **WHEN** a bot is switched to active mode
- **THEN** the output states that the bot may now transmit, and that transmission still requires the run's transmit flag

#### Scenario: Setting driver configuration
- **WHEN** a configuration value is set that the driver rejects
- **THEN** the command refuses with the driver's reason and the stored configuration is unchanged

#### Scenario: Inspecting durable state
- **WHEN** a bot's state is inspected
- **THEN** the stored keys and values are reported, and clearing state states what it will make the bot do again

#### Scenario: Deciding per contact whether it has been greeted
- **WHEN** an operator clears or sets one contact's greeting record
- **THEN** the command names the contact it resolved, and states what the bot will now do about it

### Requirement: A run runs the bots that are configured and says what it is running
The system SHALL run every enabled bot bound to an enabled entity when it starts, SHALL report each
of them before any traffic is handled — with its driver, mode and limits — SHALL report a bot it
did not run together with the reason, and SHALL state plainly when no database is configured that
no bots are running because bots require durable storage.

#### Scenario: A run with bots configured
- **WHEN** the runtime starts with bots in the database
- **THEN** each bot, its identity, driver, mode and limits are reported before the first frame is handled

#### Scenario: A run without a database
- **WHEN** the runtime starts with no database configured
- **THEN** the output states that no bots are run because bots require durable storage, rather than omitting the subject

#### Scenario: A bot that is not run
- **WHEN** a bot is disabled, or bound to an entity that is not enabled
- **THEN** it is reported as not running, with the reason

### Requirement: Bot activity is rendered as run output
The system SHALL render each bot decision as it happens — an action taken, an action that would
have been taken in observe mode, or an action suppressed with its reason — naming the bot and the
peer, and SHALL include per-bot counters in the periodic status report, so that an operator
watching a run can tell a bot that is deciding not to act from one that is not being asked to act.

#### Scenario: A driver acts
- **WHEN** a bot transmits
- **THEN** a line names the bot, the peer, and the outcome including whether it was acknowledged

#### Scenario: A driver would have acted
- **WHEN** a bot in observe mode decides to act
- **THEN** a line marks it plainly as an observation that transmitted nothing, and is not presented as a transmission

#### Scenario: Periodic status
- **WHEN** the periodic status report is emitted
- **THEN** each running bot's actions, observations, suppressions by reason, dropped dispatches and driver failures are included

### Requirement: A run serves the web interface when asked and says where
The system SHALL provide options on the run command to enable the web interface, to choose its
listening address and port, and to name additional host names it answers to, SHALL default the
address to loopback, and SHALL report at startup whether the interface is being served and, when
it is, the address and port it is listening on and how many enabled accounts can sign in. A run
not asked for the interface SHALL report nothing about it. A run asked for the interface SHALL
fail at startup, before any traffic is processed and before any port is listened on, when no
database is configured, or when the database holds accounts and none of them is enabled, naming
the commands that resolve it. When the database holds no account at all, the run SHALL instead
serve the interface in first-run setup, and its startup output SHALL state that setup is pending,
give the address of the setup form and give the one-time setup code; the startup event SHALL
record that setup is pending and SHALL NOT carry the code.

#### Scenario: A run with the interface enabled
- **WHEN** the run command is given the web option with a database holding at least one enabled account
- **THEN** startup reports the address and port the interface is listening on and the number of enabled accounts

#### Scenario: A run without the interface
- **WHEN** the run command is not given the web option
- **THEN** startup says nothing about a web interface and no port is listened on

#### Scenario: A non-loopback address
- **WHEN** the interface's address is set to a non-loopback address
- **THEN** startup states that the interface is reachable from the network over plain HTTP and that passwords and session cookies are unencrypted in transit, alongside the address and port

#### Scenario: The port cannot be bound
- **WHEN** the configured port cannot be bound
- **THEN** startup fails naming the address, the port and the reason, in the same way a configured database that cannot be reached fails

#### Scenario: The interface without a database
- **WHEN** the run command is given the web option and no database is configured
- **THEN** startup fails stating that the web interface's accounts are stored in the database, and nothing is received or transmitted

#### Scenario: The interface with no account at all
- **WHEN** the run command is given the web option and the database holds no account
- **THEN** the interface is served, startup output states that first-run setup is pending with the setup form's address and the setup code, the startup event records setup as pending without the code, and the run otherwise starts as any run does

#### Scenario: The interface with no account to sign in with
- **WHEN** the run command is given the web option and the database holds accounts none of which is enabled
- **THEN** startup fails naming the command that enables an account and the command that adds one, and no port is listened on

#### Scenario: A non-loopback address during setup
- **WHEN** first-run setup is served on a non-loopback address
- **THEN** the plain-HTTP warning is stated exactly as for any other run, alongside the setup code

### Requirement: Direct message activity from the interface is rendered as run output
The system SHALL render a message sent from the web interface, and its outcome, in the run's output
exactly as it renders one sent from the command line, naming the identity it was sent as, so that
an operator watching the terminal sees everything the platform transmits regardless of which
surface asked for it.

#### Scenario: A message sent from the browser
- **WHEN** a message is sent from the web interface
- **THEN** the run's output reports it and its outcome in the same form as a command-line send

#### Scenario: A guarded action taken in the browser
- **WHEN** transmission is enabled or the airtime ceiling is raised from the web interface
- **THEN** the run's output states that the change was made, what it now is, and which account made it

### Requirement: A web account command surface manages the accounts that sign in to the interface
The system SHALL provide commands to add an account, list accounts, set an account's password,
disable an account, enable it again and remove it. Each SHALL require a configured database, SHALL
state its consequence for sessions already signed in, and SHALL refuse a change that would leave
the database with no enabled account only when the operator has not explicitly acknowledged it.
A change that leaves the database with no account at all SHALL state that the next run serving the
interface will offer first-run setup.

#### Scenario: Adding an account
- **WHEN** an account is added with a username
- **THEN** the password is read by prompt or standard input, the account is stored enabled, and the command states that it can sign in to any run using this database

#### Scenario: Setting a password
- **WHEN** an account's password is set
- **THEN** the command states that sessions signed in with the old password end within a minute on every run using this database

#### Scenario: Disabling the last enabled account
- **WHEN** the only enabled account is disabled, or removed while other disabled accounts remain, without the explicit acknowledgement option
- **THEN** the command refuses, stating that no run could then start its web interface

#### Scenario: Removing the only account
- **WHEN** the only account is removed without the explicit acknowledgement option
- **THEN** the command refuses, stating that the next run serving the interface would offer first-run setup to whoever holds its setup code

#### Scenario: Removing the only account with acknowledgement
- **WHEN** the only account is removed with the explicit acknowledgement option
- **THEN** it is removed and the command states that the next run serving the interface will offer first-run setup

#### Scenario: Listing accounts
- **WHEN** accounts are listed
- **THEN** each account's username, enabled state, creation time and password-set time are shown, and no hash is shown

#### Scenario: Listing with no accounts
- **WHEN** accounts are listed and none exists
- **THEN** the command states that a run serving the interface will offer first-run setup, and names the command that adds an account from the terminal

#### Scenario: Without a database
- **WHEN** any account command is run with no database configured
- **THEN** it fails stating that accounts are stored in the database

### Requirement: A webhook command surface manages webhooks
The system SHALL provide commands to add a webhook, list webhooks, show one, enable and disable one,
change its triggers, format and maximum hop count, replace its URL, remove it, and send it a sample
event. A URL SHALL be read from standard input and SHALL NOT be accepted as a command-line argument,
because arguments are visible in process listings and shell history. Every refusal SHALL be the one
the stored-configuration rules make, with its reason.

#### Scenario: Adding a webhook
- **WHEN** a webhook is added with a name, a format and triggers, and its URL on standard input
- **THEN** the output states it is enabled, its format, its triggers, its hop limit, and its target as scheme and host only

#### Scenario: A URL given as an argument
- **WHEN** an operator tries to pass the URL as a command-line argument
- **THEN** no such argument exists, and the help states that the URL is read from standard input

#### Scenario: Showing a webhook
- **WHEN** a webhook is shown
- **THEN** the output names its format, triggers, hop limit, enablement, target host, and its last successful and last failed delivery with the failure reason

#### Scenario: Testing a webhook
- **WHEN** a webhook is tested with a named trigger
- **THEN** one sample event is sent and the output states whether it was delivered, with the HTTP status or the failure reason

#### Scenario: Removing a webhook
- **WHEN** a webhook is removed
- **THEN** it is deleted, and a running process stops delivering to it for events raised afterwards

#### Scenario: No database configured
- **WHEN** any webhook command is run with no database configured
- **THEN** the command refuses and states that webhooks require durable storage

### Requirement: A run reports the webhooks it will deliver to
The system SHALL report at startup, before any traffic is handled, how many webhooks are enabled and
which triggers they subscribe to; SHALL state plainly when no webhooks will be sent because no
database is configured or because the run is a replay; and SHALL include webhook delivery counters
— delivered, failed and dropped — in the periodic status line whenever webhooks are active.

#### Scenario: A run with webhooks configured
- **WHEN** the runtime starts with enabled webhooks in the database
- **THEN** the startup output states the count of enabled webhooks and the triggers they cover

#### Scenario: A replay run
- **WHEN** a replay run starts with webhooks in the database
- **THEN** the startup output states that no webhooks are sent during a replay

#### Scenario: Periodic status
- **WHEN** the periodic status line is emitted during a run with webhooks active
- **THEN** it includes the delivered, failed and dropped webhook counts

### Requirement: A channel command surface manages channels
The system SHALL provide commands to add a channel, list channels, show one, remove one, print one's
key, and print one's recent history. A channel SHALL be added as the Public channel, from a hashtag, from
a pre-shared key read from standard input, or with a newly generated 16-byte pre-shared key that is
printed once. A pre-shared key SHALL NOT be accepted as a command-line argument. Every refusal SHALL be
the one the stored-configuration rules make, with its reason.

#### Scenario: Adding a hashtag channel
- **WHEN** a channel is added from the hashtag `#dev-sighop`
- **THEN** the output states its name, kind, channel hash, and that anyone who guesses the hashtag can read and post in it

#### Scenario: A key given as an argument
- **WHEN** an operator tries to pass a pre-shared key as a command-line argument
- **THEN** no such argument exists, and the help states that the key is read from standard input or generated

#### Scenario: Generating a key
- **WHEN** a channel is added with a generated key
- **THEN** the output prints the key once in base64, states that it is the credential for reading and posting in the channel, and names the command that prints it again

#### Scenario: Printing a key
- **WHEN** a channel's key is printed
- **THEN** the base64 key is written to standard output, and for a hashtag or Public channel the output states that the key is derivable by anyone

#### Scenario: Removing a channel
- **WHEN** a channel with recorded messages is removed
- **THEN** the command states how many messages will be deleted and requires confirmation, or a flag stating the operator accepts that, before deleting

#### Scenario: Reading history
- **WHEN** a channel's history is printed
- **THEN** each received message's sender is rendered as an unverified claim, and each post names the identity that posted it with its outcome and repeats heard

#### Scenario: A post by an identity the entity store does not hold
- **WHEN** a channel's history holds a post made by an identity loaded from a keyfile rather than from the entity store
- **THEN** the post's line identifies it by its public key and states that the store does not hold it, rather than stating that it was removed

#### Scenario: No database configured
- **WHEN** any channel command is run with no database configured
- **THEN** the command refuses and states that channels require durable storage

### Requirement: A run reports the channels it has loaded
The system SHALL report at startup, before any traffic is handled, the channels loaded with their
names and channel hashes, any channel skipped because its key could not be opened, and plainly that
no channels are loaded when no database is configured; and SHALL include channel counters — decrypted,
unknown channel, undecryptable, posts transmitted, repeats heard — in the periodic status line.

#### Scenario: A run with channels
- **WHEN** a run starts with the Public channel and one hashtag channel stored
- **THEN** the startup output lists both with their channel hashes

#### Scenario: Periodic status
- **WHEN** the periodic status line is emitted during a run with channels loaded
- **THEN** it includes the channel counters

### Requirement: Channel activity is rendered as run output
The system SHALL render each decrypted channel message, each post and its outcome, each post
submitted from the web interface naming the account that submitted it, and each reload that adopted a
different set of channels, as run output. A claimed sender name SHALL be rendered with the same
unverified marking used for other unauthenticated content.

#### Scenario: A received channel message
- **WHEN** a channel message is decrypted during a run
- **THEN** one output line names the channel, the hop count and the claimed sender marked unverified, and the text

#### Scenario: A post from the interface
- **WHEN** a channel post is submitted from the web interface
- **THEN** the run output names the channel, the identity, and the account that submitted it

#### Scenario: A channel added or removed elsewhere
- **WHEN** a run adopts a channel set that differs from the one it held
- **THEN** one output line names the channels added and removed and how many are now loaded
