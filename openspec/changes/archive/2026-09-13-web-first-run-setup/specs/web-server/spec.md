## MODIFIED Requirements

### Requirement: A state-changing request must originate from the interface itself
The system SHALL reject any state-changing request that it cannot attribute to a page it served.
It SHALL require a token present only in served pages — bound to the requester's session once
one exists, and issued by this process for the sign-in and first-run setup forms before one does
— SHALL reject a request whose declared host is not one the interface was configured to serve,
and SHALL NOT perform any state change in response to a request a browser could issue by
navigation, prefetching or reloading. This holds alongside authentication rather than being
replaced by it: a page on another origin can reach a loopback port and ride a signed-in browser,
and name resolution can point a hostname at one.

#### Scenario: A request from another origin
- **WHEN** a state-changing request arrives without the token for its session
- **THEN** it is rejected, nothing changes, and the rejection is recorded in the request's event

#### Scenario: A token from a different session
- **WHEN** a state-changing request carries a token that was issued into another session's pages
- **THEN** it is rejected exactly as a request with no token is

#### Scenario: A sign-in submitted from another origin
- **WHEN** a sign-in submission arrives without the token this process placed in its sign-in form
- **THEN** it is rejected before any password is verified

#### Scenario: A setup submission from another origin
- **WHEN** a first-run setup submission arrives without the token this process placed in its setup form
- **THEN** it is rejected before the setup code is checked or any password is hashed, and nothing is created

#### Scenario: A request for an unexpected host
- **WHEN** a request declares a host that is not one the interface was configured to serve
- **THEN** it is rejected before any handler runs

#### Scenario: A safe method never changes state
- **WHEN** any request is made with a method that browsers treat as safe
- **THEN** no state changes, nothing is transmitted, and no key material is revealed
