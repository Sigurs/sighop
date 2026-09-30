# webhooks Specification

## Purpose

Tells systems outside sighop — chat services, automation tools — when something worth an operator's
attention happens on the mesh, starting with a new repeater or a new companion being heard.

## Requirements
### Requirement: A webhook is stored configuration with a name, a target, a format and triggers
The system SHALL store each webhook durably with a unique name, a target URL, a payload format of
either `json` or `discord`, a non-empty set of triggers, an optional maximum hop count, and an
enabled flag. A new webhook SHALL be enabled. The system SHALL refuse a target URL whose scheme is
not `http` or `https` or that has no host, SHALL refuse an unknown format, SHALL refuse an unknown
trigger naming the triggers that exist, SHALL refuse an empty trigger set, SHALL refuse a negative
maximum hop count, and SHALL refuse a name already in use.

#### Scenario: Adding a webhook
- **WHEN** a webhook is added with a name, an `https` URL, the `discord` format and the `new_repeater` trigger
- **THEN** it is stored enabled, with no hop limit, subscribed to `new_repeater` only

#### Scenario: An unknown trigger
- **WHEN** a webhook is added or changed naming a trigger that does not exist
- **THEN** the change is refused, the refusal lists `new_repeater` and `new_companion`, and nothing is stored

#### Scenario: A URL that is not HTTP
- **WHEN** a webhook is added with a URL such as `file:///etc/passwd` or `ftp://host/`
- **THEN** the change is refused and nothing is stored

#### Scenario: A plain-HTTP URL
- **WHEN** a webhook is added with an `http` URL
- **THEN** it is stored, and the output states that the URL and every payload will cross the network unencrypted

### Requirement: A webhook URL is a secret at rest and on display
The system SHALL store a webhook's target URL sealed under the platform's sealing secret, so that a
database dump alone does not reveal it. After a URL is stored the system SHALL NOT display it in
full on any surface; listings and detail views SHALL show the scheme and host only. Replacing a
URL SHALL be possible; reading it back SHALL NOT. A URL SHALL NOT appear in any log event.

#### Scenario: Listing webhooks
- **WHEN** webhooks are listed on the command line or in the interface
- **THEN** each target is shown as its scheme and host, and no path or query is shown

#### Scenario: A sealing secret that does not open a stored URL
- **WHEN** a stored webhook URL does not authenticate under the configured sealing secret
- **THEN** that webhook is reported as unusable with the reason, it is not delivered to, and other webhooks are unaffected

#### Scenario: A delivery failure is logged
- **WHEN** a delivery fails
- **THEN** the log event names the webhook and its host and does not contain the URL's path or query

### Requirement: New-repeater and new-companion triggers fire once per first verified sighting
The system SHALL raise a `new_repeater` event when a verified advert creates a contact whose node
type is repeater, and a `new_companion` event when a verified advert creates a contact whose node
type is chat. The system SHALL NOT raise either event for an advert that updates a contact already
known — whether it was heard before, restored from the database, or added by an operator from a
public key — and SHALL NOT raise either for room servers, sensors, or unknown node types. An
unverified advert SHALL raise nothing.

#### Scenario: A repeater is heard for the first time
- **WHEN** a verified advert from a repeater whose key the node has never held is received
- **THEN** exactly one `new_repeater` event is raised for that key

#### Scenario: The same repeater adverts again
- **WHEN** a later advert, or another flood copy of the same advert, arrives from that repeater
- **THEN** no further event is raised

#### Scenario: A known contact after restart
- **WHEN** the runtime restarts with a database holding a contact and that contact adverts
- **THEN** no event is raised

#### Scenario: A contact an operator pasted in is first heard
- **WHEN** a contact added from a public key alone is heard in a verified advert for the first time
- **THEN** no event is raised

#### Scenario: A room server is heard for the first time
- **WHEN** a verified advert creates a contact whose node type is room server
- **THEN** no event is raised

### Requirement: An event is delivered only to webhooks that subscribe to it and pass their filter
The system SHALL deliver an event to every enabled webhook whose trigger set contains the event's
trigger and whose maximum hop count, when set, is not exceeded by the reception's hop count. A
reception whose hop count is unknown SHALL pass a webhook with no hop limit and SHALL NOT pass one
with a hop limit. A disabled webhook SHALL receive nothing.

#### Scenario: Trigger match
- **WHEN** a `new_companion` event is raised and one enabled webhook subscribes to `new_companion` and another only to `new_repeater`
- **THEN** only the first receives a delivery

#### Scenario: Hop filter
- **WHEN** a `new_repeater` event is raised from a reception of 3 hops and a subscribed webhook has a maximum of 1 hop
- **THEN** that webhook receives nothing

#### Scenario: Disabled webhook
- **WHEN** an event is raised that a disabled webhook subscribes to
- **THEN** that webhook receives nothing

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

### Requirement: Delivery never runs on the reception path
The system SHALL hand an event to delivery without waiting for any network I/O and without being
able to raise into advert processing. Pending events SHALL be held in a bounded queue; when it is
full the oldest pending event SHALL be dropped and counted. Each HTTP attempt SHALL be bounded by a
timeout.

