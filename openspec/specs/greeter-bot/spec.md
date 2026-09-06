# greeter-bot Specification

## Purpose

The greeter driver: it welcomes a node it has never said anything to with a single direct message,
and it is defined as much by what it refuses to greet — nodes it has already greeted, nodes far
away, nodes that are not people, nodes it has no route to — as by what it sends.

"Never said anything to" is not "never heard of". A node the platform has known for weeks and never
greeted is a node with an unsent welcome, and the record of what has been greeted is the only thing
that decides. What keeps that from meaning *the whole contact table at once* is that a new greeter
starts owing nothing: every contact present when it is created is recorded as already greeted, and
an operator releases them one at a time.

## Requirements

### Requirement: A contact is greeted exactly once, and the greeting record is what decides
The system SHALL greet a contact when no greeting record exists for it, whether or not the advert
that triggered the decision created the contact. A contact the platform already knew but has never
greeted SHALL be greeted; a contact carrying a greeting record SHALL NOT be, whatever that record
says happened. The record SHALL be durable and per bot, and SHALL be consulted before every
greeting.

Being previously *known* is not being previously *greeted*: a peer heard before the greeter
existed, added by an operator as a public key, or recorded by an earlier milestone has had nothing
said to it, and is exactly the peer a welcome is for.

#### Scenario: A first advert from an unknown node
- **WHEN** a verified advert creates a contact and every other gate passes
- **THEN** a greeting is sent to that contact

#### Scenario: An advert from a node known but never greeted
- **WHEN** an advert is observed from a contact that already existed and carries no greeting record, and every other gate passes
- **THEN** a greeting is sent to that contact

#### Scenario: A second advert from a greeted node
- **WHEN** a further advert from a contact carrying a greeting record is observed
- **THEN** no greeting is sent and the reception is otherwise handled as usual

#### Scenario: After a restart
- **WHEN** the runtime is restarted and adverts arrive from contacts greeted before the restart
- **THEN** none of them is greeted

#### Scenario: A greeting record without a contact record
- **WHEN** a contact is greeted, and later re-created because its contact record was removed
- **THEN** the greeting record still suppresses a second greeting

#### Scenario: An observed greeting
- **WHEN** a greeting is decided in observe mode
- **THEN** it is recorded as would-have-greeted, and switching the bot to active does not cause that contact to be greeted

### Requirement: A greeting nobody acknowledged is not a greeting delivered
The system SHALL treat an unacknowledged greeting as an attempt rather than as a delivery,
recording that it was made and retrying it on a later advert from the same contact once a
configured cooldown has passed. The number of attempts SHALL be bounded by configuration, and a
contact whose attempts are spent SHALL be left alone and reported as such. An acknowledged
greeting SHALL settle the contact permanently.

This is required because a greeting the recipient could not read is indistinguishable, from the
sender's side, from a recipient that was not listening — and recording either as delivered makes
one silent failure permanent.

#### Scenario: A greeting that is never acknowledged
- **WHEN** a greeting is sent, no acknowledgement arrives, and the contact adverts again after the cooldown
- **THEN** the greeting is sent again, and the attempt count recorded against that contact increases

#### Scenario: An advert inside the cooldown
- **WHEN** a contact with an unacknowledged greeting adverts before the cooldown has passed
- **THEN** no greeting is sent, and the suppression reports how long is left

#### Scenario: The attempts are spent
- **WHEN** every attempt a contact is allowed has gone unacknowledged
- **THEN** no further greeting is sent to it, and the suppression reports how many attempts went unanswered

#### Scenario: An acknowledged greeting
- **WHEN** a greeting is acknowledged
- **THEN** the contact is settled and is never greeted again, however often it adverts

#### Scenario: A retry across a restart
- **WHEN** the runtime is restarted between an unacknowledged greeting and the retry
- **THEN** neither the attempt count nor the cooldown is forgotten

### Requirement: A peer is introduced to before it is messaged
The system SHALL, before greeting a contact that may not hold the greeter's public key, emit an
advert for the greeter's own identity and wait for it to reach the air, because a direct message
is decryptable only by a node that already holds the sender's key. The advert SHALL reach at
least as far as the contact: an advert that stops at direct neighbours for a contact heard
directly, and one the mesh repeats for a contact heard further away.

