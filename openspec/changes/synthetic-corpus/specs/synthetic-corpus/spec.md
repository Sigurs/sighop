# Spec Delta

## Purpose

The synthetic regression corpus: a deterministic, generated stand-in for recorded mesh traffic that
carries no real identity, name or message, spans every shape the protocol layer must handle, and
repeats itself only where a test needs it to.

## ADDED Requirements

### Requirement: The corpus is generated, not recorded
The system SHALL produce the committed corpus by running a generator built on sighop's own packet and
payload encoders, and SHALL write it in the same capture JSONL format the capture writer produces, so
that every consumer of a capture file reads the synthetic corpus without modification.

#### Scenario: The corpus is read by the ordinary replay path
- **WHEN** a synthetic corpus file is opened by the capture replay source
- **THEN** it yields modem events exactly as it would for a recorded file, its first line is consumed as provenance, and no line is reported unreadable

#### Scenario: The header states it is synthetic
- **WHEN** the `capture_meta` header of a corpus file is read
- **THEN** it identifies the file as synthetic and records the generator version and seed, and describes no real device, board, firmware or mesh

### Requirement: Generation is deterministic
The system SHALL generate byte-identical output for the same generator version and seed, drawing
every identity, timestamp, signal reading and choice from that seed and from nothing else, so that a
change to the corpus is always a change to the generator or its seed and is visible as a diff.

#### Scenario: Two runs agree
- **WHEN** the generator runs twice with the same seed, in different processes and at different times
- **THEN** the two outputs are byte-identical

#### Scenario: The committed corpus has not drifted
- **WHEN** the test suite regenerates the corpus in memory
- **THEN** it equals the committed corpus files byte for byte, and a difference fails the run naming the file and the first differing line

#### Scenario: A different seed differs
- **WHEN** the generator runs with a different seed
- **THEN** every synthetic identity differs, so no output depends on a fixed key that is not part of the declared cast

### Requirement: The corpus contains no real data
The system SHALL build every node identity, node name, channel, message and text in the corpus from a
declared synthetic cast, SHALL name every synthetic node so it is recognisable as fictional, and SHALL
carry no key, name, message or device string taken from a recorded capture or from a live mesh.

#### Scenario: Every advertised node is from the cast
- **WHEN** every ADVERT in the corpus is parsed
- **THEN** its public key and its name both belong to the declared cast

#### Scenario: Every decryptable message is from the cast
- **WHEN** every frame in the corpus that the suite can decrypt is decrypted
- **THEN** its sender name and its text both come from the declared cast's message set

#### Scenario: A real-looking value is refused
- **WHEN** a corpus record names a node, key or text that is not from the cast
- **THEN** the guard test fails naming the file, the record and the offending value

#### Scenario: No private key is committed
- **WHEN** the repository's tracked files are scanned for private-key material
- **THEN** none is found outside test code that generates its keys in memory

### Requirement: The cast is deliberately varied
The system SHALL give the synthetic mesh enough variety that the corpus exercises every shape the
recorded corpus did, so that replacing it narrows nothing: every route type the codec supports that
the recording held, path hash sizes 1 to 3, hop counts 0 to 5, every payload type the recording
held, every advert flag combination it held, and every discovery form.

#### Scenario: Node roles
- **WHEN** the corpus's adverts are grouped by node type and flags
- **THEN** repeaters, room servers and chat nodes are all present, with and without a location, and every advert verifies

#### Scenario: Multi-hop adverts depend on the encoded hash size
- **WHEN** a multi-hop advert with a multi-byte path hash is read with its encoded hash size
- **THEN** its flags and name are correct, and reading the same bytes as 1-byte hashes yields corrupt flags or a truncated name, so the corpus still discriminates the two readings

#### Scenario: Both acknowledgement forms
- **WHEN** the corpus's ACK frames are grouped by payload length
- **THEN** both the 4-byte and the 6-byte forms are present

#### Scenario: Discovery in all corpus forms
- **WHEN** the corpus's CONTROL frames are decoded
- **THEN** discovery requests of 6 and 10 bytes and discovery responses of 38 bytes are present, and every response comes from a repeater

### Requirement: Repetition is bounded and declared
The system SHALL keep the share of receptions that repeat an already-seen packet small and SHALL
declare, in the generator, every repeat it emits and why, so that the corpus is not padded and every
duplicate is there for a reason a test relies on.

#### Scenario: The repeat share is bounded
- **WHEN** the corpus's receptions are counted by distinct packet content
- **THEN** repeats are no more than the declared ceiling of the receptions, and no packet has more copies than the declared maximum

#### Scenario: Flood repeats over different paths
- **WHEN** a flood packet is repeated in the corpus
- **THEN** its copies share payload bytes but differ in path, hop count, signal readings and arrival time within a few seconds

#### Scenario: A late echo
- **WHEN** the corpus's repeats are ordered by spread between first and last copy
- **THEN** exactly one flood packet has a later copy arriving over a different path more than 60 seconds after the first

#### Scenario: A retransmitted direct message
- **WHEN** the corpus's direct messages are compared byte for byte
- **THEN** exactly one is repeated identically, more than 30 minutes after the first, standing for a sender retrying an unacknowledged message

#### Scenario: Nothing else repeats
- **WHEN** a corpus record is neither a declared repeat nor a designed case
- **THEN** its packet content appears nowhere else in the corpus

### Requirement: The corpus and its manifest are reproducible together
The system SHALL emit, with the corpus, a manifest of what it contains — record counts per file, per
payload type, route type, hop count, hash size and advert form — and SHALL take the tests' recorded
expectations from a reviewed copy of it, never by regenerating them from a failing run.

#### Scenario: The manifest matches the recorded expectations
- **WHEN** the corpus is decoded and counted
- **THEN** the counts equal the expectations recorded in the tests, and equal the generator's manifest

#### Scenario: An intentional change to the corpus
- **WHEN** the generator is changed to add or remove frames
- **THEN** the drift check, the manifest and the recorded expectations all fail until each is updated in the same reviewed change

### Requirement: History carries no real data
The system SHALL leave no recorded capture, private key, deployment secret or real node name or key
reachable from any ref, tag or reflog entry of the repository, SHALL perform the purge only after the
operator has explicitly confirmed it, and SHALL keep a backup outside the repository until the operator
discards it.

#### Scenario: No removed path is reachable
- **WHEN** every path ever committed on any branch or tag is listed
- **THEN** none is under `captures/`, none is the burned key fixture, and none is a `.env.prod`

#### Scenario: No real string is reachable
- **WHEN** every revision of every tracked file on every branch is searched for each name and key prefix on the operator's redaction list
- **THEN** none is found

#### Scenario: The reflog and unreachable objects are gone
- **WHEN** the purge has completed
- **THEN** the reflog is expired and unreachable objects are pruned, so the removed content cannot be recovered from the repository's own object store

#### Scenario: The purge waits for confirmation
- **WHEN** the rewritten repository has been produced and verified
- **THEN** the original is not replaced until the operator has confirmed, and until then it is untouched

#### Scenario: A backup exists outside the repository
- **WHEN** the purge begins
- **THEN** a full backup of the original repository exists outside the working tree and is never committed, and the operator is told it still holds the real data

#### Scenario: Deployment secrets are rotated
- **WHEN** history is purged of `.env.prod`
- **THEN** the secret key and database credentials it held are reported as compromised and rotated, because removing them from history does not un-disclose them

