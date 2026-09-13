## Purpose

Who may use the web interface and how that is established: operator accounts, signing in and
out, sessions and their expiry, resistance to password guessing, and the acting user every
state-changing and guarded action is attributed to. The interface can transmit and reveal the
private keys that are the platform's identities, so nothing it serves is reachable without it.

## ADDED Requirements

### Requirement: Every part of the interface requires a signed-in user unless explicitly public
The system SHALL refuse every page, form submission, partial and live-feed connection to a
request that does not carry a valid session, except for an explicit, fixed set of public
routes consisting only of the sign-in form, its submission, and static assets that carry no
platform state. A route SHALL be public only by being named in that set, so that a route added
later is protected without anyone remembering to protect it. There SHALL be no configuration,
command-line option or environment variable that serves the interface without authentication,
whatever address it is bound to.

#### Scenario: An unauthenticated page request
- **WHEN** a request without a valid session asks for any page that is not public
- **THEN** the browser is sent to the sign-in form, and no platform state is present in the response

#### Scenario: An unauthenticated state-changing request
- **WHEN** a request without a valid session submits any state-changing request
- **THEN** it is refused, nothing changes, nothing is transmitted, and the refusal is recorded in the request's event

#### Scenario: An unauthenticated feed connection
- **WHEN** a live-feed connection is opened without a valid session
- **THEN** the connection is closed before any record is sent

#### Scenario: A newly added route
- **WHEN** the set of routes the application serves is enumerated
- **THEN** every route outside the public set refuses a request without a session

#### Scenario: Authentication cannot be turned off
- **WHEN** the interface is served on a loopback address
- **THEN** it requires a signed-in user exactly as it does on any other address

#### Scenario: Static assets carry no state
- **WHEN** a public static asset is requested without a session
- **THEN** it is served, and it contains no platform state, token or key material

### Requirement: Operator accounts are durable, hashed, and managed from the terminal
The system SHALL store operator accounts in the database, each with a unique username, a
password stored only as a salted memory-hard hash whose parameters travel with it, an enabled
state, and the time its password was last set. Usernames SHALL be compared case-insensitively
and stored in one normalised form. The system SHALL NOT store, log or render a password or its
hash anywhere else. Accounts SHALL be created, listed, re-passworded, disabled, re-enabled and
removed through the command line only.

#### Scenario: An account is created
- **WHEN** an operator creates an account with a username and a password
- **THEN** the account is stored with a hash of the password and never the password itself, and it is enabled

#### Scenario: A username that differs only in case
- **WHEN** an account is created whose username differs from an existing one only in letter case
- **THEN** creation is refused, naming the existing account

#### Scenario: Listing accounts
- **WHEN** accounts are listed
- **THEN** each shows its username, whether it is enabled, when it was created and when its password was last set, and no hash is shown

### Requirement: Signing in establishes a new session and reveals nothing to a guesser
The system SHALL sign a user in only when the submitted username names an enabled account and
the submitted password verifies against that account's hash. On success it SHALL issue a new
session identifier that has never been used before, regardless of any identifier the browser
already presented. On failure it SHALL give the same response, in wording and in status, whether
the username is unknown, the account is disabled, or the password is wrong, and SHALL spend the
same password verification work in each case. Password verification SHALL NOT run on the event
loop that carries the radio, and the number running at once SHALL be bounded.

#### Scenario: A successful sign-in
- **WHEN** a correct username and password for an enabled account are submitted
- **THEN** a new session is issued, the browser is sent to the page it originally asked for or to the overview, and a sign-in event records the username and a success outcome

#### Scenario: A wrong password and an unknown username are indistinguishable
- **WHEN** a sign-in is submitted once with a wrong password for an existing account and once with a username that does not exist
- **THEN** both receive the same response and both perform a password verification

#### Scenario: A disabled account
- **WHEN** a correct password is submitted for a disabled account
- **THEN** the sign-in fails with the same response as a wrong password, and the event records that the account is disabled

#### Scenario: Session fixation
- **WHEN** a browser that already holds a session identifier signs in
- **THEN** the identifier it held is not the one it is issued, and the old one grants nothing

#### Scenario: A password is never recorded
- **WHEN** any sign-in attempt is recorded
- **THEN** the event carries the submitted username and the outcome, and never the submitted password

### Requirement: Repeated sign-in failures are slowed per account and per client
The system SHALL delay further sign-in attempts after consecutive failures, tracked separately
for the targeted username and for the requesting client address, with the delay growing with
the number of failures up to a fixed ceiling and reset by a success. While a delay is in force
an attempt SHALL be refused without verifying the password. The tracking state SHALL be bounded
in size, so that many distinct usernames or addresses cannot exhaust memory.

