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
cache occupancy, and count of learned paths.

#### Scenario: Status during a receive-only run
- **WHEN** the status interval elapses during a gated run
- **THEN** the line reports the gate as closed, the duty-cycle usage that would have been consumed, and the remaining fields

#### Scenario: Duty-cycle usage approaching the ceiling
- **WHEN** the rolling-hour usage passes the reserve threshold
- **THEN** the status line marks the budget state distinctly so an operator can see that classes 2 and 3 are stalled

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

