## ADDED Requirements

### Requirement: A loaded identity can be told to advert now, zero-hop or flood
The system SHALL allow an operator to request one zero-hop advert or one flood advert for any
identity loaded by the running process, whether it serves a room, a bot or nothing, and whether its
key is stored, loaded from a keyfile or generated for this run. Each is a distinct guarded action
per identity: it SHALL be performed only by a submission from a confirmation view that states what
that action does and mints a confirmation for that action and that identity alone. It SHALL NOT be
reachable by following, prefetching or reloading a page. It SHALL NOT require the operator's
password. It SHALL emit its own structured event naming the action, the identity, the outcome and
the acting user, whether it succeeds or is refused. A successful request SHALL also be stated in
the run's own output, naming the operator.

The zero-hop confirmation SHALL state that the advert reaches direct neighbours only and leaves the
flood schedule unchanged. The flood confirmation SHALL state that the advert is repeated by every
repeater in the mesh, and that it takes the place of the identity's next scheduled flood, which
moves one full interval out. Each confirmation SHALL show the identity's next scheduled flood as
it stands.

The system SHALL refuse the request and submit nothing when any of these holds, and the refusal
SHALL state which:
- the transmit gate is closed
- the run has no radio readback to compute airtime from
- the identity is not loaded by this run
- for a flood only, a flood advert from any identity of this run was submitted less than the
  inter-entity gap ago; the refusal SHALL state the time remaining

The system SHALL NOT impose any other limit on how often an operator may request either advert.

#### Scenario: Requesting a zero-hop advert
- **WHEN** an operator confirms a zero-hop advert for a loaded identity while the gate is open and a radio readback exists
- **THEN** exactly one zero-hop advert is submitted for that identity, its next scheduled flood is unchanged, the action is recorded as its own event naming the identity and the operator with outcome success, and the run's output states the request

#### Scenario: Requesting a flood advert
- **WHEN** an operator confirms a flood advert for a loaded identity while the gate is open, a radio readback exists, and no flood from this run is inside the inter-entity gap
- **THEN** exactly one flood advert is submitted for that identity, its next scheduled flood moves one interval out, and the action is recorded as its own event with outcome success

#### Scenario: The confirmations state the cost
- **WHEN** the flood confirmation for an identity is opened
- **THEN** it states that every repeater in the mesh repeats the advert and that the identity's next scheduled flood moves out, and shows when that flood is currently due; and the zero-hop confirmation states that the advert stops at direct neighbours

#### Scenario: No password is asked
- **WHEN** either confirmation is opened
- **THEN** it carries no password field, and a submission is decided without one

#### Scenario: The transmit gate is closed
- **WHEN** either advert is confirmed while the transmit gate is closed
- **THEN** nothing is submitted, the identity's next scheduled flood is unchanged, no airtime is charged, the refusal states that transmission is disabled, and the refusal is recorded as its own event

#### Scenario: No radio readback yet
- **WHEN** either advert is confirmed before the run has a radio readback
- **THEN** nothing is submitted, the refusal states that airtime cannot be computed until the board has answered, and the refusal is recorded

#### Scenario: A flood inside the inter-entity gap
- **WHEN** a flood advert is confirmed less than the inter-entity gap after any flood advert from this run
- **THEN** nothing is submitted, the refusal states the seconds remaining before another flood is accepted, and the refusal is recorded

#### Scenario: A zero-hop advert inside the inter-entity gap
- **WHEN** a zero-hop advert is confirmed less than the inter-entity gap after a flood advert from this run
- **THEN** it is accepted as if no flood had been sent

#### Scenario: A confirmation minted for another identity or the other kind
- **WHEN** a submission carries a confirmation minted for a different identity, for the other advert kind, one already used, or none
- **THEN** nothing is submitted and the refusal is recorded as its own event

#### Scenario: An identity this run does not hold
- **WHEN** an advert is requested for an identity this run has not loaded
- **THEN** nothing is submitted and the refusal states that this run does not hold that identity

#### Scenario: Repeated requests are not throttled beyond the gap
- **WHEN** an operator confirms a second zero-hop advert for the same identity immediately after the first
- **THEN** it is submitted like the first

#### Scenario: Reaching the actions from a room or a bot
- **WHEN** the rooms or bots administration list shows a room this run serves or a bot this run runs
- **THEN** it links to the advert actions for the identity that room or bot speaks as, and a room or bot this run does not serve offers no such link

### Requirement: Each loaded identity's advert schedule is visible
The system SHALL show, for every identity loaded by the running process, when its next flood
advert is scheduled, when it last flooded in this run or that it has not, how many adverts it has
sent in this run, and, while one is active, the override's interval and expiry.

#### Scenario: An identity that has not yet flooded
- **WHEN** the identities page is opened in a run where an identity has sent no flood advert
- **THEN** its next scheduled flood time is shown, and its last flood is shown as none this run

#### Scenario: An identity with an active override
- **WHEN** an identity has an active advert override
- **THEN** the override's interval and expiry are shown beside its schedule

#### Scenario: The schedule after a requested flood
- **WHEN** a flood advert has just been requested for an identity through the interface
- **THEN** the identities page shows its last flood as that request and its next scheduled flood one interval later
