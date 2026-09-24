# Spec Delta

## ADDED Requirements

### Requirement: Repeater statistics body parsing
The system SHALL parse the statistics body a repeater returns for a status request in the layout
the reference repeater implementation defines — an echoed 4-byte request timestamp followed by
little-endian fields: battery millivolts, transmit queue length, noise floor, last RSSI (16-bit),
packets received, packets sent, transmit airtime seconds, uptime seconds, flood sent, direct sent,
flood received, direct received (32-bit), error flags, last SNR in quarter-decibels, direct
duplicates, flood duplicates (16-bit), receive airtime seconds and receive errors (32-bit) — and
SHALL keep it distinct from the room server statistics body, whose last four bytes carry different
counters. A body that ends after the flood duplicates field SHALL parse with receive airtime and
receive errors absent. A body shorter than that, or one ending partway through a field, SHALL be a
decode failure. Signed fields SHALL be read as signed.

#### Scenario: A full repeater status body
- **WHEN** a sixty-byte repeater status body is parsed
- **THEN** it yields the echoed timestamp and every field at its reference offset, with the SNR divided by four

#### Scenario: An older repeater without receive counters
- **WHEN** a fifty-two-byte repeater status body is parsed
- **THEN** it yields every field up to flood duplicates, with receive airtime and receive errors absent

#### Scenario: A truncated body
- **WHEN** a status body ends partway through the uptime field
- **THEN** parsing fails with a truncation failure rather than yielding partial values

#### Scenario: A negative noise floor
- **WHEN** the noise floor bytes encode -110
- **THEN** it is parsed as -110, not as a large unsigned value

### Requirement: Repeater login body building and the login answer as a client reads it
The system SHALL build a repeater login body as a 4-byte little-endian timestamp followed by the
password bytes, with no sync timestamp, so that an empty password leaves the byte after the
timestamp zero once the body is padded. The system SHALL read a repeater's login answer with the
same layout as the room server login response, and SHALL accept an answer of twelve bytes — from
firmware that predates the protocol-level byte — with the protocol level absent.

#### Scenario: A blank-password login body
- **WHEN** a repeater login body with timestamp 1700000000 and an empty password is built
- **THEN** it is exactly the four timestamp bytes, and the byte following them after padding is zero

#### Scenario: A twelve-byte login answer
- **WHEN** a login answer of twelve bytes is read as a client
- **THEN** it yields the server timestamp, result, permissions and blob, with the protocol level absent

### Requirement: Neighbour list request and response
The system SHALL build a neighbour list request as the request type, a version byte of zero, the
number of entries wanted, a 16-bit little-endian offset, an ordering byte, the key prefix length
wanted, and four random bytes; and SHALL parse its response as the echoed 4-byte request timestamp,
a 16-bit total neighbour count, a 16-bit count of entries in this answer, then that many entries
each of the requested prefix length followed by a 32-bit seconds-since-heard and a signed 8-bit SNR
in quarter-decibels. A response whose entries do not fit its length SHALL be a decode failure.

#### Scenario: Building a request
- **WHEN** a neighbour request for 11 entries at offset 22, newest first, with a 6-byte prefix is built
- **THEN** its bytes are the request type, 0, 11, 22 as two little-endian bytes, 0, 6, and four random bytes

#### Scenario: Parsing a page
- **WHEN** a response reporting 25 neighbours and 2 entries with 6-byte prefixes is parsed
- **THEN** it yields the total 25 and two entries, each with its prefix, seconds since heard and SNR in decibels

#### Scenario: A lying entry count
- **WHEN** a response claims 3 entries but carries bytes for 2
- **THEN** parsing fails rather than yielding a partial entry
