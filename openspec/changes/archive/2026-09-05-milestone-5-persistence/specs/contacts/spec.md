## REMOVED Requirements

### Requirement: The contact store is in-memory for this milestone
**Reason**: The milestone it was scoped to is over. Milestone 4 recorded the cost of it plainly —
"the run that sends must hear the peer's advert in that same run; a restart forgets every
contact" — and named this milestone as the one that removes it.

**Migration**: Replaced by "The contact store is durable when a database is configured" below,
which keeps the in-memory behaviour, and the startup statement about it, for the case where no
database is configured. Nothing that depended on the in-memory store changes shape: the store's
interface is unchanged and a run without a database behaves exactly as before.

## ADDED Requirements

> Reference: DESIGN.md §6 (the `contact` table). Contact semantics — verification, keying by
> public key, node-hash candidate sets, manual addition, reference resolution — are unchanged by
> this delta and remain as specified.

### Requirement: The contact store is durable when a database is configured
The system SHALL persist contacts when a database is configured, restoring them at startup so a
peer heard on an earlier run is addressable without hearing its advert again, and SHALL hold
contacts in memory only when no database is configured. The system SHALL state at startup which
of the two is in force and how many contacts were restored, so an operator is never left to
assume durability.

#### Scenario: Restart with a database configured
- **WHEN** the runtime is restarted with a database holding contacts
- **THEN** those contacts are available before any traffic arrives, the startup output reports how many were restored, and a peer among them resolves by name or key prefix immediately

#### Scenario: Restart with no database configured
- **WHEN** the runtime is restarted with no database configured
- **THEN** the contact store is empty and the startup output says that contacts do not survive the process

### Requirement: A verified advert is persisted as it is observed
The system SHALL write a created or updated contact to the database at the point the advert is
recorded, not at shutdown, so that state survives a process that is killed rather than stopped
gracefully. The write SHALL NOT happen on the path that records the advert, and SHALL NOT be able
to delay it. Persistence SHALL NOT change what is recorded: an unverified advert stays a discard,
the public key stays the identity, and a name change stays a reported update.

#### Scenario: Contact created by an advert
- **WHEN** a verified advert creates a contact and the process is then killed without a graceful stop
- **THEN** the contact is present on the next start with its public key, node hash, name, flags and heard timestamps

#### Scenario: The database is slow while adverts arrive
- **WHEN** verified adverts are observed while the database is slow to accept writes
- **THEN** each contact is recorded and usable immediately, the path that records it is not delayed, and the writes complete in their own time

#### Scenario: Advert with a bad signature
- **WHEN** an advert whose signature does not verify is received
- **THEN** nothing is written to the database, and the discard is reported exactly as before

#### Scenario: Repeated adverts from a known peer
- **WHEN** the same peer's verified advert is heard many times
- **THEN** the stored contact's last-heard timestamp advances, its first-heard timestamp is unchanged, and the write rate stays proportional to observed changes rather than to receptions

### Requirement: A restored contact carries whether an advert verified behind it
The system SHALL record, for each stored contact, whether it was created from a verified advert
or added manually from a public key, and SHALL restore that distinction. A manually added contact
SHALL NOT become advert-verified by being persisted and reloaded.

#### Scenario: Restoring a manually added contact
- **WHEN** a contact added from a hex public key is restored on a later run
- **THEN** it is still marked as having no verified advert behind it, and carries no name it never had

#### Scenario: A manually added contact is later heard
- **WHEN** a verified advert arrives for a stored contact that was added manually
- **THEN** the stored contact gains the advert's name and flags and becomes advert-verified

### Requirement: A contact write failure does not lose the in-memory contact
The system SHALL keep a contact usable in memory when its write to the database fails, SHALL
report the failure as an error naming the contact, and SHALL NOT drop the contact or refuse the
advert because persistence was unavailable.

#### Scenario: Write fails while the runtime is receiving
- **WHEN** a verified advert is observed and its write to the database fails
- **THEN** the contact is present in memory for the rest of the run, the failure is reported, and messages to that peer can still be addressed

### Requirement: Contacts unwritten during a database outage are backfilled on recovery
The system SHALL track which contacts it has not succeeded in persisting, SHALL write them when
the database becomes available again without requiring a restart and without waiting for the peer
to advert again, and SHALL write each such contact's latest state once rather than replaying every
observation made during the outage. The system SHALL detect the database's return by its own means
rather than by the arrival of new work, because a contact may be the only thing that would have
prompted a write and adverts are hours apart.

#### Scenario: Adverts observed while the database is unreachable
- **WHEN** verified adverts are observed while the database is unreachable, and the database later becomes reachable
- **THEN** those contacts are written without a restart and without a further advert from the peer, and are present on the next start

#### Scenario: A contact changes several times during the outage
- **WHEN** one peer's verified advert is observed several times during an outage, changing what is recorded
- **THEN** the recovery writes that contact once, carrying its latest state, rather than one write per observation

#### Scenario: Recovery while nothing is being written
- **WHEN** the database becomes reachable again during a period in which no contact is observed and no write is attempted
- **THEN** the return is detected within a bounded interval, the backfill runs, and persistence stops being reported as degraded

#### Scenario: Process restarted during the outage
- **WHEN** the process is restarted while the database is still unreachable
- **THEN** the contacts observed during the outage are absent from the store, and the startup output's restored count reflects what was actually persisted rather than what was observed
