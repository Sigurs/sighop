## ADDED Requirements

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

## MODIFIED Requirements

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
