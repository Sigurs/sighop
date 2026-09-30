# Spec Delta

## ADDED Requirements

### Requirement: An event names its place once, before delivery
The system SHALL name the place of an event's advertised position once per event, after the event
has left the reception path and before it is rendered for any webhook, so that every webhook and
every retry of the event carries the same place. An event with no advertised position, or whose
position has no place, SHALL be delivered without one. Naming a place SHALL NOT delay or prevent a
delivery beyond the one-time gazetteer load.

#### Scenario: Several webhooks, one place
- **WHEN** an event with a position is delivered to a `json` and a `discord` webhook, and one delivery is retried
- **THEN** every body names the same place

#### Scenario: The gazetteer is unavailable
- **WHEN** an event with a position is raised and the gazetteer cannot be loaded
- **THEN** the event is delivered with its coordinates and no place

## MODIFIED Requirements

### Requirement: The JSON format is a versioned, documented event
The system SHALL send a `json` webhook an HTTP `POST` with content type `application/json` whose
body carries: a schema version of `1`; the trigger name; an event identifier unique to the event and
identical across retries of it; the time the event occurred in UTC; the node's full public key in
hex, its node hash as the key's first byte, its node hash at the size it was heard together with
that hash size, its advertised name or null, its node type, and its advertised position or null —
a position carrying its latitude, its longitude, a map URL that opens those coordinates, and its
place or null — a place carrying its neighborhood or null, its city, its country name and its
two-letter country code;
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

#### Scenario: A located node in JSON
- **WHEN** an event for a node whose advert carried coordinates is delivered to a `json` webhook
- **THEN** the node's position carries its latitude, its longitude and a map URL for those coordinates

#### Scenario: A named place in JSON
- **WHEN** an event for a node advertising coordinates in Stockholm is delivered to a `json` webhook
- **THEN** the node's position carries a place with city `Stockholm`, country `Sweden`, country code `SE`, and a neighborhood or null

#### Scenario: A position with no place in JSON
- **WHEN** an event for a node advertising coordinates far from any town is delivered to a `json` webhook
- **THEN** the node's position carries its coordinates and map URL, and its place is null

#### Scenario: A node with no advertised position in JSON
- **WHEN** an event for a node whose advert carried no coordinates is delivered to a `json` webhook
- **THEN** the node's position is null, and the reception's SNR is unaffected

### Requirement: The Discord format is safe to render advert content
The system SHALL send a `discord` webhook a message with one embed that names the trigger in words,
the node's name, node type, node hash at the size it was heard, full public key, hop count, location,
and the path it was heard by. The message SHALL NOT show the reception's SNR, which stays in the
`json` format. The full public key SHALL be shown as 64 lower-case hex characters in
inline code so it can be copied whole. The location SHALL be a link that opens the node's advertised
coordinates in a map, shown under the coordinates themselves, preceded on its own line by the
position's place as neighborhood, city and country separated by commas — the neighborhood left out
when there is none, and the line left out when the position has no place — and SHALL state that no
position was advertised when the advert carried none. The path SHALL list each hop's hash with its
shown label in travel order, or state that the node was heard directly when the path is empty, and
SHALL be shortened with an ellipsis rather than exceed Discord's field length limit. Because advert
names are chosen by whoever sent the advert, the system SHALL escape Discord markdown in advert
content — including contact names shown in the path — and SHALL disable all mentions in the message,
so that an advert cannot ping users or roles or change the message's formatting. Place names SHALL
be escaped the same way.

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

#### Scenario: A located node
- **WHEN** a new repeater adverts coordinates
- **THEN** the location field shows those coordinates as a link that opens them in a map

#### Scenario: A located node in a named place
- **WHEN** a new repeater adverts coordinates whose place is neighborhood `Södermalm`, city `Stockholm`, country `Sweden`
- **THEN** the location field shows `Södermalm, Stockholm, Sweden` on one line and the coordinates link below it

#### Scenario: A named place with no neighborhood
- **WHEN** a new repeater adverts coordinates whose place has city `Uppsala`, country `Sweden` and no neighborhood
- **THEN** the location field shows `Uppsala, Sweden` above the coordinates link

#### Scenario: A located node with no place
- **WHEN** a new repeater adverts coordinates that have no place
- **THEN** the location field shows only the coordinates link

#### Scenario: A node with no advertised position
- **WHEN** a new companion adverts no coordinates
- **THEN** the location field states that no position was advertised

#### Scenario: No SNR in the message
- **WHEN** any event is delivered to a `discord` webhook
- **THEN** the embed carries no SNR field

### Requirement: A webhook can be tested with a sample event
The system SHALL allow an operator to send a sample event of a chosen trigger to one webhook,
regardless of whether it is enabled and regardless of its hop filter, and SHALL report the outcome
of the attempt — delivered with status code, or failed with status code or reason. A sample event
SHALL be marked as a test in both formats so a receiver cannot mistake it for a real sighting,
SHALL carry a sample path containing a named hop and an `<unknown>` hop and a sample position whose
place is named as a real event's would be, and SHALL NOT be retried.

#### Scenario: Testing a Discord webhook
- **WHEN** an operator tests a `discord` webhook with `new_repeater`
- **THEN** one message marked as a test is posted, showing a sample path, the sample position's place and a location link, and the outcome with its status code is reported

#### Scenario: Testing an unreachable webhook
- **WHEN** an operator tests a webhook whose host does not resolve
- **THEN** the reported outcome says the attempt failed and why, once, without retrying
