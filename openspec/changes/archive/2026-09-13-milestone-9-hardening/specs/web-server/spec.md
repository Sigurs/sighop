## MODIFIED Requirements

### Requirement: The listening address defaults to loopback and a wider bind is announced
The system SHALL default the interface's listening address to a loopback address. It SHALL permit
an operator to configure any other address, and SHALL, when the configured address is not
loopback, report at startup — in the run's output and as its own logged event — that the interface
is reachable beyond this host, that it is served over plain HTTP, and that passwords and session
cookies therefore cross the network unencrypted unless the operator carries the connection over
an encrypted tunnel. The system SHALL NOT terminate TLS itself and SHALL NOT trust any
forwarding header to establish a client's address or scheme.

#### Scenario: The default
- **WHEN** the interface is enabled without an address
- **THEN** it listens on a loopback address only, and the startup output names the address and port

#### Scenario: A non-loopback bind
- **WHEN** the interface is configured to listen on a non-loopback address
- **THEN** it does so, and startup states in its output and in a logged event that the interface is reachable from the network over plain HTTP and that credentials and session cookies are unencrypted in transit

#### Scenario: The warning is not suppressible
- **WHEN** a non-loopback bind is configured
- **THEN** there is no option that serves that bind without the warning

#### Scenario: Forwarding headers are ignored
- **WHEN** a request carries a header claiming a different client address or scheme
- **THEN** the sign-in throttle, the request's event and the cookie's attributes use the connection's own address and scheme

### Requirement: A state-changing request must originate from the interface itself
The system SHALL reject any state-changing request that it cannot attribute to a page it served.
It SHALL require a token present only in served pages — bound to the requester's session once
one exists, and issued by this process for the sign-in form before one does — SHALL reject a
request whose declared host is not one the interface was configured to serve, and SHALL NOT
perform any state change in response to a request a browser could issue by navigation,
prefetching or reloading. This holds alongside authentication rather than being replaced by it:
a page on another origin can reach a loopback port and ride a signed-in browser, and name
resolution can point a hostname at one.

#### Scenario: A request from another origin
- **WHEN** a state-changing request arrives without the token for its session
- **THEN** it is rejected, nothing changes, and the rejection is recorded in the request's event

#### Scenario: A token from a different session
- **WHEN** a state-changing request carries a token that was issued into another session's pages
- **THEN** it is rejected exactly as a request with no token is

#### Scenario: A sign-in submitted from another origin
- **WHEN** a sign-in submission arrives without the token this process placed in its sign-in form
- **THEN** it is rejected before any password is verified

#### Scenario: A request for an unexpected host
- **WHEN** a request declares a host that is not one the interface was configured to serve
- **THEN** it is rejected before any handler runs

#### Scenario: A safe method never changes state
- **WHEN** any request is made with a method that browsers treat as safe
- **THEN** no state changes, nothing is transmitted, and no key material is revealed

### Requirement: The interface degrades rather than fails when state is unavailable
The system SHALL serve the interface when a configured database is degraded, presenting the parts
that are available and stating plainly which parts are not and why. A page whose content is
entirely durable state SHALL say that it is unavailable rather than render empty, so that "no
rooms" and "cannot read rooms" are never the same screen. The system SHALL NOT serve the
interface on a run with no database configured, because the accounts that authenticate it are
durable state.

#### Scenario: No database configured
- **WHEN** the interface is requested on a run with no database configured
- **THEN** startup fails before any traffic is processed, stating that the web interface requires a database for its accounts, and no port is listened on

#### Scenario: The database is degraded
- **WHEN** the database is unreachable while the interface is being used
- **THEN** the affected sections say so, name the degraded state the run reports, and offer no write action that would be silently discarded

#### Scenario: Empty is distinguishable from unavailable
- **WHEN** a durable collection is readable and contains nothing
- **THEN** the page says it is empty, in wording distinct from the wording used when it cannot be read

## ADDED Requirements

### Requirement: Host names beyond the bind address can be allowed explicitly
The system SHALL let an operator name additional host names, with or without a port, that the
interface answers to, in addition to those implied by the bound address, and SHALL continue to
refuse every other declared host. A wildcard or empty entry SHALL be refused at startup.

#### Scenario: A panel bound to all addresses inside a container
- **WHEN** the interface is bound to an all-addresses address and `localhost:8080` is named as an allowed host
- **THEN** a request declaring `localhost:8080` is served and a request declaring any unnamed host is refused before any handler runs

#### Scenario: A wildcard allowed host
- **WHEN** an allowed host of `*` or an empty value is configured
- **THEN** startup fails naming the value, and no port is listened on
