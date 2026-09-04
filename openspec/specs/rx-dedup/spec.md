# rx-dedup Specification

## Purpose
How repeated copies of the same packet are recognised — by payload content alone — reported
rather than silently dropped, and held in a cache bounded by both age and entry count.
## Requirements
### Requirement: Duplicates are identified by payload content alone
The system SHALL key its duplicate cache on a hash of the payload type together with the payload
bytes, and SHALL exclude the path, hop count, transport codes and route type from that key,
because those mutate as a packet is flooded.

#### Scenario: Same packet received again over a different path
- **WHEN** two receptions carry identical payload type and payload bytes but different path bytes and hop counts
- **THEN** the second reception is reported as a duplicate of the first

#### Scenario: Different payloads with the same path
- **WHEN** two receptions share a path but differ in payload bytes
- **THEN** neither is reported as a duplicate of the other

#### Scenario: Same payload bytes under a different payload type
- **WHEN** two receptions carry identical payload bytes but different payload types
- **THEN** neither is reported as a duplicate of the other

### Requirement: Duplicates are reported, never silently dropped
The system SHALL mark a duplicate reception as such and count it, and SHALL emit its packet RX
wide event with the duplicate flag set. A duplicate SHALL NOT be delivered to subscribers.

#### Scenario: Duplicate reception
- **WHEN** a reception matches an entry already in the cache
- **THEN** a wide event is emitted marking it a duplicate and naming the reception it duplicates, the duplicate counter increases, and the reception is not fanned out

#### Scenario: First reception of a packet
- **WHEN** a reception does not match any cache entry
- **THEN** it is recorded in the cache and fanned out to subscribers

### Requirement: Undecodable frames are never deduplicated
The system SHALL pass through every reception that has no parsed payload — structural decode
failures, payload parse failures and modem-unparsed frames — without consulting or populating
the duplicate cache.

#### Scenario: Two identical corrupt frames
- **WHEN** the same corrupt bytes are received twice
- **THEN** both are fanned out and neither is reported as a duplicate

### Requirement: The cache is bounded by both age and entry count
The system SHALL evict cache entries older than a configurable time-to-live and SHALL evict the
least recently used entry when a configurable maximum entry count is reached, so that the cache
is bounded in memory independent of traffic rate.

#### Scenario: Entry expires by age
- **WHEN** a reception matches an entry whose age exceeds the time-to-live
- **THEN** it is treated as a first reception, not a duplicate

#### Scenario: Entry count reached
- **WHEN** a reception must be recorded and the cache holds its maximum number of entries
- **THEN** the least recently used entry is evicted and the new entry is recorded

### Requirement: Cache behaviour is measurable at runtime
The system SHALL report the duplicate hit rate, the current entry count against the cap, and the
largest observed interval between a reception and the duplicate it matched, so that the
time-to-live and entry cap can be sized from observation rather than assumption.

#### Scenario: Operator inspects dedup behaviour during a run
- **WHEN** the periodic status output is produced
- **THEN** it states the duplicate hit rate, the entry count against the configured cap, and the largest observed inter-copy interval

