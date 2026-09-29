# Proposal

## Why

The only record of what the radio heard is `packet_log`: a 100 000-row ring buffer that keeps a
frame's bytes only when it failed to decode. When a new feature needs history (who relayed what,
which nodes were heard, how a path changed), sighop can only start collecting from the day it
ships. A permanent archive of every frame's wire bytes lets a new feature be backfilled by
replaying past traffic through the same decoder that live traffic goes through.

## What Changes

- New append-only `packet_archive` table, range-partitioned by month on reception time. For every
  reception, including KISS-unparsed and undecodable frames and duplicates, it stores the raw wire
  bytes, time, SNR/RSSI and the reception's `packet_id`. For every resolved transmission, including
  suppressed, dropped and expired ones, it stores the packet bytes, outcome, airtime, originating
  entity and priority class.
- Archive rows are written off the reception path through their own bounded write-behind queue,
  like `packet_log`. Rows the queue cannot hold, or that a failed write loses, are **discarded and
  counted**, and the count shows in status output and on the system page. The archive is complete
  whenever the database is healthy, and the gaps are visible when it isn't.
- A partition maintainer creates the current and upcoming months' partitions at startup and daily.
- Retention defaults to **forever**. An operator may set a whole-days age bound on the system page.
  Pruning drops whole monthly partitions that fall entirely outside the bound; it never runs row
  deletes.
- An admin-only export streams a time range of the archive as a capture-format JSONL file, with
  a header saying it came from the archive. `radio/replay.py`, the corpus tooling and any future
  backfill job can read it unchanged. In-process backfill gets a repository method that streams
  a time range in reception order.
- The system page shows archive status: row count, oldest and newest frame, partition count,
  retention setting, written/discarded counters, and the export form.
- `packet_log` is unchanged. It stays the bounded feed and keeps its own pruning.

## Capabilities

### New Capabilities

- `packet-archive`: what is archived and when, append-only semantics, discard-and-count
  durability, monthly partitions and their maintenance, age-bound retention (default forever),
  ordered range reads, and capture-format export.

### Modified Capabilities

- `web-admin`: the system page shows archive status, offers a retention form (forever or N days),
  and offers an admin-only export of a time range.

## Impact

- **Schema**: migration `0016` (after `0015_channel_message_paths`) creates the partitioned parent
  `packet_archive`, its `packet_archive_settings` single row, and the current month's partition.
  The downgrade drops the whole table with every partition, which loses the archive.
- **Code**: `db/models.py`, `db/repositories.py` (`PacketArchiveRepository`), new
  `db/archive.py` (row builders, partition maintainer, pruner), `db/persistence.py` (new
  write-behind lane on `record_rx`/`record_tx`), `radio/capture.py` (shared record builders for
  export), `web/routes/system.py` plus template, status output.
- **Tests**: `test_db_schema.py` drift checks must ignore runtime-created partition children.
  New archive repository, maintainer, retention, export and web tests.
- **Storage**: about 4 600 frames/day at the measured ~191 RX/h, roughly 0.3 GB/year with indexes.
  Kept forever by default, so disk use grows until an operator sets a bound.
- **Docs**: DESIGN.md §6 gains the archive table and explains how it differs from `packet_log`.
  The line in §4.1 about logging every frame now has a durable counterpart.
- Replay runs (`writes_enabled=False`) write nothing to the archive, like every other sink.
