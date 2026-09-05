# contacts Specification

## Purpose
TBD - created by archiving change milestone-4-first-transmit. Update Purpose after archive.
## Requirements
### Requirement: Only verified adverts become contacts
The system SHALL create or update a contact from an advert only after its Ed25519 signature has
verified, and SHALL discard an unsigned or badly-signed advert without recording any of its
content, including the name.

#### Scenario: Verified advert
- **WHEN** an advert whose signature verifies is received
- **THEN** a contact is created or updated with its public key, node hash, name, appdata flags and heard timestamps

#### Scenario: Advert with a bad signature
- **WHEN** an advert whose signature does not verify is received
- **THEN** no contact is created or updated, and the discard is reported

### Requirement: Contacts are keyed by public key
The system SHALL key contacts by their full 32-byte public key, and SHALL treat two adverts
carrying the same public key as the same contact regardless of the path they arrived by.

#### Scenario: Same peer heard twice by different paths
- **WHEN** two verified adverts carrying one public key arrive with different paths
- **THEN** one contact exists, its last-heard timestamp advances, and its first-heard timestamp is unchanged

#### Scenario: Name changes between adverts
- **WHEN** a verified advert carries a name different from the one recorded for that public key
- **THEN** the contact's name is updated and the change is reported, because the name is advert content and only the key is the identity

### Requirement: Node-hash lookup returns every candidate
The system SHALL index contacts by node hash and SHALL return the complete set of contacts
sharing a node hash, never a single contact. No consumer may treat a node hash as identifying a
peer.

#### Scenario: Two contacts share a node hash
- **WHEN** two contacts whose public keys share their first byte are held and a lookup is made by that node hash
- **THEN** both contacts are returned

#### Scenario: Node hash matches nothing
- **WHEN** a lookup is made for a node hash held by no contact
- **THEN** an empty set is returned rather than an error

### Requirement: A contact may be added from a public key alone
The system SHALL allow a contact to be added from a hex public key supplied by the operator,
for the case where the peer's advert has not been heard, and SHALL mark such a contact as
having no verified advert behind it.

#### Scenario: Manual addition
- **WHEN** a 32-byte hex public key is supplied
- **THEN** a contact is created with that key and its derived node hash, carrying no name and marked as unverified-by-advert

#### Scenario: Manual addition of a key already known
- **WHEN** a hex public key matching an existing contact is supplied
- **THEN** the existing contact is returned unchanged, keeping the name and flags its verified advert supplied

### Requirement: Contacts are selectable by name or key prefix
The system SHALL resolve a peer reference to a contact by exact name or by a hex public-key
prefix, and SHALL fail with an error listing the candidates when a reference matches more than
one contact.

#### Scenario: Unambiguous reference
- **WHEN** a peer reference matches exactly one contact by name or key prefix
- **THEN** that contact is selected

#### Scenario: Ambiguous reference
- **WHEN** a peer reference matches more than one contact
- **THEN** selection fails with an error naming every match, and no message is composed

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

