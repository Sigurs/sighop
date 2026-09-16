# channel-store Specification

## Purpose
The station's set of group channels: which channels sighop can read and post in, how each one's key
is obtained and protected at rest, and how a running process learns that the set has changed.
## Requirements

### Requirement: A channel is stored configuration belonging to the station
The system SHALL store each channel as one record with a unique name, a kind, its channel hash and
its creation time, and SHALL NOT associate a channel with any identity: every loaded identity that
may chat can read and post in every stored channel. The kind SHALL be one of `public` (the stock
MeshCore Public channel), `hashtag` (a key derived from a hashtag name) or `psk` (a supplied 16- or
32-byte pre-shared key).

#### Scenario: Two identities and one channel
- **WHEN** two identities are loaded and one channel is stored
- **THEN** both identities can post in that channel and its history is one history, not one per identity

#### Scenario: A hashtag channel
- **WHEN** a channel is added from the hashtag `#dev-sighop`
- **THEN** it is stored with kind `hashtag`, the name `#dev-sighop`, and the channel hash of the key derived from that hashtag

#### Scenario: A pre-shared key of the wrong length
- **WHEN** a channel is added with a pre-shared key that decodes to neither 16 nor 32 bytes
- **THEN** the addition is refused naming the decoded length, and nothing is stored

#### Scenario: A pre-shared key that is not valid base64
- **WHEN** a channel is added with a pre-shared key that does not decode as base64
- **THEN** the addition is refused saying so, and nothing is stored

### Requirement: A pre-shared key is sealed at rest and a public derivation is not stored as a secret
The system SHALL store a `psk` channel's key only sealed under the platform's sealing secret, and
SHALL store a `hashtag` channel as its hashtag and a `public` channel as its kind alone, deriving
their keys when loaded. A stored pre-shared key SHALL NOT appear in any listing, log event, error or
rendered page.

#### Scenario: A database dump
- **WHEN** the channel table is read without the sealing secret
- **THEN** no pre-shared key can be recovered from it, and hashtag and Public channels reveal only what their names already reveal

#### Scenario: A key that does not open
- **WHEN** a stored pre-shared key cannot be opened under the configured sealing secret
- **THEN** that channel is reported by name as unusable and skipped, and every other channel is loaded

#### Scenario: Listing channels
- **WHEN** channels are listed
- **THEN** each shows its name, kind and channel hash, and no key material

### Requirement: Hashtag channels are marked as having a guessable key
The system SHALL mark a `hashtag` channel, and the `public` channel, as readable by anyone who knows
or guesses its name, wherever a channel is added or listed, so that no operator mistakes either for
a private channel.

#### Scenario: Adding a hashtag channel
- **WHEN** a hashtag channel is added from any surface
- **THEN** the result states that anyone who guesses the hashtag can read and post in it

#### Scenario: Listing
- **WHEN** channels are listed
- **THEN** hashtag and Public channels carry a marking that distinguishes them from pre-shared-key channels

### Requirement: The same key cannot be stored twice
The system SHALL refuse to add a channel whose key equals the key of a channel already stored,
naming the existing channel, and SHALL refuse a name already in use.

#### Scenario: Adding a hashtag that already exists under another name
- **WHEN** a pre-shared-key channel is added whose key equals an existing hashtag channel's derived key
- **THEN** the addition is refused naming the existing channel

#### Scenario: Two distinct keys with the same channel hash
- **WHEN** a channel is added whose key differs from every stored key but whose channel hash equals a stored channel's
- **THEN** the addition is accepted, because a one-byte hash collides routinely

### Requirement: The Public channel is present after the schema upgrade
The schema upgrade that introduces channels SHALL add the stock Public channel, whose pre-shared key
is `izOH6cXN6mrJ5e26oRXNcg==` and whose channel hash is `0x11`. Adding it SHALL transmit nothing. An
operator SHALL be able to remove it, and it SHALL NOT be re-added by a later start.

#### Scenario: Upgrading a database
- **WHEN** a database is upgraded to the channel schema
- **THEN** it holds exactly one channel, of kind `public`, and no transmission has occurred

#### Scenario: Removing Public
- **WHEN** the Public channel is removed and the run is restarted
- **THEN** no Public channel exists

### Requirement: Removing a channel removes its history and says so
The system SHALL delete a channel's recorded messages together with the channel, and SHALL state
the number of messages that will be deleted before a removal is carried out.

#### Scenario: Removing a channel with history
- **WHEN** a channel with 40 recorded messages is removed
- **THEN** the confirmation states that 40 messages will be deleted, and after removal neither the channel nor its messages exist

### Requirement: A running process uses changed channel configuration without restart
The system SHALL decrypt and post using channels held in memory, loaded at startup, SHALL apply a
change made through the web interface of the same run immediately, and SHALL apply a change made by
another process within 60 seconds. A reload that adopts a different set SHALL be reported, naming the
channels added and removed; a reload that changes nothing SHALL be silent. When the channel
configuration cannot be read, the system SHALL keep the last set it loaded and report the failure.

#### Scenario: A channel added from the command line
- **WHEN** a channel is added with the command line while a run is active
- **THEN** within 60 seconds that run decrypts receptions on the new channel and reports that the channel was added

#### Scenario: A refresh that finds no change
- **WHEN** the channel set is re-read and matches the one in force
- **THEN** nothing is reported, because the station's state did not change

#### Scenario: The database is unreachable at refresh
- **WHEN** a refresh of the channel set fails because the database is degraded
- **THEN** receptions continue to be decrypted with the channels already loaded, and the failure is reported

### Requirement: Channels require durable storage
The system SHALL store channels only in the database. A run with no database configured SHALL load
no channels and SHALL say so at startup; channel commands run with no database SHALL refuse and
state that channels require durable storage.

#### Scenario: A run without a database
- **WHEN** a run starts with no database configured
- **THEN** the startup output states that no channels are loaded because channels require durable storage, and group text is left undecrypted
