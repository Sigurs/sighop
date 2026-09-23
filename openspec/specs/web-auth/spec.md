# web-auth Specification

## Purpose

Who may use the web interface and how that is established: operator accounts, signing in and
out, sessions and their expiry, resistance to password guessing, and the acting user every
state-changing and guarded action is attributed to. The interface can transmit and reveal the
private keys that are the platform's identities, so nothing it serves is reachable without it.

## Requirements
### Requirement: Every part of the interface requires a signed-in user unless explicitly public
The system SHALL refuse every page, form submission, partial and live-feed connection to a
request that does not carry a valid session, except for an explicit, fixed set of public
routes consisting only of the sign-in form, its submission, the first-run setup form, its
submission, and static assets that carry no platform state. A route SHALL be public only by
being named in that set, so that a route added later is protected without anyone remembering to
protect it. There SHALL be no configuration, command-line option or environment variable that
serves the interface without authentication, whatever address it is bound to. While first-run
setup is pending, a request refused for want of a session SHALL be sent to the setup form rather
than the sign-in form; otherwise it SHALL be sent to the sign-in form.

#### Scenario: An unauthenticated page request
- **WHEN** a request without a valid session asks for any page that is not public
- **THEN** the browser is sent to the sign-in form, or to the setup form while first-run setup is pending, and no platform state is present in the response

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

#### Scenario: The setup form carries no state
- **WHEN** the setup form is requested without a session
- **THEN** it contains no platform state, key material or setup code

### Requirement: Operator accounts are durable, hashed, and created only by first-run setup
The system SHALL store operator accounts in the database, each with a unique username, a password
stored only as a salted memory-hard hash whose parameters travel with it, an enabled state, the time
its password was last set, and the account's default chat identity where one is set. Usernames SHALL
be compared case-insensitively and stored in one normalised form. The system SHALL NOT store, log or
render a password or its hash anywhere else.

An account SHALL come to exist in exactly one way: through first-run setup, once, while the database
holds no account at all. The system SHALL offer no other way to create, list, re-password, disable,
re-enable or remove an account. An account's default chat identity is an interface preference rather
than an account operation: it carries no privilege, it SHALL be settable and clearable by the
signed-in operator it belongs to and by no one else, and an operator SHALL NOT be able to read or
change another account's preference.

#### Scenario: The first account is created
- **WHEN** first-run setup creates an account with a username and a password
- **THEN** the account is stored with a hash of the password and never the password itself, and it is enabled, with no default chat identity

#### Scenario: No second account can be created
- **WHEN** an account already exists
- **THEN** no surface offers to create, re-password, disable or remove an account, and first-run setup is closed

#### Scenario: A preference belongs to one account
- **WHEN** an operator sets their default chat identity
- **THEN** it is stored against their own account only, and no other account's preference is read or changed

### Requirement: First-run setup creates the first account with a one-time code
The system SHALL, when the interface is served against a database holding no account at all,
offer first-run setup: a form taking a setup code, a username and a new password entered twice.
The setup code SHALL be generated randomly by the serving process at startup with at least 100
bits of entropy, SHALL be shown only in the run's own human-readable output, and SHALL NOT be
written to any structured event, rendered in any page, or stored. It SHALL be accepted without
regard to letter case or to separator and space characters, and SHALL be compared in constant
time. A submission SHALL be checked in this order: the setup code first, and a wrong or missing
code SHALL be refused with one fixed response that says nothing about the username or password,
without hashing any password; then the username's validity and the two password entries, a
refusal of which SHALL name what is wrong and leave the code usable. A submission that passes
every check SHALL create the account enabled, issue that browser a new session exactly as a
successful sign-in does, and close setup. The password SHALL be hashed off the event loop that
carries the radio, bounded like sign-in verification.

#### Scenario: Setup with the right code
- **WHEN** the setup form is submitted with the code from the run's output, a valid username and two matching non-empty passwords
- **THEN** the account is created enabled, the browser is signed in as it and sent to the overview, and one event records the username and a success outcome

#### Scenario: Setup with a wrong code
- **WHEN** the setup form is submitted with a code that is not the process's code
- **THEN** no account is created, no password is hashed, the form is shown again with the same message whatever else was submitted, the username is not echoed, and the refusal is recorded without the submitted code or password

#### Scenario: The code typed differently
- **WHEN** the correct code is submitted in lower case, without its separators, or with surrounding spaces
- **THEN** it is accepted

#### Scenario: The passwords differ or are empty
- **WHEN** the right code is submitted with two password entries that differ, or with an empty password
- **THEN** no account is created, the form states that the passwords differ or that a password cannot be empty, and the same code still works on the next submission

#### Scenario: An invalid username
- **WHEN** the right code is submitted with a username the account rules refuse
- **THEN** no account is created, the form states why the username is refused, and the same code still works

#### Scenario: The code never leaves the terminal output
- **WHEN** a run serving first-run setup is started and the setup form is used, successfully or not
- **THEN** no structured event, page or database row contains the setup code

#### Scenario: A restart
- **WHEN** a run serving first-run setup is restarted before setup completes
- **THEN** the previous code is refused and a new code is shown in the new run's output

### Requirement: First-run setup closes once any account exists and creates at most one account
The system SHALL close first-run setup, retiring its code, as soon as any account exists, whether
created by setup or through the command line while the run is serving setup. Once closed, a
request for the setup form SHALL send the browser to the sign-in form, and a setup submission
SHALL be refused without creating anything. Creating the first account SHALL be atomic with
checking that no account exists, so that concurrent setup submissions, or a setup submission
racing an account created through the command line, result in no setup-created account beside
any other account. The system SHALL NOT offer first-run setup when the database holds accounts
that are all disabled.

#### Scenario: Setup after it has completed
- **WHEN** the setup form is requested or submitted after first-run setup created an account
- **THEN** the form request is sent to the sign-in form, a submission creates nothing even with the old code, and the refusal is recorded

#### Scenario: An account added from the terminal during setup
- **WHEN** an account is added through the command line while a run is serving first-run setup
- **THEN** the next request for the setup form is sent to the sign-in form, and a setup submission with the code creates nothing

#### Scenario: Two setup submissions at once
- **WHEN** two valid setup submissions with different usernames arrive concurrently
- **THEN** exactly one account exists afterwards, and the other submission is refused stating that setup has already been completed

#### Scenario: Only disabled accounts exist
- **WHEN** the setup form is requested against a database whose accounts are all disabled
- **THEN** no setup form is offered and no account can be created through the browser

#### Scenario: Signing in during setup
- **WHEN** a sign-in is submitted while first-run setup is pending
- **THEN** it fails exactly as a sign-in for an unknown username does

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