#### Scenario: Guessing one account
- **WHEN** several consecutive wrong passwords are submitted for one username
- **THEN** further attempts for that username are refused until the delay passes, without a password verification being run, and each refusal is recorded

#### Scenario: Guessing from one client
- **WHEN** one client address submits consecutive failures across many usernames
- **THEN** further attempts from that address are refused until the delay passes

#### Scenario: A success resets the delay
- **WHEN** a correct sign-in follows failures that did not reach a delay
- **THEN** the failure count for that username and that address is reset

#### Scenario: Tracking stays bounded
- **WHEN** failures arrive for more distinct usernames and addresses than the tracking bound
- **THEN** memory held for tracking does not grow past the bound

### Requirement: Sessions are held in memory, expire, and end on sign-out
The system SHALL hold sessions in the serving process only, bounded in number. A session SHALL
end after a period without requests, after a maximum lifetime regardless of activity, when its
user signs out, and when the process stops. Signing out SHALL be a state-changing request. The
session cookie SHALL be unreadable by page scripts, SHALL NOT be sent on cross-site requests,
and SHALL carry no expiry of its own beyond the browser session, the server's expiry being the
authority.

#### Scenario: An idle session
- **WHEN** a session makes no request for longer than the idle limit
- **THEN** its next request is treated as unauthenticated

#### Scenario: A long-lived session
- **WHEN** a session stays active beyond the maximum lifetime
- **THEN** its next request is treated as unauthenticated, however recently it was used

#### Scenario: Signing out
- **WHEN** a signed-in user signs out
- **THEN** the session ends immediately, its identifier grants nothing afterwards, and an event records it

#### Scenario: Sign-out by navigation
- **WHEN** a browser follows a link or reloads a page that points at sign-out
- **THEN** no session is ended by that request

#### Scenario: A restart
- **WHEN** the process is restarted
- **THEN** every session from before the restart grants nothing

#### Scenario: The cookie's attributes
- **WHEN** a session cookie is issued
- **THEN** it is marked inaccessible to scripts and restricted to same-site requests

### Requirement: An account change made elsewhere reaches existing sessions within a minute
The system SHALL re-check each session's account against the database at least once a minute
while the session is used. A session whose account has been disabled, removed, or had its
password set since the session was issued SHALL end at that check. If the account cannot be
re-checked because the database is unreachable, ordinary pages SHALL remain available to the
session, and every guarded action SHALL be refused until a re-check succeeds.

#### Scenario: An account is disabled from the terminal while signed in
- **WHEN** an account is disabled through the command line while a browser holds a session for it
- **THEN** within one minute that browser's next request is treated as unauthenticated

#### Scenario: A password is changed from the terminal
- **WHEN** an account's password is set through the command line
- **THEN** sessions issued before the change end at their next re-check

#### Scenario: The database is unreachable at re-check time
- **WHEN** a session's re-check cannot reach the database
- **THEN** read-only pages continue to be served to it, and a guarded action is refused stating that the account cannot currently be verified

### Requirement: Guarded actions require the acting user's password
The system SHALL require, for revealing a private key, exporting a private key, enabling
transmission and raising the airtime ceiling, that the confirmation submitted carries the
signed-in user's current password in addition to the one-shot confirmation for that action and
target. A wrong password SHALL refuse the action, SHALL count as a sign-in failure for the
throttle, and SHALL NOT end the session.

#### Scenario: Revealing a key with the right password
- **WHEN** a signed-in user confirms a key reveal and supplies their password
- **THEN** the key is revealed in that one response and the action's event records success and the user

#### Scenario: A guarded action with a wrong password
- **WHEN** a guarded action's confirmation is submitted with a wrong password
- **THEN** nothing is revealed, enabled, raised or exported, the refusal is recorded as the action's own event, and the failure counts toward the sign-in throttle for that user

#### Scenario: A guarded action without a password
- **WHEN** a guarded action's confirmation is submitted without a password field
- **THEN** it is refused exactly as a wrong password is

### Requirement: Every request event and every guarded action names the acting user
The system SHALL record, on each request's event and on each guarded action's event, the
username of the signed-in user who made it, or state that the request was unauthenticated when
it was made without a session. Sign-in, sign-in refusal, sign-out and session end by expiry or
re-check SHALL each be recorded as their own event naming the username and the reason.

#### Scenario: A page request by a signed-in user
- **WHEN** a signed-in user's request completes
- **THEN** its event names that user

#### Scenario: A room post
- **WHEN** a signed-in user posts to a room through the interface
- **THEN** the post's guarded-action event names that user

#### Scenario: A session ends by expiry
- **WHEN** a session is found expired or invalidated
- **THEN** one event records the username and whether it was idle expiry, maximum lifetime, or an account change
