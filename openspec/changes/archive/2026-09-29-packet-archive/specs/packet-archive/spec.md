# Spec Delta

## Purpose

A durable, append-only archive of every frame the station received or resolved for transmission,
with its wire bytes. Features added later can backfill from past traffic by replaying it, instead
of starting empty on the day they ship.

## ADDED Requirements

### Requirement: Every reception is archived with its wire bytes
The system SHALL archive, for every reception the modem delivers, a record carrying the frame's
exact wire bytes, its reception time, the SNR and RSSI where the modem reported them, the
reception's packet identifier, and whether the modem could frame it. This SHALL include duplicate
receptions, frames that failed to decode, and bytes the modem could not frame at all. For bytes
it could not frame, the modem's reason SHALL be kept alongside them.

#### Scenario: A decodable frame is received
- **WHEN** a frame is received and decoded
- **THEN** an archive record holds its exact wire bytes, reception time, SNR, RSSI and packet identifier

#### Scenario: A duplicate reception
- **WHEN** a frame is received that duplicates one received moments earlier
- **THEN** it is archived as a record of its own with its own packet identifier and signal measurements

#### Scenario: A frame that fails to decode
- **WHEN** a framed reception cannot be decoded as a packet
- **THEN** it is archived with its wire bytes exactly as received

#### Scenario: Bytes the modem could not frame
- **WHEN** the modem delivers bytes it could not frame, with a reason
- **THEN** they are archived with that reason and marked as unframed

#### Scenario: Correlating with the feed
- **WHEN** an archived reception also has a packet-log row
- **THEN** both carry the same packet identifier

### Requirement: Every resolved transmission is archived with its packet bytes
The system SHALL archive, for every transmission outcome, a record carrying the packet bytes
offered for transmission, the time it resolved, the outcome, the computed airtime, the originating
identity where it is a stored one, the priority class, and the packet identifier. Transmissions
that never reached the air (suppressed, dropped or expired) SHALL be archived too, with their
outcome.

#### Scenario: A packet is sent
- **WHEN** a transmission completes on air
- **THEN** an archive record holds the packet bytes, outcome, airtime, originating identity and priority class

#### Scenario: A transmission is suppressed
- **WHEN** a transmission is suppressed because transmit is disabled
- **THEN** it is archived with its packet bytes and its suppressed outcome

### Requirement: The archive is append-only
The system SHALL NOT modify an archived record after it is written, and SHALL NOT delete archived
records except when retention removes them. The archive SHALL NOT be read to decide protocol
behaviour, detect duplicates, or answer anything on the reception, dispatch or transmit path.

#### Scenario: A later reception of the same content
- **WHEN** a frame whose content matches an archived record is received again
- **THEN** a new record is archived and the earlier record is unchanged

#### Scenario: Duplicate detection
- **WHEN** a duplicate reception arrives
- **THEN** duplicate detection does not consult the archive

### Requirement: Archiving never delays or fails a packet, and every lost record is counted
The system SHALL write archive records off the path that decodes, dispatches and schedules
packets, through a bounded buffer. A slow or unreachable database SHALL NOT delay a reception,
dispatch or transmission. A record that cannot be buffered or written SHALL be discarded. Every
discarded record SHALL be counted, and the count SHALL appear in periodic status output and on
the system page, so a gap in the archive is visible as a gap.

#### Scenario: The database is unreachable
- **WHEN** the database is unreachable while frames are received
- **THEN** frames are still received, decoded and dispatched, and archive records beyond the buffer are discarded and counted

#### Scenario: The buffer overflows
- **WHEN** more records are produced than can be written and the buffer is full
- **THEN** records are discarded, the discard count increases by the number discarded, and status output reports it

#### Scenario: No records discarded
- **WHEN** every produced record was written
- **THEN** status output reports an archive discard count of zero rather than omitting it

#### Scenario: A replay run
- **WHEN** the runtime replays a capture instead of listening to a modem
- **THEN** nothing is written to the archive

### Requirement: The archive is partitioned by month and partitions exist before they are needed
The system SHALL store the archive in partitions by calendar month (UTC) of the record's time. It
SHALL ensure, at startup and at least daily, that the partitions for the current month and the
next month exist. A record whose month has no partition SHALL be discarded and counted like any
other failed write, and SHALL NOT fail or delay the packet it describes.

#### Scenario: Startup in a new month
- **WHEN** the runtime starts and the current month has no partition
- **THEN** the current and next months' partitions are created before archive records are written

#### Scenario: Crossing a month boundary
- **WHEN** the runtime runs past midnight UTC at the end of a month
- **THEN** records after midnight are written to the new month's partition, which already existed

#### Scenario: Partition maintenance fails
- **WHEN** creating a partition fails
- **THEN** the failure is reported, the next attempt runs at the normal interval, and the maintainer keeps running

### Requirement: Retention keeps everything by default and otherwise removes whole months
The system SHALL keep archived records forever unless an operator sets an age bound in whole days.
With a bound set, the system SHALL periodically remove every month's partition whose whole month
is older than the bound, and SHALL NOT delete individual rows. Records newer than the bound SHALL
always be kept. Records may be kept for up to one month longer than the bound. Each removal SHALL
be reported with the month removed.

#### Scenario: Default retention
- **WHEN** no age bound has been set
- **THEN** no archived record is ever removed

#### Scenario: A bound is set
- **WHEN** the bound is 90 days and a month's partition ended more than 90 days ago
- **THEN** that partition is removed and the removal is reported with its month

#### Scenario: A month straddling the bound
- **WHEN** the bound falls in the middle of a month
- **THEN** that month's partition is kept whole

### Requirement: The archive can be read back in order for a time range
The system SHALL offer a way to read archived records for a time range in the order they were
recorded (time, then insertion order). It SHALL stream them in bounded batches rather than loading
the range into memory, and each record SHALL come back with exactly the bytes and fields it was
written with.

#### Scenario: Reading a range
- **WHEN** records for a range spanning two months are read
- **THEN** every record in the range is returned, oldest first, with bytes identical to what was archived

#### Scenario: A large range
- **WHEN** a range holding more records than one batch is read
- **THEN** records are delivered batch by batch without the whole range being held in memory

### Requirement: The archive exports as a capture file
The system SHALL export a time range of the archive as a capture-format JSONL file that the
existing capture replay reads without changes. The first line SHALL be a header record stating
that the file came from the archive, the range requested, the export time, and the sighop version
and commit. Receptions SHALL be written as received-frame records with signal measurements,
unframed bytes as unparsed records with their reason, and transmissions that reached the air as
transmitted-frame records. Transmissions that never reached the air SHALL be left out.

#### Scenario: Exporting and replaying
- **WHEN** a range is exported and the file is replayed
- **THEN** the replay yields every archived reception in the range, in order, with identical bytes and signal measurements

#### Scenario: The header states provenance
- **WHEN** an export is opened
- **THEN** its first line is a header record saying it was exported from the archive, with the range, the export time and the sighop version and commit

#### Scenario: Suppressed transmissions
- **WHEN** the range holds a suppressed transmission
- **THEN** the export does not contain it
