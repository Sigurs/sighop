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
hex to the clipboard and confirms that it did. Copying SHALL succeed whether or not the page is in
a secure context. Activating the control SHALL NOT change the abbreviation it sits beside, SHALL
NOT change the width of the row or table containing it, and SHALL NOT leave the page altered once
the confirmation has passed. Where no copy can be performed at all, the system SHALL expose the
full key selected for manual copying without displacing the abbreviation, and SHALL return the
display to its abbreviated form once the operator has moved on. A private key the operator has
deliberately revealed, and any field into which a key is entered, SHALL NOT be abbreviated.

#### Scenario: Copying a key
- **WHEN** the operator activates the copy control beside an abbreviated key
- **THEN** the clipboard holds the full 64-character hex key and the control indicates it was copied

#### Scenario: Copying over plain HTTP
- **WHEN** the operator activates the copy control on a panel served without TLS, where the browser reports no secure context
- **THEN** the full key is copied and the abbreviation beside the control still reads three bytes

#### Scenario: The row does not move
- **WHEN** the copy control in a table of keys is activated
- **THEN** no column changes width and no row reflows

#### Scenario: Clipboard not available
- **WHEN** the page is in no context where any copy can be performed
- **THEN** the full key is exposed selected for manual copying without replacing the abbreviation, and the abbreviated display returns once the operator moves on

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

### Requirement: The panel is usable at the width of a phone
The system SHALL render every page of the web interface usable at a viewport 360 CSS pixels wide:
no page SHALL scroll horizontally as a whole, the navigation and the header SHALL wrap rather than
overflow, and a table too wide for the viewport SHALL scroll horizontally inside its own box while
the page around it does not. Nothing SHALL be hidden at a narrow width that is shown at a wide one,
and no control SHALL become unreachable.

#### Scenario: The contact table on a phone
- **WHEN** the contacts page is opened at a viewport 360 pixels wide
- **THEN** the page itself does not scroll sideways, and the contact table scrolls sideways within its own box with every column still reachable

#### Scenario: Navigation at a narrow width
- **WHEN** any page is opened at a viewport 360 pixels wide
- **THEN** every navigation link is visible and reachable, wrapped onto as many lines as it needs

#### Scenario: Nothing is dropped
- **WHEN** a page is compared at 360 pixels and at desktop width
- **THEN** the same statements, statuses and controls are present at both

### Requirement: The panel is drawn in one palette, defined once and legible throughout
The system SHALL draw the web interface in a single dark palette — it sits beside other radio
tooling — and SHALL define that palette in one place as named values, with no rule in the interface
stating a colour of its own. Every colour the palette gives to text SHALL reach a contrast ratio of
at least 4.5:1 against every background the interface places it on. No status, marking, budget
state or direction SHALL be distinguishable by colour alone; each SHALL also carry a glyph, a word
or a shape, so the interface survives a monochrome rendering and a colourblind operator.

#### Scenario: A colour stated outside the palette
- **WHEN** the interface's stylesheet is examined
- **THEN** every colour it uses is one of the palette's named values, and no rule states a colour literally

#### Scenario: Text against its background
- **WHEN** each colour the palette gives to text is measured against each background colour it is placed on
- **THEN** every pair reaches a contrast ratio of at least 4.5:1

#### Scenario: Monochrome rendering
- **WHEN** a page carrying several distinct statuses, markings and directions is rendered without colour
- **THEN** each remains distinguishable by its glyph, word or shape alone
