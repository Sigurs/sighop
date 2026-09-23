# node-boot Specification

## Purpose
The entry point that boots the node: what it reads from the environment, what it refuses to start
without, and what it reports about the radio, the database, the web interface and the rooms, bots,
channels and webhooks it is serving before the first frame is handled.
## Requirements
### Requirement: The node is started by an entry point that takes no arguments
The system SHALL provide one entry point that boots the node and does nothing else. It SHALL take
no subcommands, options or positional arguments, and SHALL take its entire configuration from the
environment. Starting it SHALL open the modem source, decode receptions, deduplicate them, learn
paths, fan out on the bus, run the transmit scheduler with its advert stubs and serve the web
interface, in one process.

#### Scenario: Started with no arguments
- **WHEN** the entry point is started with a complete environment
- **THEN** it performs the startup handshake and probe, then processes receptions, runs the scheduler and serves the web interface until stopped

#### Scenario: Started with an argument
- **WHEN** the entry point is given any argument
- **THEN** it exits non-zero stating that configuration comes from the environment, and nothing is started

#### Scenario: Graceful stop
- **WHEN** the process is interrupted or asked to stop
- **THEN** it stops the scheduler, drains and logs its queued packets as dropped with a shutdown reason, and emits a final summary

### Requirement: Every setting is read from a named environment variable
The system SHALL read each runtime setting from its own environment variable, SHALL name the
variable and the offending value in any configuration error, and SHALL NOT clamp, round or
otherwise reinterpret a value it does not accept. A setting that is unset SHALL take its documented
default, except where a requirement here states that it is required.

#### Scenario: A malformed value
- **WHEN** a setting's variable holds a value outside what it accepts
- **THEN** startup fails naming the variable, the value given and the accepted values, and nothing is transmitted

#### Scenario: Defaults applied
- **WHEN** an optional setting's variable is unset
- **THEN** its documented default is in force and the startup output states the value in force rather than that it was defaulted

### Requirement: Starting without a database is a startup failure
The system SHALL require a database configuration. When none is present the system SHALL exit
non-zero at startup naming the variable that supplies it, SHALL NOT open the modem, and SHALL NOT
serve the web interface. The system SHALL NOT print the configuration's password in any output, log
event or error message.

#### Scenario: No database configured
- **WHEN** the entry point starts with no database configured
- **THEN** it exits non-zero naming the variable that supplies the database, having opened no modem and served no interface

#### Scenario: Configuration echoed in output
- **WHEN** the database configuration is named in startup output, a log event or a connection error
- **THEN** the password does not appear in it

### Requirement: Starting without a sealing secret prints the command that generates one
The system SHALL require the sealing secret. When its variable is unset or empty the system SHALL
exit non-zero at startup, SHALL name the variable, and SHALL print a command an operator can run to
generate an acceptable value. The system SHALL NOT generate one itself, because a secret the
operator has not recorded cannot unseal anything after the next restart.

#### Scenario: No secret configured
- **WHEN** the entry point starts with the sealing secret unset
- **THEN** it exits non-zero naming the variable and printing a command that generates a valid value, and nothing is started

#### Scenario: A secret of the wrong shape
- **WHEN** the sealing secret is set to a value that is not the required length when decoded
- **THEN** startup fails naming the variable and what the value must be, rather than padding, truncating or hashing it into shape

### Requirement: Outstanding migrations are applied before the node serves
The system SHALL apply outstanding migrations as part of starting, before the schema-version check
and before any traffic is handled, and SHALL emit an event naming the revision before and after. A
database ahead of the code's migration chain SHALL still be refused, naming both revisions.

#### Scenario: Starting against a database behind the code
- **WHEN** the node starts against a database behind the code's migration chain
- **THEN** the outstanding migrations are applied in order, an event names the revision before and after, and the node continues starting

#### Scenario: Starting against a database at the expected revision
- **WHEN** the node starts against a database already at the expected revision
- **THEN** no migration is applied and the node starts

#### Scenario: Starting against a database ahead of the code
- **WHEN** the node starts against a database migrated by newer code
- **THEN** it applies nothing, refuses to run, and names both revisions

### Requirement: Transmission stays off unless the environment enables it
The system SHALL keep the transmit gate closed unless its environment variable enables it
explicitly, and SHALL state the gate's state at startup and in every periodic status line. When the
gate is open the startup output SHALL state that packets will be transmitted on air, SHALL name
each identity that will originate them with its public key and node hash, and SHALL state the
duty-cycle ceiling in force.

#### Scenario: Started without transmit enabled
- **WHEN** the node starts with the transmit variable unset
- **THEN** the startup output states that transmit is disabled and no `Data` frame is written for the run's lifetime

