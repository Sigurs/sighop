## MODIFIED Requirements

### Requirement: Periodic status reports the operating limits
The system SHALL print a periodic status line, at a configurable interval defaulting to 60
seconds, carrying at least: transmit gate state, duty-cycle usage over the rolling hour against
the ceiling, queue depth by priority class, packets suppressed and dropped, dedup hit rate and
cache occupancy, count of learned paths, and — because durability is now a property an operator
relies on — the persistence state, the count of packet-log rows discarded because logging fell
behind, and the count of routes discarded unwritten.

#### Scenario: Status during a receive-only run
- **WHEN** the status interval elapses during a gated run
- **THEN** the line reports the gate as closed, the duty-cycle usage that would have been consumed, and the remaining fields

#### Scenario: Duty-cycle usage approaching the ceiling
- **WHEN** the rolling-hour usage passes the reserve threshold
- **THEN** the status line marks the budget state distinctly so an operator can see that classes 2 and 3 are stalled

#### Scenario: Status with no database configured
- **WHEN** the status interval elapses on a run with no database configured
- **THEN** the line reports persistence as off, and the discard counters read zero rather than being omitted

#### Scenario: Status while the database is failing
- **WHEN** the status interval elapses while database writes are failing
- **THEN** the line marks persistence as degraded distinctly from off, and reports what has been discarded since the run began

## REMOVED Requirements

### Requirement: Entity identities are supplied as keyfiles
**Reason**: Keyfiles are no longer the only source of a local identity. With a database
configured, entities come from the entity store, and a requirement whose name asserts otherwise
would mis-describe the command's behaviour on every persistent run.

**Migration**: Replaced by "Entity identities come from the entity store or from keyfiles" below,
which keeps every existing behaviour verbatim — keyfiles on the command line, startup reporting
of name, public key and node hash, ephemeral stubs when nothing is supplied, and failure on
colliding node hashes — and adds the entity store as a second source.

## ADDED Requirements

> Reference: DESIGN.md §6 (persistence), §10 (secrets from the environment, never baked in).
> Everything the run command already does is unchanged: the transmit gate stays closed without
> its flag, and nothing here relaxes a safety property.

### Requirement: Entity identities come from the entity store or from keyfiles
The system SHALL load local entities from the entity store when a database is configured, SHALL
accept one or more entity keyfiles on the `run` command in either case, SHALL report at startup
the name, public key, node hash and source of each, and SHALL apply the node-hash collision rule
across every entity loaded in the run whatever its source. Where no database is configured and no
keyfile is supplied, the existing ephemeral advert stubs remain available and SHALL continue to be
marked ephemeral.

#### Scenario: Run with an entity keyfile
- **WHEN** `sighop run` is given an entity keyfile
- **THEN** that identity is loaded, reported at startup with its public key, node hash and its keyfile as the source, and used for both advert origination and inbound message matching

#### Scenario: Run with persisted entities
- **WHEN** `sighop run` starts with a database holding enabled entities and no keyfile supplied
- **THEN** each enabled entity is loaded, reported at startup with its public key, node hash and the entity store as the source, and used for advert origination and inbound message matching

#### Scenario: Run with two colliding keyfiles
- **WHEN** two supplied keyfiles have public keys sharing their first byte
- **THEN** startup fails naming both files and their shared node hash, and nothing is transmitted

#### Scenario: A supplied keyfile collides with a persisted entity
- **WHEN** a supplied keyfile's public key shares its first byte with an enabled persisted entity
- **THEN** startup fails naming both the file and the stored entity and their shared node hash, and nothing is transmitted

### Requirement: The database is configured on the command line or from the environment
The system SHALL accept a database configuration from the environment and SHALL allow it to be
overridden on the command line, SHALL run with none supplied, and SHALL NOT print the
configuration's password in any output, log event or error message.

#### Scenario: Configured from the environment
- **WHEN** the runtime starts with a database configured in the environment and no command-line override
- **THEN** it connects to that database

#### Scenario: Overridden on the command line
- **WHEN** a database is given on the command line and another is present in the environment
- **THEN** the command-line value is used and the startup output names the host and database in force

#### Scenario: Configuration echoed in output
- **WHEN** the database configuration is named in startup output, a log event or a connection error
- **THEN** the password does not appear in it

### Requirement: Startup reports what persistence restored
The system SHALL report at startup whether it is running persistently or in memory, and when
persistent SHALL report the applied schema version and the number of entities, contacts and paths
restored — so the difference between "nothing was heard yet" and "nothing was restored" is
visible before any traffic arrives.

#### Scenario: Persistent start
- **WHEN** the runtime starts against a configured database
- **THEN** the startup output names the database in force, the applied schema version, and the counts of entities, contacts and paths restored

#### Scenario: In-memory start
- **WHEN** the runtime starts with no database configured
- **THEN** the startup output states that state will not survive the process, in the same place a persistent run reports its counts

### Requirement: A database command applies and reports migrations
The system SHALL provide a command surface that applies outstanding migrations and reports the
database's current and expected schema versions, separate from the command that runs the node.
Applying migrations SHALL NOT be a side effect of running the node.

#### Scenario: Applying migrations
- **WHEN** the migration command is run against a database behind the code
- **THEN** the outstanding migrations are applied in order and the resulting version is printed

#### Scenario: Reporting version
- **WHEN** the version command is run
- **THEN** the applied version and the version the code expects are printed, and whether they agree

#### Scenario: Running the node against an unmigrated database
- **WHEN** `sighop run` starts against a database that is not at the expected version
- **THEN** it fails naming both versions and the command that reconciles them, and applies nothing

### Requirement: A replay run does not write to the database by default
The system SHALL NOT persist contacts, paths or packet log rows learned from a replayed capture
unless persistence for replay is explicitly requested, and SHALL say at startup that a replay run
is not writing. Replayed receptions carry the timestamps of an earlier session, and writing them
as though they had just been heard would make a recorded contact indistinguishable from a live
one.

#### Scenario: Replay with a database configured
- **WHEN** `sighop run --replay` is invoked with a database configured and no explicit request to persist
- **THEN** the pipeline runs against the recorded events, nothing is written, and the startup output states that the replay is not persisting

#### Scenario: Replay with persistence explicitly requested
- **WHEN** a replay run is explicitly asked to persist
- **THEN** it writes as a live run would, and the startup output states that recorded state is being written

#### Scenario: Replay with no database configured
- **WHEN** `sighop run --replay` is invoked with no database configured
- **THEN** it behaves exactly as it does today

### Requirement: Identity management commands cover the entity store
The system SHALL extend its key management command surface with actions to list stored
identities, import a keyfile into the store, and export a stored identity to a keyfile, and SHALL
generate an encryption secret on request. None of these SHALL print a seed except the export,
which writes it to a file rather than to the terminal.

#### Scenario: Listing stored identities
- **WHEN** the list command is run against a configured database
- **THEN** each stored entity's name, type, public key, node hash and enabled state is printed, and no seed or ciphertext appears

#### Scenario: Importing a keyfile
- **WHEN** the import command is run with a keyfile
- **THEN** the identity is stored with its seed encrypted and the stored public key is printed

#### Scenario: Generating an encryption secret
- **WHEN** the secret generation command is run
- **THEN** a correctly-formed secret is printed once, with the statement that losing it makes every stored identity unrecoverable

#### Scenario: A store command run with no database configured
- **WHEN** a command that requires the entity store is run with no database configured
- **THEN** it fails saying a database is required for that action, and the keyfile commands remain usable
