## MODIFIED Requirements

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

### Requirement: Operator accounts are durable, hashed, and managed from the terminal
The system SHALL store operator accounts in the database, each with a unique username, a
password stored only as a salted memory-hard hash whose parameters travel with it, an enabled
state, and the time its password was last set. Usernames SHALL be compared case-insensitively
and stored in one normalised form. The system SHALL NOT store, log or render a password or its
hash anywhere else. Accounts SHALL be created, listed, re-passworded, disabled, re-enabled and
removed through the command line, with one exception: when the database holds no account at all,
the first account MAY be created once through first-run setup in the browser. No other account
operation SHALL be offered in the browser.

#### Scenario: An account is created
- **WHEN** an operator creates an account with a username and a password
- **THEN** the account is stored with a hash of the password and never the password itself, and it is enabled

#### Scenario: A username that differs only in case
- **WHEN** an account is created whose username differs from an existing one only in letter case
- **THEN** creation is refused, naming the existing account

#### Scenario: Listing accounts
- **WHEN** accounts are listed
- **THEN** each shows its username, whether it is enabled, when it was created and when its password was last set, and no hash is shown

#### Scenario: A setup-created account is an ordinary account
- **WHEN** the first account has been created through first-run setup
- **THEN** it is stored, listed, re-passworded, disabled and removed from the command line exactly as an account created there

## ADDED Requirements

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