The cost of those two differs by orders of magnitude, so the system SHALL spend the far-reaching
one only after a greeting has actually gone unacknowledged, and never on the first attempt to a
contact that may already hold the key.

When that first bare greeting does go unacknowledged, the system SHALL transmit the far-reaching
advert and greet again **without waiting for the cooldown**, because the silence has already
identified its own cause: a peer that cannot decrypt us will still not be able to in fifteen
minutes, and it is adverting — hence awake and reachable — now. That escalation SHALL happen at
most once per contact, after which the ordinary cooldown governs any further attempt.

Before believing that silence, the system SHALL keep listening for a configured grace period past
the message path's last attempt, without transmitting anything further, because an acknowledgement
returning over a longer path than the one sent on is late rather than absent and the action taken
on the difference costs the whole mesh.

#### Scenario: Greeting a direct neighbour
- **WHEN** a contact heard directly is about to be greeted
- **THEN** an advert that stops at direct neighbours is transmitted first, and the greeting follows it

#### Scenario: A first greeting to a more distant contact
- **WHEN** a contact heard over a repeater is greeted for the first time
- **THEN** no advert is transmitted, because the contact may already hold the key

#### Scenario: Escalating to a more distant contact
- **WHEN** that bare greeting goes unacknowledged
- **THEN** an advert the mesh repeats is transmitted and the greeting is sent again in the same reaction, without waiting for the cooldown

#### Scenario: The escalation happens once
- **WHEN** the greeting that followed the far-reaching advert also goes unacknowledged
- **THEN** nothing further is transmitted to that contact until the cooldown has passed and it adverts again

#### Scenario: A late acknowledgement
- **WHEN** an acknowledgement arrives after the message path's last attempt but inside the grace period
- **THEN** the greeting counts as acknowledged, the contact is settled, and no advert is transmitted

#### Scenario: The grace period costs no airtime
- **WHEN** a greeting is waiting out its grace period
- **THEN** no packet is transmitted during it, and the number of attempts the message path made is unchanged

#### Scenario: Ordering against the outbound queue
- **WHEN** an advert is transmitted before a greeting
- **THEN** the greeting is not composed until the advert has reached the air, so it cannot overtake the advert that makes it readable

#### Scenario: Observe mode
- **WHEN** a bot in observe mode decides to greet
- **THEN** no advert is transmitted either, because an advert is a transmission

### Requirement: A new greeter starts with nothing owed to the contacts it already knows
The system SHALL, when a greeter is created, record every contact the platform already holds as
already greeted, marked as seeded rather than as sent, so that creating a greeter on an established
node does not oblige it to greet the whole contact table. Nodes heard after that point carry no
record and are greeted normally.

#### Scenario: Creating a greeter on an established node
- **WHEN** a greeter is created and the contact table already holds contacts
- **THEN** each of them is recorded as greeted, the count is reported, and none of them is greeted when it next adverts

#### Scenario: Creating a greeter on an empty node
- **WHEN** a greeter is created and the contact table is empty
- **THEN** nothing is seeded and the output says so

#### Scenario: A contact heard after creation
- **WHEN** an advert is observed from a contact that was not present when the greeter was created
- **THEN** it carries no greeting record and is greeted if the other gates pass

### Requirement: An operator decides, per contact, whether it has been greeted
The system SHALL let an operator inspect the greeting record of a named contact, mark a contact as
greeted so that it is never greeted, and clear a contact's record so that it is greeted the next
time it adverts. Clearing SHALL state that consequence rather than reporting only that a record was
removed.

#### Scenario: Releasing a seeded contact
- **WHEN** an operator clears the greeting record of a contact that was seeded at creation
- **THEN** the output states that the contact will be greeted when it next adverts, and it is

#### Scenario: Excusing a contact from being greeted
- **WHEN** an operator marks a contact as greeted
- **THEN** no greeting is sent to it, and the record shows it was set by an operator rather than sent

#### Scenario: Inspecting one contact
- **WHEN** an operator asks whether a named contact has been greeted
- **THEN** the answer names the contact, whether a record exists, and what that record says happened

