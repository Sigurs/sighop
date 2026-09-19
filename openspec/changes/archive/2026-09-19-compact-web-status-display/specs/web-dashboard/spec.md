# Spec Delta

## MODIFIED Requirements

### Requirement: The contact table shows identity, route and signal together
The system SHALL present the known contacts with their name, public key, node hash, node type,
when each was first and last heard, and the learned routes for each — including an empty path
shown as a zero-hop direct route rather than as no route — with the hop count, the signal quality
and when the route was last confirmed. The public key SHALL be shown abbreviated with the full key
copyable, and the route as a hop glyph with its count and path, as the `web-display` conventions
require.

#### Scenario: A zero-hop contact
- **WHEN** a contact's learned route has an empty path
- **THEN** it is displayed as a zero-hop direct route, distinct from a contact with no route at all

#### Scenario: A route found by node hash
- **WHEN** a route was matched by node hash rather than by public key
- **THEN** the display says the match is ambiguous

#### Scenario: A contact's public key
- **WHEN** the contact table is displayed
- **THEN** each public key shows its first three bytes, and its copy control copies the full key

### Requirement: Unverified content is never presented as verified
The system SHALL visually distinguish content whose origin is cryptographically verified from
content that is not, and SHALL NOT present an unverified name, identity or claim in the same form
as a verified one. A contact whose advert signature has been verified SHALL be distinguishable
from one that has not. The marking MAY be a glyph without an accompanying word, provided each
verification state and the unverified-claim marking have distinct glyphs and the meaning of each is
available on hover, to assistive technology, and in a legend on any page that draws them. This is a
display requirement, not a preference, and applies to every view.

#### Scenario: An unverified contact name
- **WHEN** a contact's advert signature has not been verified
- **THEN** its name is drawn distinctly from a verified contact's, and the view says what the distinction means

#### Scenario: Content whose sender is not authenticated
- **WHEN** a view shows a name or claim that the protocol does not authenticate
- **THEN** it is marked as unauthenticated wherever it appears, including in dense tabular views

#### Scenario: Colour is not the only signal
- **WHEN** verification status is conveyed
- **THEN** it is conveyed by more than colour alone

#### Scenario: Hovering a verification mark
- **WHEN** the operator hovers a verified, unverified, key-only or claimed-name mark
- **THEN** a statement of what that mark means is shown
