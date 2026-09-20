# Spec Delta

## MODIFIED Requirements

### Requirement: Operator accounts are durable, hashed, and managed from the terminal
The system SHALL store operator accounts in the database, each with a unique username, a
password stored only as a salted memory-hard hash whose parameters travel with it, an enabled
state, the time its password was last set, and the account's default chat identity where one is
set. Usernames SHALL be compared case-insensitively and stored in one normalised form. The system
SHALL NOT store, log or render a password or its hash anywhere else. Accounts SHALL be created,
listed, re-passworded, disabled, re-enabled and removed through the command line, with one
exception: when the database holds no account at all, the first account MAY be created once through
first-run setup in the browser. No other account operation SHALL be offered in the browser. An
account's default chat identity is an interface preference rather than an account operation: it
carries no privilege, it SHALL be settable and clearable by the signed-in operator it belongs to and
by no one else, and an operator SHALL NOT be able to read or change another account's preference.

#### Scenario: An account is created
- **WHEN** an operator creates an account with a username and a password
- **THEN** the account is stored with a hash of the password and never the password itself, and it is enabled, with no default chat identity

#### Scenario: A username that differs only in case
- **WHEN** an account is created whose username differs from an existing one only in letter case
- **THEN** creation is refused, naming the existing account

#### Scenario: Listing accounts
- **WHEN** accounts are listed
- **THEN** each shows its username, whether it is enabled, when it was created and when its password was last set, and no hash is shown

#### Scenario: A setup-created account is an ordinary account
- **WHEN** the first account has been created through first-run setup
- **THEN** it is stored, listed, re-passworded, disabled and removed from the command line exactly as an account created there

#### Scenario: A preference belongs to one account
- **WHEN** an operator sets their default chat identity
- **THEN** it is stored against their own account only, and no other account's preference is read or changed
