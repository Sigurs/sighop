# Spec Delta

## MODIFIED Requirements

### Requirement: A poll logs in as a guest with a blank password, then asks for status and neighbours
The system SHALL poll a repeater by sending, from the login identity, an anonymous login request
carrying an empty password and a timestamp greater than any it has sent that repeater, and on an
accepted login SHALL send a status request followed by neighbour requests. Neighbour requests SHALL
be paged from offset zero until the number of entries received reaches the total the repeater
reports, an answer carries no entries, or a fixed page limit is reached. The system SHALL NOT
offer or send any other password. Each request SHALL be routed directly when a route to the
repeater is known and flooded otherwise, except for the login flood fallback below.

A request that goes unanswered within its step's timeout SHALL be sent again, as a new request
with a new timestamp, until the step has been attempted three times; only then SHALL the poll end
at that step. A repeater counts as having answered recently when it has answered a login from us
within the recency window. For a repeater that has not answered recently, the login SHALL be
attempted once. For a repeater that has answered recently and has a known direct route, the third
login attempt SHALL be flooded rather than sent along that route. A login
that is already flooded because no route is known SHALL be sent once per poll. A submission the
scheduler does not send SHALL end the poll without a further attempt.

#### Scenario: A repeater with no guest password
- **WHEN** a selected repeater whose guest password is unset is polled
- **THEN** it is sent a login with an empty password, answers it, and is then asked for status and for its neighbours

#### Scenario: Paging neighbours
- **WHEN** a repeater reports 25 neighbours and each answer holds at most 11
- **THEN** three neighbour requests are answered, at offsets 0, 11 and 22, and all 25 entries are recorded

#### Scenario: A repeater with a guest password set
- **WHEN** a repeater that has not answered a login within the recency window is polled, because its guest password is set
- **THEN** one login is sent, it goes unanswered, no status or neighbour request is sent, no flood is sent, and the poll is recorded as login unanswered

#### Scenario: A known route
- **WHEN** a route to the repeater has been learned
- **THEN** the login and requests are sent directly along it rather than flooded

#### Scenario: A status request lost once
- **WHEN** a login is answered, the first status request goes unanswered, and the second is answered
- **THEN** the poll goes on to the neighbour requests and is recorded with the status, not as status unanswered

#### Scenario: A neighbour page lost once
- **WHEN** the status is answered and the first neighbour request at offset 0 goes unanswered but its resend is answered
- **THEN** paging continues from the entries that resend returned

#### Scenario: A stale direct route
- **WHEN** a repeater that has answered recently does not answer two direct logins along its learned route
- **THEN** the third login is flooded, and when that is answered the route it returns is used for the status and neighbour requests

#### Scenario: A repeater that answered recently is unreachable
- **WHEN** a repeater that has answered recently answers none of its three logins
- **THEN** two direct logins and one flooded login were sent, and the poll is recorded as login unanswered

#### Scenario: A repeater that stopped answering
- **WHEN** a repeater last answered a login four days ago, with a 3-day recency window, and is still heard advertising
- **THEN** it is sent one direct login per poll and no flood

#### Scenario: No route known
- **WHEN** a repeater with no learned route is polled and its flooded login goes unanswered
- **THEN** no further login is sent to it in that poll

### Requirement: Answers are accepted only when they match an outstanding request
The system SHALL accept a response only when it decrypts under the shared secret of the login
identity and the repeater being polled, and — for status and neighbour requests — only when its
echoed timestamp equals the timestamp of any attempt of the step currently outstanding to that
repeater. A response bundled inside a returned path SHALL be accepted on the same terms as one sent
directly. A response that matches nothing outstanding SHALL be counted and discarded, and so SHALL
a second answer to a step that has already been answered. Each attempt SHALL wait for its answer
for a bounded time derived from the frame's airtime and route; an attempt not answered in that
time SHALL be followed by the step's next attempt, and a step whose attempts are all unanswered
SHALL end the poll. When an answer arrives by flood, the system SHALL wait for the mesh's re-floods
of it to subside before transmitting again, and SHALL then send the repeater a direct path return
carrying the route that flood took, as stock clients do. The next request SHALL not be sent until
every relay on that path return's route has had time to forward it.

#### Scenario: A late answer to an earlier poll
- **WHEN** a status response arrives echoing a timestamp from a poll that has ended
- **THEN** it is counted as unmatched and nothing is recorded from it

#### Scenario: A late answer to an earlier attempt
- **WHEN** a status request is resent after a timeout and then an answer echoing the first attempt's timestamp arrives
- **THEN** that answer is accepted as the step's answer, and a later answer echoing the second attempt's timestamp is counted as unmatched

#### Scenario: A flooded request answered by path return
- **WHEN** a flooded login is answered by a returned path that carries the login response
- **THEN** the login is accepted and the returned route is learned

#### Scenario: A flooded answer
- **WHEN** a repeater answers a request by flood, because it holds no route to the station
- **THEN** nothing further is sent to it until the re-floods of that answer have had time to die down, and it is then sent a direct path return stating the route the flood took, so that later answers come direct

#### Scenario: A path return sent through a relay
- **WHEN** a path return is sent to a repeater along a route through one or more relays
- **THEN** the next request to it waits until each relay has had time to forward the path return, so that no relay is still transmitting when the request reaches it

#### Scenario: Status not answered
- **WHEN** a login is accepted but none of the three status requests is answered in time
- **THEN** the poll is recorded as status unanswered and no neighbour request is sent

### Requirement: Every poll is recorded with its outcome, status and neighbours
The system SHALL record, for each repeater polled, when the poll started, the login identity, the
route used, the outcome — succeeded, not sent, login unanswered, status unanswered, or neighbours
incomplete — and the number of requests that were resends of an unanswered one. The route SHALL
name every route used during the poll in order, so a login flood fallback is visible. A succeeded
or neighbours-incomplete poll SHALL carry every status field the repeater returned. Neighbour
entries SHALL be recorded as received: the key prefix, seconds since heard as of the answer, and
signal-to-noise ratio in decibels. A neighbour prefix SHALL be shown as a known contact only when
exactly one contact's public key starts with it. Polls recorded before resends were counted SHALL
show no resend count rather than zero.

#### Scenario: A complete poll
- **WHEN** login, status and every neighbour page are answered at the first attempt
- **THEN** one poll record with outcome succeeded, its status fields, every neighbour entry and a resend count of zero is stored

#### Scenario: A poll that needed resends
- **WHEN** a poll succeeds after one status request and one neighbour request were each sent twice
- **THEN** the poll is recorded as succeeded with a resend count of two

#### Scenario: A poll saved by the flood fallback
- **WHEN** two direct logins go unanswered and the flooded third is answered
- **THEN** the recorded route names the direct route followed by the flood, and the resend count includes both resent logins

#### Scenario: Neighbours cut short
- **WHEN** the status is answered but none of the attempts at the second neighbour page is
- **THEN** the poll is recorded as neighbours incomplete, with the status and the first page's entries

#### Scenario: An ambiguous neighbour prefix
- **WHEN** a neighbour prefix matches two known contacts
- **THEN** the neighbour is shown by its prefix and marked ambiguous, not attributed to either contact
