## MODIFIED Requirements

### Requirement: A run serves the web interface when asked and says where
The system SHALL provide options on the run command to enable the web interface, to choose its
listening address and port, and to name additional host names it answers to, SHALL default the
address to loopback, and SHALL report at startup whether the interface is being served and, when
it is, the address and port it is listening on and how many enabled accounts can sign in. A run
not asked for the interface SHALL report nothing about it. A run asked for the interface SHALL
fail at startup, before any traffic is processed and before any port is listened on, when no
database is configured or when the database holds no enabled account, naming the command that
resolves it.

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

#### Scenario: The interface with no account to sign in with
- **WHEN** the run command is given the web option and the database holds no enabled account
- **THEN** startup fails naming the command that creates an account, and no port is listened on

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

## ADDED Requirements

### Requirement: A web account command surface manages the accounts that sign in to the interface
The system SHALL provide commands to add an account, list accounts, set an account's password,
disable an account, enable it again and remove it. Each SHALL require a configured database, SHALL
state its consequence for sessions already signed in, and SHALL refuse a change that would leave
the database with no enabled account only when the operator has not explicitly acknowledged it.

#### Scenario: Adding an account
- **WHEN** an account is added with a username
- **THEN** the password is read by prompt or standard input, the account is stored enabled, and the command states that it can sign in to any run using this database

#### Scenario: Setting a password
- **WHEN** an account's password is set
- **THEN** the command states that sessions signed in with the old password end within a minute on every run using this database

#### Scenario: Disabling the last enabled account
- **WHEN** the only enabled account is disabled or removed without the explicit acknowledgement option
- **THEN** the command refuses, stating that no run could then start its web interface

#### Scenario: Listing accounts
- **WHEN** accounts are listed
- **THEN** each account's username, enabled state, creation time and password-set time are shown, and no hash is shown

#### Scenario: Without a database
- **WHEN** any account command is run with no database configured
- **THEN** it fails stating that accounts are stored in the database
