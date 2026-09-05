## MODIFIED Requirements

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

## ADDED Requirements

> Reference: DESIGN.md §12 milestone 4. The behaviours themselves belong to `local-identity`,
> `contacts` and `direct-messaging`; this capability covers only how `sighop run` and the
> key-management command expose them.

### Requirement: Entity identities are supplied as keyfiles
The system SHALL accept one or more entity keyfiles on the `run` command, SHALL load each as a
local entity that both adverts and receives, and SHALL report at startup the name, public key
and node hash of each. Where no keyfile is supplied, the existing ephemeral advert stubs remain
available and SHALL continue to be marked ephemeral.

#### Scenario: Run with an entity keyfile
- **WHEN** `sighop run` is given an entity keyfile
- **THEN** that identity is loaded, reported at startup with its public key and node hash, and used for both advert origination and inbound message matching

#### Scenario: Run with two colliding keyfiles
- **WHEN** two supplied keyfiles have public keys sharing their first byte
- **THEN** startup fails naming both files and their shared node hash, and nothing is transmitted

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
