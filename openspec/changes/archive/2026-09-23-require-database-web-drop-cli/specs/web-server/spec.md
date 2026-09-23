# Spec Delta

## ADDED Requirements

### Requirement: The web interface is always served
The system SHALL serve the web interface whenever the node runs. There SHALL be no option, flag or
setting that starts the node without it, and a failure to serve it SHALL be a startup failure rather
than a node that runs without an interface.

#### Scenario: A node that starts
- **WHEN** the node starts
- **THEN** the interface is listening before the first frame is handled, and the startup output names the address and port it is served on

#### Scenario: The interface cannot be served
- **WHEN** the interface cannot be started for any reason
- **THEN** startup fails, rather than the node continuing to run the radio with no interface

## MODIFIED Requirements

### Requirement: The listening address defaults to loopback and a wider bind is announced
The system SHALL read the interface's listening address, port and additional accepted host names
from the environment, and SHALL default the listening address to a loopback address. It SHALL permit
any other address to be configured, and SHALL, when the configured address is not loopback, report
at startup — in the node's output and as its own logged event — that the interface is reachable
beyond this host, that it is served over plain HTTP, and that passwords and session cookies
therefore cross the network unencrypted unless the operator carries the connection over an encrypted
tunnel. The system SHALL NOT terminate TLS itself and SHALL NOT trust any forwarding header to
establish a client's address or scheme.

#### Scenario: The default
- **WHEN** the node starts with no listening address configured
- **THEN** the interface listens on a loopback address only, and the startup output names the address and port

#### Scenario: A non-loopback bind
- **WHEN** the listening address is configured to a non-loopback address
- **THEN** it is bound, and startup states in its output and in a logged event that the interface is reachable from the network over plain HTTP and that credentials and session cookies are unencrypted in transit

#### Scenario: The warning is not suppressible
- **WHEN** a non-loopback bind is configured
- **THEN** there is no setting that serves that bind without the warning

#### Scenario: Forwarding headers are ignored
- **WHEN** a request carries a header claiming a different client address or scheme
- **THEN** the sign-in throttle, the request's event and the cookie's attributes use the connection's own address and scheme

## REMOVED Requirements

### Requirement: The web interface is opt-in and off by default
**Reason**: The interface is the only surface through which the platform is administered now that the
command line is gone, so a node without one cannot be administered at all.
**Migration**: Replaced by "The web interface is always served" above. Deployments passing `--web`
drop it; the address and port come from the environment.
