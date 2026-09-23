# Spec Delta

## ADDED Requirements

### Requirement: Operator accounts are durable, hashed, and created only by first-run setup
The system SHALL store operator accounts in the database, each with a unique username, a password
stored only as a salted memory-hard hash whose parameters travel with it, an enabled state, the time
its password was last set, and the account's default chat identity where one is set. Usernames SHALL
be compared case-insensitively and stored in one normalised form. The system SHALL NOT store, log or
render a password or its hash anywhere else.

An account SHALL come to exist in exactly one way: through first-run setup, once, while the database
holds no account at all. The system SHALL offer no other way to create, list, re-password, disable,
re-enable or remove an account. An account's default chat identity is an interface preference rather
than an account operation: it carries no privilege, it SHALL be settable and clearable by the
signed-in operator it belongs to and by no one else, and an operator SHALL NOT be able to read or
change another account's preference.

#### Scenario: The first account is created
- **WHEN** first-run setup creates an account with a username and a password
- **THEN** the account is stored with a hash of the password and never the password itself, and it is enabled, with no default chat identity

#### Scenario: No second account can be created
- **WHEN** an account already exists
- **THEN** no surface offers to create, re-password, disable or remove an account, and first-run setup is closed

#### Scenario: A preference belongs to one account
- **WHEN** an operator sets their default chat identity
- **THEN** it is stored against their own account only, and no other account's preference is read or changed

## REMOVED Requirements

### Requirement: Operator accounts are durable, hashed, and managed from the terminal
**Reason**: The terminal surface that managed them is removed with the command line, and this change
deliberately adds no replacement in the browser.
**Migration**: Replaced by "Operator accounts are durable, hashed, and created only by first-run
setup" above. This is a capability loss: after this change the setup-created account is the only
account, its password cannot be changed, and an operator locked out of it has no way back in without
acting on the database directly. A follow-up change should add account routes to the panel.
