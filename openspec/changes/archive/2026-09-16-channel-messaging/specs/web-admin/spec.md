## ADDED Requirements

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
