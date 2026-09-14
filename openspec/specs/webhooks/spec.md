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

### Requirement: The JSON format is a versioned, documented event
The system SHALL send a `json` webhook an HTTP `POST` with content type `application/json` whose
body carries: a schema version of `1`; the trigger name; an event identifier unique to the event and
identical across retries of it; the time the event occurred in UTC; the node's full public key in
hex, its node hash, its advertised name or null, its node type, and its advertised position or null;
and the reception's hop count, SNR, RSSI and receive time, each null when unknown. Fields SHALL NOT
be removed or change meaning within schema version `1`.

#### Scenario: A new-repeater JSON event
- **WHEN** a `new_repeater` event is delivered to a `json` webhook
- **THEN** the body has `schema` 1, `event` `new_repeater`, an event identifier, and the node and reception fields above

#### Scenario: A retried delivery
- **WHEN** a delivery is retried
- **THEN** the body carries the same event identifier as the first attempt

### Requirement: The Discord format is safe to render advert content
The system SHALL send a `discord` webhook a message with one embed that names the trigger in words,
the node's name, node type, node hash, abbreviated public key, hop count and SNR. Because advert
names are chosen by whoever sent the advert, the system SHALL escape Discord markdown in advert
content and SHALL disable all mentions in the message, so that an advert cannot ping users or
roles or change the message's formatting.

#### Scenario: A name containing a mention
- **WHEN** a new companion adverts the name `@everyone`
- **THEN** the delivered message shows the text and notifies nobody

#### Scenario: A name containing markdown
- **WHEN** a new repeater adverts a name containing `**`, `_` or a masked link
- **THEN** the delivered message shows those characters literally

#### Scenario: An unnamed node
- **WHEN** the advert carries no name
- **THEN** the message states the node is unnamed and identifies it by node hash and key prefix

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

### Requirement: Webhooks require durable storage and are never sent from a replay
The system SHALL send no webhook when no database is configured, and SHALL send no webhook during a
replay run, whose receptions carry an earlier session's timestamps. Whether transmit is enabled
SHALL NOT affect webhooks, because they are not radio transmissions.

#### Scenario: A run without a database
- **WHEN** a run starts with no database configured and a new repeater is heard
- **THEN** no HTTP request is made

#### Scenario: A replay run
- **WHEN** a capture containing a first sighting of a repeater is replayed
- **THEN** no HTTP request is made

#### Scenario: A receive-only run
- **WHEN** a run with transmit disabled hears a new repeater and a subscribed webhook is enabled
- **THEN** the webhook is delivered

### Requirement: A webhook can be tested with a sample event
The system SHALL allow an operator to send a sample event of a chosen trigger to one webhook,
regardless of whether it is enabled and regardless of its hop filter, and SHALL report the outcome
of the attempt — delivered with status code, or failed with status code or reason. A sample event
SHALL be marked as a test in both formats so a receiver cannot mistake it for a real sighting, and
SHALL NOT be retried.

#### Scenario: Testing a Discord webhook
- **WHEN** an operator tests a `discord` webhook with `new_repeater`
- **THEN** one message marked as a test is posted and the outcome with its status code is reported

#### Scenario: Testing an unreachable webhook
- **WHEN** an operator tests a webhook whose host does not resolve
- **THEN** the reported outcome says the attempt failed and why, once, without retrying
