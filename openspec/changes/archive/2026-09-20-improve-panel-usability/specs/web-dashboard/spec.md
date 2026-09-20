# Spec Delta

## MODIFIED Requirements

### Requirement: Navigation names each concern once
The system SHALL offer, on every page, navigation to exactly these pages: overview, contacts, chat,
rooms, identities, webhooks and system. Each concern SHALL be presented on one of them rather than
split across two: rooms are read and configured on the rooms page, channels are read and
administered from chat, a bot is configured on its identity's page, and the board's readback and
the schema revision are on the system page. The former pages for modem health, radio, schema, room
configuration, bots and channels SHALL NOT be served. The navigation SHALL mark which of those
pages is being viewed, both visibly by something other than colour alone and to assistive
technology, including on a page reached beneath one of them.

#### Scenario: The navigation
- **WHEN** any page is opened by a signed-in operator
- **THEN** its navigation links to overview, contacts, chat, rooms, identities, webhooks and system, and to nothing else

#### Scenario: A former page
- **WHEN** a former page for modem health, radio, schema, room configuration, bots or channels is requested
- **THEN** it is not found, and no page links to it

#### Scenario: The page being viewed
- **WHEN** the contacts page is opened
- **THEN** its navigation marks contacts as the current page, distinguishably without colour and to assistive technology, and marks no other

#### Scenario: A page beneath a navigation entry
- **WHEN** a conversation under chat is opened
- **THEN** the navigation marks chat as the current page
