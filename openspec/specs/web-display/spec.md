# web-display Specification

## Purpose

Shared display conventions for the web interface: how status, public keys, timestamps and durations
are drawn compactly in dense views while their full meaning and exact values stay one hover away.

## Requirements

### Requirement: A status is drawn as a glyph with its essential numbers and its meaning on hover
The system SHALL draw a message, delivery, reception or verification status in the web interface as
a glyph followed only by the numbers that distinguish one occurrence from another (attempts,
latency, hop count, repeats heard). The full statement of what the status means SHALL be available
on pointer hover and to assistive technology on the same element. A status whose explanation is a
reason specific to that occurrence (a refusal or failure reason) SHALL keep the reason visible, not
only on hover. Each distinct status SHALL use a distinct glyph, so the distinction never rests on
colour alone.

#### Scenario: Hovering a status glyph
- **WHEN** the operator hovers a status glyph
- **THEN** the full explanatory statement for that status is shown

#### Scenario: A screen reader reads a status
- **WHEN** assistive technology reads a status cell
- **THEN** it reads the explanatory statement, not the bare glyph

#### Scenario: A failure keeps its reason visible
- **WHEN** a channel post was not transmitted for a stated reason
- **THEN** the row shows the not-transmitted glyph and the reason as visible text

#### Scenario: Monochrome rendering
- **WHEN** the page is viewed without colour
- **THEN** every status remains distinguishable by its glyph

### Requirement: A public key is shown abbreviated with the full key copyable
The system SHALL show a public key in the web interface as its first three bytes in hex, SHALL make
the full key available on hover, and SHALL provide a control beside it that copies the full key in
hex to the clipboard and confirms that it did. Where the clipboard is unavailable the control SHALL
instead select the full key as text so it can be copied by hand. A private key the operator has
deliberately revealed, and any field into which a key is entered, SHALL NOT be abbreviated.

#### Scenario: Copying a key
- **WHEN** the operator activates the copy control beside an abbreviated key
- **THEN** the clipboard holds the full 64-character hex key and the control indicates it was copied

#### Scenario: Clipboard not available
- **WHEN** the page is not in a context where the clipboard can be written
- **THEN** activating the control exposes the full key selected for manual copying

#### Scenario: A revealed private key
- **WHEN** the operator reveals an identity's private key
- **THEN** it is shown in full, not abbreviated

### Requirement: A timestamp is shown relative to now with its exact value on hover
The system SHALL show a timestamp in the web interface relative to the present (for example
"just now", "3 min ago", "2 h ago", "4 d ago"), SHALL keep that text current while the page stays
open, including in content refreshed in place, and SHALL make the exact time available on hover as
both the viewer's local date and time and the exact UTC value. A timestamp in the future SHALL read
as such ("in 5 min"). Where the page's script does not run, the system SHALL show a compact UTC time
marked as UTC instead.

#### Scenario: A message received a few minutes ago
- **WHEN** a message received three minutes ago is displayed
- **THEN** its time reads "3 min ago" and hovering shows the local date-time and the exact UTC timestamp

#### Scenario: The page stays open
- **WHEN** the page is left open for several minutes
- **THEN** relative times advance without a reload

#### Scenario: A scheduled flood
- **WHEN** an identity's next flood is scheduled five minutes ahead
- **THEN** its time reads "in 5 min"

#### Scenario: No script
- **WHEN** the page's script does not run
- **THEN** each timestamp shows a compact UTC time marked UTC, with the exact value on hover

### Requirement: Durations and counts read as a person would write them
The system SHALL show durations in the web interface in the largest sensible unit with at most one
decimal ("3.0 s" rather than "3010 ms", "1 m 30 s" rather than "90 s"), and SHALL show counted nouns
with their correct singular or plural form rather than a "(s)" suffix.

#### Scenario: An acknowledgement latency
- **WHEN** a message was acknowledged after 3010 ms
- **THEN** its latency is shown as "3.0 s"

#### Scenario: A single attempt
- **WHEN** a statement counts one attempt
- **THEN** it reads "1 attempt", not "1 attempt(s)"
