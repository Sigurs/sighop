## ADDED Requirements

### Requirement: An event carries the node hash at the size it was heard
The system SHALL record on each event the path hash size of the advert packet that raised it (1, 2
or 3 bytes, from the packet's path length byte) and the node hash as that many leading bytes of the
node's public key, in lower-case hex. A flood advert heard directly from its sender, with no hops
in its path, SHALL still be recorded at the hash size its path length byte declares. A zero-hop
advert, which MeshCore sends with a path length byte of zero and so declares a hash size of 1,
SHALL be recorded at 1 byte; the system SHALL NOT infer a larger size from any other source.

#### Scenario: A flood advert with 2-byte path hashes
- **WHEN** a new repeater whose key begins `a1b2c3` is first heard in a flood advert whose path hash size is 2
- **THEN** the event's hash size is 2 and its node hash is `a1b2`

#### Scenario: A flood advert with 3-byte path hashes
- **WHEN** a new companion whose key begins `a1b2c3` is first heard in a flood advert whose path hash size is 3
- **THEN** the event's node hash is `a1b2c3`

#### Scenario: A flood advert heard directly
- **WHEN** a new repeater whose key begins `a1b2c3` is first heard in a flood advert with no hops and a path hash size of 2
- **THEN** the event's node hash is `a1b2`

#### Scenario: A zero-hop advert
- **WHEN** a new repeater is first heard in a zero-hop advert
- **THEN** the event's hash size is 1 and its node hash is the key's first byte

### Requirement: An event carries the path it was heard by, resolved against known contacts
The system SHALL record on each event the reception's path as its ordered hops, from the hop
nearest the advertising node to the hop nearest this node, each hop being a hash of the packet's
hash size. For each hop the system SHALL look up, at the moment the event is raised, the contacts
other than the advertising node whose public key begins with that hop's bytes, and SHALL record the contact's name when exactly one
contact matches and that contact has a name, and the number of matching contacts. A hop matching no
contact SHALL be shown as `<unknown>`; a hop matching more than one contact SHALL be shown as
`<ambiguous>`; a hop matching one contact that has no name SHALL be shown by that contact's key
prefix. A zero-hop reception SHALL have an empty path.

#### Scenario: A two-hop path with one known repeater
- **WHEN** a new companion is heard through hops `c3d4` then `e5f6`, and the node holds exactly one contact whose key begins `c3d4`, named `Hilltop`, and none beginning `e5f6`
- **THEN** the event's path is `c3d4` named `Hilltop` with 1 match, then `e5f6` with no name and 0 matches, shown as `Hilltop`, then `<unknown>`

#### Scenario: A 1-byte hop that collides
- **WHEN** a hop is `7a` and the node holds two contacts whose keys begin `7a`
- **THEN** that hop has no name, 2 matches, and is shown as `<ambiguous>`

#### Scenario: A zero-hop reception
- **WHEN** a new repeater is first heard in a zero-hop advert
- **THEN** the event's path is empty

## MODIFIED Requirements

### Requirement: The JSON format is a versioned, documented event
The system SHALL send a `json` webhook an HTTP `POST` with content type `application/json` whose
body carries: a schema version of `1`; the trigger name; an event identifier unique to the event and
identical across retries of it; the time the event occurred in UTC; the node's full public key in
hex, its node hash as the key's first byte, its node hash at the size it was heard together with
that hash size, its advertised name or null, its node type, and its advertised position or null;
and the reception's hop count, SNR, RSSI and receive time, each null
when unknown, and its path as a list of hops each carrying the hop hash in hex, the resolved contact
name or null, and the number of matching contacts. Fields SHALL NOT be removed or change meaning
within schema version `1`; fields MAY be added within it.

#### Scenario: A new-repeater JSON event
- **WHEN** a `new_repeater` event is delivered to a `json` webhook
- **THEN** the body has `schema` 1, `event` `new_repeater`, an event identifier, and the node and reception fields above

#### Scenario: A retried delivery
- **WHEN** a delivery is retried
- **THEN** the body carries the same event identifier as the first attempt

#### Scenario: The 1-byte node hash keeps its meaning
- **WHEN** a `new_repeater` event heard with a path hash size of 2 is delivered to a `json` webhook
- **THEN** `node_hash` is still the key's first byte as two hex digits, and the 2-byte hash appears in its own field

#### Scenario: A zero-hop reception in JSON
- **WHEN** an event from a zero-hop advert is delivered to a `json` webhook
- **THEN** the reception's path is an empty list

### Requirement: The Discord format is safe to render advert content
The system SHALL send a `discord` webhook a message with one embed that names the trigger in words,
the node's name, node type, node hash at the size it was heard, full public key, hop count, SNR,
and the path it was heard by. The full public key SHALL be shown as 64 lower-case hex characters in
inline code so it can be copied whole. The path SHALL list each hop's hash with its shown label in
travel order, or state that the node was heard directly when the path is empty, and SHALL be
shortened with an ellipsis rather than exceed Discord's field length limit. Because advert names are
chosen by whoever sent the advert, the system SHALL escape Discord markdown in advert content —
including contact names shown in the path — and SHALL disable all mentions in the message, so that
an advert cannot ping users or roles or change the message's formatting.

#### Scenario: A name containing a mention
- **WHEN** a new companion adverts the name `@everyone`
- **THEN** the delivered message shows the text and notifies nobody

#### Scenario: A name containing markdown
- **WHEN** a new repeater adverts a name containing `**`, `_` or a masked link
- **THEN** the delivered message shows those characters literally

#### Scenario: An unnamed node
- **WHEN** the advert carries no name
- **THEN** the message states the node is unnamed and identifies it by node hash and full public key

#### Scenario: The full public key
- **WHEN** a new repeater event is delivered to a `discord` webhook
- **THEN** the public key field shows all 64 hex characters of the key as inline code, with no ellipsis

#### Scenario: A multi-byte node hash
- **WHEN** a new repeater whose key begins `a1b2` is heard with a path hash size of 2
- **THEN** the node hash field shows `a1b2`

#### Scenario: A path with known and unknown hops
- **WHEN** a new companion is heard through a hop resolving to `Hilltop` and then a hop resolving to no contact
- **THEN** the path field shows both hop hashes in order, labelled `Hilltop` and `<unknown>`

#### Scenario: A contact name in the path containing markdown
- **WHEN** a hop resolves to a contact named `**x**`
- **THEN** the path field shows `**x**` literally

#### Scenario: A zero-hop sighting
- **WHEN** the reception's path is empty
- **THEN** the path field states the node was heard directly

#### Scenario: A very long path
- **WHEN** the rendered path would exceed Discord's field length limit
- **THEN** the path field is cut to fit and ends with an ellipsis, and the message is still delivered

### Requirement: A webhook can be tested with a sample event
The system SHALL allow an operator to send a sample event of a chosen trigger to one webhook,
regardless of whether it is enabled and regardless of its hop filter, and SHALL report the outcome
of the attempt — delivered with status code, or failed with status code or reason. A sample event
SHALL be marked as a test in both formats so a receiver cannot mistake it for a real sighting,
SHALL carry a sample path containing a named hop and an `<unknown>` hop, and SHALL NOT be retried.

#### Scenario: Testing a Discord webhook
- **WHEN** an operator tests a `discord` webhook with `new_repeater`
- **THEN** one message marked as a test is posted, showing a sample path, and the outcome with its status code is reported

#### Scenario: Testing an unreachable webhook
- **WHEN** an operator tests a webhook whose host does not resolve
- **THEN** the reported outcome says the attempt failed and why, once, without retrying