### Requirement: A greeting is gated on how far away the node is
The system SHALL greet only a contact whose triggering advert arrived within a configured maximum
hop count, and SHALL treat that maximum as configuration of the bot with a documented default.
An advert exceeding it SHALL be recorded as a contact as usual, and the greeting SHALL be
suppressed and counted with the hop count that exceeded the limit.

#### Scenario: An advert from beyond the hop limit
- **WHEN** a verified advert creates a contact and arrived over more hops than configured
- **THEN** no greeting is sent, and the suppression is reported with the reception's hop count and the limit

#### Scenario: A zero-hop advert
- **WHEN** a verified advert creates a contact and arrived directly, with no repeater in between
- **THEN** the hop gate passes

#### Scenario: The limit is visible
- **WHEN** the bot is shown or a run reports it
- **THEN** the configured hop limit is reported alongside its other limits

### Requirement: A greeting is gated on the node type being one worth greeting
The system SHALL greet only a contact whose verified advert declares a node type in the bot's
configured set, defaulting to ordinary chat nodes, so that repeaters, room servers and other
infrastructure are not sent a welcome message. A suppression by node type SHALL be counted and
reported with the type that was refused.

#### Scenario: A repeater adverts for the first time
- **WHEN** a verified advert from a repeater creates a contact
- **THEN** no greeting is sent and the suppression names the node type

#### Scenario: A chat node adverts for the first time
- **WHEN** a verified advert from a chat node creates a contact and the other gates pass
- **THEN** a greeting is sent

### Requirement: A greeting is never flooded
The system SHALL send a greeting only over a route known for the contact, and SHALL suppress it
with that reason when no route is known, because a greeting is unsolicited traffic to a peer that
has never contacted us and flooding one imposes its cost on the whole mesh.

#### Scenario: No route to a new contact
- **WHEN** a contact passes every other gate but no route to it is known
- **THEN** no greeting is sent and the suppression reports that no route was known

#### Scenario: A route learned from the advert
- **WHEN** the triggering advert established a route to the contact
- **THEN** the greeting is sent over that route

### Requirement: The greeting text is configured and bounded
The system SHALL take the greeting text from the bot's configuration, SHALL refuse at configuration
time a text that cannot be carried in a single direct message, and SHALL NOT truncate or split a
greeting at send time.

#### Scenario: Configuring an over-long greeting
- **WHEN** a greeting text too long for one direct message is configured
- **THEN** the configuration is refused, stating the limit and the length given

#### Scenario: A configured greeting
- **WHEN** a greeting is sent
- **THEN** its text is exactly the configured text

### Requirement: Every greeting and every suppression is reported and counted
The system SHALL report each greeting decision — sent, would-have-sent, or suppressed — naming the
contact, and for a suppression the reason, and SHALL maintain per-reason counters that a run
reports, so that a greeter that is greeting nobody is distinguishable from a mesh that has gone
quiet and from a greeter that is broken.

#### Scenario: A greeting is sent
- **WHEN** a greeting is transmitted
- **THEN** the contact, the route used and the acknowledgement outcome are reported

#### Scenario: Counters over a run
- **WHEN** a run reports its bots
- **THEN** the greeter's greetings sent, greetings observed, and suppressions by reason are reported

#### Scenario: A greeter that greets nobody
- **WHEN** every advert in a period is suppressed
- **THEN** the reasons and their counts are reported, rather than the greeter appearing idle

### Requirement: A greeting rides the ordinary outbound message path
The system SHALL send a greeting as an ordinary direct message at the priority class originated
traffic already uses, with the same retry, acknowledgement and timeout behaviour, and SHALL NOT
retry a greeting beyond what that path does for any message.

#### Scenario: A greeting is acknowledged
- **WHEN** the recipient acknowledges the greeting
- **THEN** the outcome is recorded as acknowledged, with the attempt that was acknowledged

#### Scenario: A greeting exhausts its attempts
- **WHEN** the outbound path exhausts its attempts without an acknowledgement
- **THEN** the outcome is recorded as unacknowledged, and the contact becomes eligible for a further greeting once the cooldown has passed and while attempts remain
