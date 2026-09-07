## ADDED Requirements

### Requirement: A run serves the web interface when asked and says where
The system SHALL provide options on the run command to enable the web interface and to choose its
listening address and port, SHALL default the address to loopback, and SHALL report at startup
whether the interface is being served and, when it is, the address and port it is listening on. A
run not asked for the interface SHALL report nothing about it.

#### Scenario: A run with the interface enabled
- **WHEN** the run command is given the web option
- **THEN** startup reports the address and port the interface is listening on

#### Scenario: A run without the interface
- **WHEN** the run command is not given the web option
- **THEN** startup says nothing about a web interface and no port is listened on

#### Scenario: A non-loopback address
- **WHEN** the interface's address is set to a non-loopback address
- **THEN** startup states that the interface is unauthenticated and reachable from the network, alongside the address and port

#### Scenario: The port cannot be bound
- **WHEN** the configured port cannot be bound
- **THEN** startup fails naming the address, the port and the reason, in the same way a configured database that cannot be reached fails

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
- **THEN** the run's output states that the change was made and what it now is
