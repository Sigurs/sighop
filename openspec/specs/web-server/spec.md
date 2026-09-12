# web-server Specification

## Purpose

The web interface's existence and shape: where it runs relative to the radio, what address it
binds, how an operator opts into it, what it says about itself at startup, and how every page
behaves when the state behind it is partly or wholly unavailable. It is the surface through which
a human sees and drives the platform, and until authentication arrives it is also the surface
whose exposure has to be deliberate.

## Requirements

### Requirement: The web interface runs inside the process that owns the radio
The system SHALL serve the web interface from within the same process and event loop as the
running platform, so that it reads live scheduler, budget, bus and messenger state directly rather
than through the database. The web interface SHALL NOT be startable as a separate process against
the same modem, and serving it SHALL NOT replace, wrap or reconfigure the event loop the radio
runs on.

#### Scenario: The panel reports live state the database does not hold
- **WHEN** the interface displays queue depth, remaining airtime budget or modem health
- **THEN** those values come from the running platform's own state, and are current as of the request

#### Scenario: The web server stops with the run
- **WHEN** the run is asked to stop
- **THEN** the web server stops with it, in-flight requests are allowed to finish within the shutdown bound, and no listener outlives the process

#### Scenario: Serving the interface does not disturb the radio's loop
- **WHEN** the web interface is enabled
- **THEN** the event loop policy, the loop implementation and the process's signal handling are unchanged from a run without it

### Requirement: The web interface is opt-in and off by default
The system SHALL NOT serve a web interface unless an operator asks for one. A run that is not
asked SHALL behave in every respect as a run of a build without the interface.

#### Scenario: A run that does not ask for the interface
- **WHEN** the platform runs without the web option
- **THEN** no socket is listened on, no web task is started, and the run's output is unchanged

### Requirement: The listening address defaults to loopback and a wider bind is announced
The system SHALL default the interface's listening address to a loopback address. It SHALL permit
an operator to configure any other address, and SHALL, when the configured address is not
loopback, report at startup — in the run's output and as its own logged event — that the interface
is reachable beyond this host, that this build has no authentication, and that anything able to
reach the port can transmit and reveal private key material.

#### Scenario: The default
- **WHEN** the interface is enabled without an address
- **THEN** it listens on a loopback address only, and the startup output names the address and port

#### Scenario: A non-loopback bind
- **WHEN** the interface is configured to listen on a non-loopback address
- **THEN** it does so, and startup states in its output and in a logged event that the interface is unauthenticated and reachable from the network

#### Scenario: The warning is not suppressible
- **WHEN** a non-loopback bind is configured
- **THEN** there is no option that serves that bind without the warning

### Requirement: A request that cannot bind is a startup failure
The system SHALL treat a web interface that cannot listen on the configured address and port as a
startup failure, reported before the platform begins processing traffic, in the same way a
configured database that cannot be reached is.

#### Scenario: The port is already in use
- **WHEN** the configured port cannot be bound
- **THEN** startup fails, names the address and port and the reason, and the run does not continue with a silently absent interface

### Requirement: Every completed request emits one wide event
The system SHALL emit exactly one structured event per completed HTTP request and per closed
WebSocket connection, carrying the shared context every event carries plus the method, the route,
the outcome, the response status and the duration. A WebSocket's event SHALL additionally carry
how many records were delivered and how many were dropped for that connection.

#### Scenario: A page is served
- **WHEN** an HTTP request completes
- **THEN** one event is emitted naming the route, status, outcome and duration, and no unstructured log line is written for it

#### Scenario: A feed connection closes
- **WHEN** a WebSocket connection closes for any reason
- **THEN** one event is emitted carrying the connection's duration, delivered count and dropped count

#### Scenario: A request raises
- **WHEN** a request handler raises
- **THEN** the event records the failure outcome, the response is a generic error page, and no traceback or internal path is sent to the browser

### Requirement: A state-changing request must originate from the interface itself
The system SHALL reject any state-changing request that it cannot attribute to a page it served.
It SHALL require a token issued by this process and present only in served pages, SHALL reject a
request whose declared host is not an address the interface was configured to serve, and SHALL
NOT perform any state change in response to a request a browser could issue by navigation,
prefetching or reloading. This holds while there is no authentication precisely because there is
none: a page on another origin can reach a loopback port, and name resolution can point a hostname
at one.

#### Scenario: A request from another origin
- **WHEN** a state-changing request arrives without the token this process issued
- **THEN** it is rejected, nothing changes, and the rejection is recorded in the request's event

#### Scenario: A request for an unexpected host
- **WHEN** a request declares a host that is not one the interface was configured to serve
- **THEN** it is rejected before any handler runs

#### Scenario: A safe method never changes state
- **WHEN** any request is made with a method that browsers treat as safe
- **THEN** no state changes, nothing is transmitted, and no key material is revealed

### Requirement: The interface degrades rather than fails when state is unavailable
The system SHALL serve the interface when no database is configured and when a configured database
is degraded, presenting the parts that are available and stating plainly which parts are not and
why. A page whose content is entirely durable state SHALL say that it is unavailable rather than
render empty, so that "no rooms" and "cannot read rooms" are never the same screen.

#### Scenario: No database configured
- **WHEN** the interface is served by a run with no database
- **THEN** live state, contacts and the packet feed are shown, and the sections backed by the database state that no database is configured

#### Scenario: The database is degraded
- **WHEN** the database is unreachable while the interface is being used
- **THEN** the affected sections say so, name the degraded state the run reports, and offer no write action that would be silently discarded

#### Scenario: Empty is distinguishable from unavailable
- **WHEN** a durable collection is readable and contains nothing
- **THEN** the page says it is empty, in wording distinct from the wording used when it cannot be read

### Requirement: The interface is served without a build step or third-party runtime fetches
The system SHALL serve its pages, scripts, styles and fonts from the application itself, with no
request from the browser to any external origin, and SHALL require no asset compilation step to
build or run.

#### Scenario: The page loads with no external network
- **WHEN** a browser with no route off the host loads any page
- **THEN** every asset the page needs resolves from the application and the page is fully functional

#### Scenario: Building the application
- **WHEN** the project is built or installed
- **THEN** no asset bundler or JavaScript toolchain is required