#### Scenario: An unreachable endpoint
- **WHEN** a webhook's endpoint does not answer
- **THEN** advert processing, contact persistence, bots and the radio continue unaffected, and the attempt ends at its timeout

#### Scenario: Queue overflow
- **WHEN** events are raised faster than delivery drains them and the queue is full
- **THEN** the oldest pending event is dropped, the drop is counted and logged, and the newest is kept

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

### Requirement: Delivery is best-effort with bounded retries
The system SHALL treat a `2xx` response as delivered. It SHALL retry a delivery after a connection
failure, a timeout, a `5xx` response or a `429` response, with increasing delay, up to a fixed
number of attempts; on `429` it SHALL wait at least the delay the response's `Retry-After` states,
within a cap. It SHALL NOT retry any other `4xx` response, and SHALL NOT follow redirects. After the
last attempt the event SHALL be abandoned for that webhook and the failure recorded. Events not yet
delivered SHALL NOT survive a process restart. A delivery that fails for one webhook SHALL NOT delay
or prevent delivery to another beyond the time of its own attempts.

#### Scenario: A transient server error
- **WHEN** the endpoint answers `503` and then `204`
- **THEN** the event is delivered on the second attempt and recorded as delivered

#### Scenario: Rate limited
- **WHEN** the endpoint answers `429` with `Retry-After: 2`
- **THEN** the next attempt is not made sooner than 2 seconds later

#### Scenario: A rejected payload
- **WHEN** the endpoint answers `400` or `404`
- **THEN** no retry is made and the failure is recorded with the status code

#### Scenario: Retries exhausted
- **WHEN** every attempt fails
- **THEN** the event is abandoned for that webhook, counted as failed, and the webhook's last failure records when and why

### Requirement: Each webhook's last delivery outcome is recorded
The system SHALL record for each webhook the time of its last successful delivery and the time and
reason of its last failed delivery, so that an operator can tell a webhook that works from one that
has silently stopped. A failure to record the outcome SHALL NOT affect delivery.

#### Scenario: Inspecting a failing webhook
- **WHEN** a webhook whose last delivery failed with `404` is shown
- **THEN** its last failure time and the `404` reason are visible alongside its last success time

### Requirement: Configuration changes reach a running process without restart
The system SHALL apply a webhook added, changed, enabled, disabled or removed — from the command line
in another process or from the interface — to events raised after the change, without restarting
the run. If the stored configuration cannot be read when an event is to be delivered, the system
SHALL deliver using the configuration it last read successfully and SHALL report the read failure.

#### Scenario: A webhook disabled from the command line
- **WHEN** a webhook is disabled with the command line while a run is active, and a new repeater is then heard
- **THEN** that webhook receives nothing

#### Scenario: Database unreachable at event time
- **WHEN** an event is raised while the database cannot be read
- **THEN** the event is delivered to the webhooks from the last successful read, and the read failure is logged

### Requirement: Webhooks are never sent from a replay
The system SHALL send no webhook from a replayed capture, whose receptions carry an earlier
session's timestamps. Whether transmit is enabled SHALL NOT affect webhooks, because they are not
radio transmissions.

#### Scenario: A replay
- **WHEN** a capture containing a first sighting of a repeater is replayed
- **THEN** no HTTP request is made

#### Scenario: A receive-only run
- **WHEN** a run with transmit disabled hears a new repeater and a subscribed webhook is enabled
- **THEN** the webhook is delivered

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

### Requirement: A webhook can be renamed without changing where it delivers
The system SHALL provide an action that changes a stored webhook's name and nothing else. The
webhook's target URL, format, triggers, hop limit, enabled state and recorded delivery outcomes
SHALL be unchanged by a rename, and a running process SHALL use the new name without a restart, on
the same terms as any other webhook configuration change.

The system SHALL refuse a rename whose new name is empty or only whitespace, and SHALL refuse a
rename to a name another webhook already holds, because webhook names are unique and webhooks are
addressed by name on the command line. A rename to the name the webhook already holds SHALL be
accepted and change nothing.

A rename SHALL NOT render a stored URL in full anywhere, including in a form re-shown after a
refused rename.

#### Scenario: Renaming a webhook
- **WHEN** a webhook is renamed
- **THEN** it is listed under its new name with its format, triggers, hop limit, enabled state and recorded delivery outcomes unchanged

#### Scenario: The target is untouched
- **WHEN** a webhook is renamed and an event it subscribes to then fires
- **THEN** it is delivered to the same target as before

#### Scenario: Renaming to a name already in use
- **WHEN** a rename is asked for with a name another webhook already holds
- **THEN** the rename is refused for the same reason adding a duplicate name gives, and nothing is changed

#### Scenario: Renaming to an empty name
- **WHEN** a rename is asked for with an empty or whitespace-only name
- **THEN** the rename is refused and nothing is changed

#### Scenario: A refused rename discloses nothing
- **WHEN** a rename is refused
- **THEN** the stored URL appears as scheme and host at most, and no URL path or query is present in what is shown
