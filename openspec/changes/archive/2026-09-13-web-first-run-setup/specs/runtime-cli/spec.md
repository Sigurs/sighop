## MODIFIED Requirements

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
