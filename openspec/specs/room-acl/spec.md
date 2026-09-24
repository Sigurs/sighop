# room-acl Specification

## Purpose
Who may take part in a room and how they prove it — the login exchange, the permission levels, the
storage and rotation of passwords, and the rules that keep answering a stranger from being free.

## Requirements

### Requirement: A room accepts login over an anonymous request
The system SHALL accept a login addressed to a room server as an `ANON_REQ` carrying the sender's
full public key and an encrypted body of a sender timestamp, a sync timestamp and a password, and
SHALL derive the shared secret from the public key the envelope itself carries rather than from any
stored contact. A login SHALL be handled downstream of the decode stage, never inside it.

#### Scenario: A login arrives for a room server entity
- **WHEN** an `ANON_REQ` whose destination hash matches a room server entity is received and its MAC verifies
- **THEN** the body is decrypted, the sender timestamp, sync timestamp and password are read, and the login is evaluated against the room's passwords

#### Scenario: A login arrives for a node hash no room server carries
- **WHEN** an `ANON_REQ` is received whose destination hash matches no local room server
- **THEN** it is reported as received and no login is attempted

#### Scenario: A login whose MAC does not verify
- **WHEN** an `ANON_REQ` addressed to a room server fails MAC verification
- **THEN** no decryption result is used, no member is created, and nothing is transmitted

### Requirement: A password decides a permission level, and an unmatched password is answered with silence
The system SHALL compare the supplied password against the room's admin password and then its
guest password, granting administrator permission for the first and read-write permission for the
second. When neither matches, the system SHALL grant read-only permission if and only if the room
is configured to allow read-only access, and SHALL otherwise **transmit nothing at all** — no
refusal, no error payload, no acknowledgement.

#### Scenario: The admin password is supplied
- **WHEN** a login supplies the room's admin password
- **THEN** the sender is admitted with administrator permission and the reply says so

#### Scenario: The guest password is supplied
- **WHEN** a login supplies the room's guest password
- **THEN** the sender is admitted with read-write permission

#### Scenario: A wrong password with read-only access allowed
- **WHEN** a login supplies a password matching neither and the room allows read-only access
- **THEN** the sender is admitted with read-only permission, able to receive history and not to post

#### Scenario: A wrong password with read-only access not allowed
- **WHEN** a login supplies a password matching neither and the room does not allow read-only access
- **THEN** nothing is transmitted, the attempt is counted and reported, and no member row is created

### Requirement: Passwords are stored as Argon2id hashes and never in the clear
The system SHALL store every room password as an Argon2id hash carrying its own parameters and
salt, SHALL never store, log or render a room password in plaintext or in any recoverable form, and
SHALL keep verification off the path that receives and decodes packets so that a login cannot delay
a reception. The number of password verifications running at once SHALL be bounded.

#### Scenario: A room is created
- **WHEN** a room is created with an admin password
- **THEN** the stored value is an Argon2id hash from which the password cannot be recovered, and the plaintext appears in no log, no status output and no error message

#### Scenario: Verification while traffic is arriving
- **WHEN** a login is being verified and packets continue to arrive
- **THEN** reception, decoding and dispatch proceed without waiting for the verification to finish

#### Scenario: Many logins arrive at once
- **WHEN** more logins arrive than the configured verification concurrency
- **THEN** the excess is throttled and counted rather than verified in parallel

### Requirement: An empty guest password is legal but must be chosen explicitly
The system SHALL support a room with an empty guest password, which admits any sender that presents
one, and SHALL require that this is configured deliberately rather than being the state a room
starts in. A newly created room with no guest password configured SHALL refuse guest logins.

#### Scenario: A room created without a guest password
- **WHEN** a room is created and no guest password and no open-room choice is given
- **THEN** guest logins are refused and only the admin password admits anyone

#### Scenario: A room explicitly opened
- **WHEN** an operator explicitly configures the room as open
- **THEN** a login with an empty password is admitted with read-write permission, and the room is reported as open wherever its configuration is shown

### Requirement: Membership is durable and survives restart
The system SHALL store an admitted sender's public key, node hash and permission level, and SHALL
restore membership at startup so that a member who has logged in once is recognised after a restart
without logging in again. Subsequent traffic from a member SHALL be handled as ordinary addressed
traffic with no further login.

#### Scenario: A member posts after a restart
- **WHEN** a member that logged in before a restart sends a message after it
- **THEN** it is recognised as a member with its stored permission level and no re-login is required

#### Scenario: Membership counts are reported
- **WHEN** a run starts with rooms configured
- **THEN** the startup output reports each room and how many members were restored

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

### Requirement: Answering a login is rate-limited, counted and reported
The system SHALL bound how often it answers logins, both per source node hash and in total, and
SHALL count every refusal by reason and report those counts in periodic status output. A refusal to
answer SHALL never be silent in the operator's view, even though it is silent on the air.

#### Scenario: Repeated logins from one source
- **WHEN** logins from one source node hash arrive faster than the per-source limit
- **THEN** the excess is not answered, is counted, and is visible in status output

#### Scenario: A burst of logins from many sources
- **WHEN** logins arrive from many sources faster than the total limit
- **THEN** the excess is not answered and is counted, so the room server cannot be used to amplify traffic onto the mesh

### Requirement: Rotating a password evicts nobody, and removal is explicit
The system SHALL leave existing members admitted when a room password is changed, because
membership is keyed on the public key recorded at first login, and SHALL state this at the point a
password is rotated. Removing a member SHALL require an explicit revocation, after which that
member is treated as unknown and must log in again.

#### Scenario: A password is rotated
- **WHEN** a room's guest or admin password is changed
- **THEN** existing members continue to be recognised, only new logins are gated by the new password, and the output says so explicitly

#### Scenario: A member is revoked
- **WHEN** a member is revoked
- **THEN** its membership, permission level, sync position and replay guard are removed, and it is admitted again only by a successful login

### Requirement: A permission level bounds what a member may do
The system SHALL admit members at one of administrator, read-write or read-only, SHALL accept posts
only from read-write and administrator members, and SHALL deliver history to members at every
level. A post from a read-only member SHALL be discarded without an acknowledgement.

#### Scenario: A read-only member posts
- **WHEN** a read-only member sends a message to the room
- **THEN** nothing is stored, nothing is acknowledged, and the attempt is reported

#### Scenario: A read-only member receives history
- **WHEN** unsynced messages exist for a read-only member
- **THEN** they are delivered exactly as they would be to a read-write member