#### Scenario: Started with transmit enabled
- **WHEN** the node starts with the transmit variable set
- **THEN** the startup output states prominently that packets will be transmitted on air, names each loaded entity identity with its public key and node hash, and states the configured duty-cycle ceiling

### Requirement: Periodic status reports the operating limits
The system SHALL print a periodic status line, at an interval configured from the environment and
defaulting to 60 seconds, carrying at least: transmit gate state, duty-cycle usage over the rolling
hour against the ceiling, queue depth by priority class, packets suppressed and dropped, dedup hit
rate and cache occupancy, count of learned paths, the persistence state, the count of packet-log
rows discarded because logging fell behind, and the count of routes discarded unwritten.

#### Scenario: Status during a receive-only run
- **WHEN** the status interval elapses during a gated run
- **THEN** the line reports the gate as closed, the duty-cycle usage that would have been consumed, and the remaining fields

#### Scenario: Duty-cycle usage approaching the ceiling
- **WHEN** the rolling-hour usage passes the reserve threshold
- **THEN** the status line marks the budget state distinctly so an operator can see that classes 2 and 3 are stalled

#### Scenario: Status while the database is failing
- **WHEN** the status interval elapses while database writes are failing
- **THEN** the line marks persistence as degraded and reports what has been discarded since the run began

### Requirement: Startup reports what persistence restored
The system SHALL report at startup the database in force, the applied schema version, the number of
entities, contacts and paths restored, and the number of stored messages held — direct and channel
alike — so the difference between "nothing was heard yet" and "nothing was restored" is visible
before any traffic arrives. Posts whose outcome the last stop left unresolved SHALL be reported when
there are any.

#### Scenario: Start against a populated database
- **WHEN** the node starts against a database holding entities, contacts and paths
- **THEN** the startup output names the database in force, the applied schema version, and the counts restored

#### Scenario: Channel history held
- **WHEN** the node starts against a database holding channel messages
- **THEN** the same startup line states how many channel messages are held, beside the conversations and direct messages

### Requirement: Local identities come from the entity store
The system SHALL load its local entities from the entity store at startup, SHALL report each one's
name, public key and node hash before any traffic is handled, and SHALL apply the node-hash
collision rule across every entity loaded. Startup SHALL fail when two enabled entities share a node
hash, naming both.

#### Scenario: Start with stored entities
- **WHEN** the node starts with enabled entities in the store
- **THEN** each is loaded, reported at startup with its public key and node hash, and used for advert origination and inbound message matching

#### Scenario: Two colliding stored entities
- **WHEN** two enabled entities' public keys share their first byte
- **THEN** startup fails naming both and their shared node hash, and nothing is transmitted

### Requirement: The node serves the rooms, bots, channels and webhooks that are configured and says so
The system SHALL serve every room bound to an enabled entity, run every enabled bot bound to an
enabled entity, load every stored channel, and deliver to every enabled webhook, and SHALL report
each of them before any traffic is handled. Anything it did not serve, run, load or deliver to SHALL
be reported together with the reason.

#### Scenario: Start with rooms, bots, channels and webhooks configured
- **WHEN** the node starts with each of them in the database
- **THEN** every room with its identity, membership, message count and retention, every bot with its driver, mode and limits, every channel, and every webhook it will deliver to, are reported before the first frame is handled

#### Scenario: Something is not served
- **WHEN** a room's entity is disabled, a bot is disabled, or a stored channel could not be loaded
- **THEN** it is reported as not served, with the reason, rather than omitted

### Requirement: Rendered output never presents unverified content as verified
The system SHALL render advert content, channel sender names and any other unauthenticated field
visually distinctly from cryptographically verified identities, in the node's own output as in every
other surface.

#### Scenario: Advert with an unverified signature
- **WHEN** a reception carries an advert whose signature does not verify
- **THEN** the rendered line marks it unverified and does not present its name in the form used for verified identities

#### Scenario: Stub entity is rendered
- **WHEN** the node's in-memory advert stubs are listed in the output
- **THEN** they are marked as ephemeral stubs, so they are not mistaken for persisted identities

### Requirement: Direct message, room, bot and channel activity is rendered as output
The system SHALL render each direct message sent and received, each room post handled, each bot
decision — an action taken, an action that would have been taken in observe mode, or an action
suppressed with its reason — and each channel message, naming the entity and the peer, and SHALL
include the per-bot counters in the periodic status report.

#### Scenario: A driver acts
- **WHEN** a bot transmits
- **THEN** a line names the bot, the peer, and the outcome including whether it was acknowledged

#### Scenario: A driver would have acted
- **WHEN** a bot in observe mode decides to act
- **THEN** a line marks it plainly as an observation that transmitted nothing, and is not presented as a transmission

#### Scenario: Periodic status
- **WHEN** the periodic status report is emitted
- **THEN** each running bot's actions, observations, suppressions by reason, dropped dispatches and driver failures are included
