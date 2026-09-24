# Spec Delta

## MODIFIED Requirements

### Requirement: A login is replay-guarded, and the guard survives restart
The system SHALL reject a login whose sender timestamp is not newer than the newest one already
recorded for that member, and SHALL persist that recorded timestamp so the guard is not reset by a
restart. The system SHALL match the reference implementation's exception: a login from an existing
member carrying an **empty** password is answered without the timestamp check, because that is how a
client re-establishes a lost route. Matching the reference implementation, such a login SHALL NOT
change the recorded timestamp. The report of an admitted login SHALL state whether its password was
empty.

#### Scenario: A captured login is replayed
- **WHEN** a previously seen login for an existing member is received again with the same timestamp and a non-empty password
- **THEN** it is refused, counted as a replay, and nothing is transmitted

#### Scenario: The guard after a restart
- **WHEN** a login is replayed after the process has been restarted
- **THEN** it is still refused, because the recorded timestamp was restored rather than reset

#### Scenario: A member re-establishing a route
- **WHEN** an existing member logs in with an empty password
- **THEN** it is answered so the route can be re-established, subject to the reply throttle

#### Scenario: A route re-establishment leaves the guard alone
- **WHEN** an existing member logs in with an empty password and a timestamp above the recorded one
- **THEN** the recorded timestamp is unchanged, so the member's later posts are measured against the same value as before the login

#### Scenario: A login with a password raises the guard
- **WHEN** a member logs in with a non-empty password and a timestamp above the recorded one
- **THEN** the recorded timestamp becomes the login's timestamp

#### Scenario: Reporting how a member logged in
- **WHEN** a login is admitted
- **THEN** the report names whether the password was empty

#### Scenario: A member whose clock has gone backwards
- **WHEN** a member's timestamps are permanently below its recorded value
- **THEN** its logins are refused, and the reported reason names the recorded timestamp and states that revoking the member allows a fresh login
