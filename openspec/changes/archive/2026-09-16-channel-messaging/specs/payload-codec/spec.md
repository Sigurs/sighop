## ADDED Requirements

### Requirement: Group text body building
The system SHALL build a group text body from a timestamp, a sender name and a message body as the
plain-text-message layout with text type plain, attempt zero, and text `<sender name>: <body>`, and
SHALL refuse to build one whose sender name contains `": "`, because a receiver splits on the first
occurrence.

#### Scenario: Build round-trips a parse
- **WHEN** a group text body is built with sender `dev-companion` and body `hello: world` and then parsed
- **THEN** the parse reports sender name `dev-companion`, body `hello: world`, text type plain and attempt zero

#### Scenario: A sender name containing the separator
- **WHEN** a group text body is built with sender name `a: b`
- **THEN** building fails naming the separator

#### Scenario: Corpus group text rebuilds
- **WHEN** a decrypted plain group text body from the capture corpus is parsed and rebuilt from its timestamp, sender name and body
- **THEN** the rebuilt bytes equal the decrypted bytes with their zero padding removed
