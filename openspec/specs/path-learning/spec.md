# path-learning Specification

## Purpose
How reverse paths back to a sender are learned from what we receive — flood receptions and
zero-hop direct ones — how they are keyed, and which one wins when several are known.
## Requirements
### Requirement: Reverse paths are learned from flood and direct-neighbour receptions
The system SHALL record, for each flood reception whose sender can be identified, the reverse of
the received path as a candidate route back to that sender, together with the reception time,
hop count and the SNR of the reception. The system SHALL additionally record a zero-hop route
for a DIRECT reception that arrives with an empty path, because such a packet reached the
receiver with no repeater in between. The system SHALL NOT learn a route from a DIRECT reception
that carries a path, whose route was chosen by someone else.

#### Scenario: Flood reception with a non-empty path
- **WHEN** a flood packet is received carrying a path of one or more hops and an identifiable sender
- **THEN** a path entry is recorded for that sender holding the reversed path, hop count, SNR and reception time

#### Scenario: Zero-hop flood reception
- **WHEN** a flood packet is received directly with an empty path
- **THEN** a zero-hop entry is recorded for that sender, which is a valid route and not an absence of one

#### Scenario: Zero-hop direct reception
- **WHEN** a DIRECT packet is received with an empty path and an identifiable sender
- **THEN** a zero-hop entry is recorded for that sender, because the packet reached the receiver over the air with no repeater between them

#### Scenario: Direct reception carrying a path
- **WHEN** a DIRECT packet is received whose path has one or more hops
- **THEN** no path entry is recorded, because that path is a route someone else chose and not a route back

#### Scenario: Reception whose sender cannot be identified
- **WHEN** a reception carries no field identifying its sender
- **THEN** no path entry is recorded and no error is raised

### Requirement: Paths are keyed by public key when one is known
The system SHALL key path entries on the sender's public key when the reception carries one, and
otherwise on the sender's node hash, marking hash-keyed entries as ambiguous because a 1-byte
hash may denote more than one node.

#### Scenario: Advert reception
- **WHEN** a verified advert is received
- **THEN** the path entry is keyed on the advert's public key and is not marked ambiguous

#### Scenario: Reception carrying only a source hash
- **WHEN** a reception identifies its sender only by node hash
- **THEN** the path entry is keyed on that hash and marked ambiguous

#### Scenario: Public key later observed for a hash-keyed entry
- **WHEN** a verified advert is received whose node hash matches an existing ambiguous entry
- **THEN** the system records the key-keyed entry and does not silently merge the ambiguous entry into it

### Requirement: The most recently confirmed path wins
The system SHALL resolve a lookup for a destination to its most recently confirmed path, and
SHALL retain the SNR and hop count of each candidate without applying any further scoring.

#### Scenario: Two routes observed to one peer
- **WHEN** two different paths have been learned for the same sender
- **THEN** a lookup returns the one most recently confirmed, and both remain recorded

#### Scenario: Lookup for an unknown destination
- **WHEN** a lookup is made for a destination with no learned path
- **THEN** the store reports no path rather than returning an empty path, so a caller cannot mistake "unknown" for "zero hops"

### Requirement: The path store is shared, authoritative in memory, and durable when a database is configured
The system SHALL maintain one path store for the whole platform rather than one per entity, and
SHALL answer every lookup from memory so that route selection never waits on a database. The
store SHALL be bounded by a configurable maximum number of destinations with least-recently-updated
eviction. When a database is configured the system SHALL additionally persist learned routes and
restore them at startup; when none is configured the store SHALL be memory-only as before.

#### Scenario: Two subscribers look up the same destination
- **WHEN** two subscribers query for the same destination
- **THEN** both receive the same learned path from the one shared store

#### Scenario: Restart with a database configured
- **WHEN** the process restarts with a database holding learned routes
- **THEN** those routes are restored before traffic arrives, a lookup for a restored destination returns its most recently confirmed path, and the count restored is reported at startup

#### Scenario: Restart with no database configured
- **WHEN** the process restarts with no database configured
- **THEN** the path store is empty and is relearned from received traffic

#### Scenario: Lookup while the database is unreachable
- **WHEN** a route is looked up while the database is unreachable
- **THEN** the lookup is answered from memory at full speed and reports no error, because the memory store is the authority

### Requirement: Route persistence happens off the reception path
The system SHALL NOT make decoding, deduplicating or dispatching a reception wait on a route
being written, and SHALL bound the work outstanding behind that path. A route that could not be
written SHALL be discarded rather than queued without bound, and the discard SHALL be counted,
because a route is cheap to relearn from the next reception.

#### Scenario: Routes learned faster than they are written
- **WHEN** routes are learned faster than the database accepts them
- **THEN** reception and dispatch proceed at full rate, the outstanding work stays bounded, and the number of unwritten routes is counted and reported

#### Scenario: Write fails
- **WHEN** a route write fails
- **THEN** the in-memory route is unaffected and usable, and the failure is reported as an error

### Requirement: A restored route is a candidate, not a confirmation
The system SHALL restore a route with the confirmation time it had when it was learned, and
SHALL NOT treat restoration as fresh confirmation. A route confirmed by a live reception SHALL
therefore win over an older restored one under the existing most-recently-confirmed rule.

#### Scenario: A restored route and a freshly learned one
- **WHEN** a route is restored from the database and a different route to the same destination is then learned from a reception
- **THEN** the freshly learned route wins the lookup, and both remain recorded

#### Scenario: Ambiguity survives the round trip
- **WHEN** a route keyed only on a node hash, and therefore marked ambiguous, is persisted and restored
- **THEN** it is still marked ambiguous, because restoring it resolves nothing about which node the hash denoted

