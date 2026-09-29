# Design

## Context

`packet_log` (DESIGN.md §6) is a best-effort feed capped at 100 000 rows, and it keeps `raw` only
for frames that failed to decode. `db/packetlog.py`'s `rx_row` says why: keeping every frame's
bytes "would make the feed a capture file, which is a different artefact with a different lifetime
(§12)". This change adds that artefact, but inside the database and permanent.

What already exists:

- `Runtime._consume` → `decode_event` → `pipeline.ingest` → `Persistence.record_rx(record)`.
  `RxRecord` carries `raw`, `received_at`, `snr_db`, `rssi_dbm`, `packet_id` and an outcome. That
  covers KISS-unparsed bytes (`ModemUnparsed`) as well as framed receptions.
- `Runtime._record_tx` → `Persistence.record_tx(submission, outcome, at)`. `Submission.packet`
  holds the bytes, and `TxResult` is one of `transmitted | failed | suppressed | dropped`.
- `WriteBehind[T]` is the bounded write lane with discard counters. `Database.run` gives each
  operation its bound (statement_timeout 5 s) and never raises.
- Capture JSONL (`radio/capture.py`, `radio/replay.py`) uses the kinds `capture_meta`,
  `rx_frame`, `unparsed` and `tx_frame`. Replay skips `tx_frame`.
- The repeater collection settings are the precedent for a single-row settings table with nullable
  `retention_days` meaning forever (migrations 0011 and 0012).
- The database is external, Postgres 17+, and the app role runs migrations on start, so it holds
  DDL rights.
- Measured rate is ~191 RX/h, about 4 600 records/day.

Operator decisions (2026-09-29): discard-and-count when the database is behind, with no disk
spool. Retention forever by default with an optional age bound. Monthly range partitions.

## Goals / Non-Goals

**Goals**
- Every reception and transmission outcome is stored with its wire bytes whenever the database is
  healthy, and every gap is counted.
- A future feature can backfill by reading a time range in order and running the bytes through the
  current decoder.
- The archive can be exported in the format the replay and corpus tooling already read.
- Retention costs nothing on the hot path, and removing old data is cheap.

**Non-Goals**
- Durability through database outages (no disk spool; operator chose discard-and-count).
- Storing decoded fields. The bytes are the source of truth, so old frames are always decoded by
  the current decoder.
- A generic backfill framework or job runner. This change delivers the ordered read and the
  export, and each future feature writes its own backfill against them.
- Migrating `packet_log` history into the archive. It holds no bytes for decoded frames.
- Search or browse UI for the archive.

## Decisions

### D1 — A separate table, not a wider `packet_log`
`packet_log` stays a pruned feed with its own contract ("nothing may depend on a row"). The archive
has the opposite lifetime and a different shape: bytes always, decoded fields never. Merging them
would force either a feed that grows forever or an archive pruned by row count. Rows in both carry
the same `packet_id`, so they can be joined.

### D2 — Its own write-behind lane, fed from the same two hooks
`Persistence.record_rx` and `record_tx` each offer a second item, an `ArchiveRow`, to a new
`archive_writer: WriteBehind[ArchiveRow]` (capacity 4096, batch 256, `drop_oldest=True` like the
packet log). Its own lane means a slow archive insert can't starve the feed and the reverse. The
cost to the hot path is one dataclass build and one `offer`. The flush function counts
`archive_written` and `archive_discarded` in `Database.stats`. Queue overflow is counted by
`WriteBehind.discarded`, and the status line reports the sum as `arch_drop=`. When
`writes_enabled=False` (replay), nothing is offered.

*Alternative rejected:* a disk spool for outages. The operator chose simplicity. The counters make
the gaps visible, and a DESIGN.md note records that the archive is complete only while the
database is healthy.

### D3 — Row shape
```
packet_archive (partitioned BY RANGE (at))
  id             bigint GENERATED ALWAYS AS IDENTITY
  at             timestamptz NOT NULL      -- RxRecord.received_at / TX resolve time
  kind           text NOT NULL CHECK (kind IN ('rx','unparsed','tx'))
  packet_id      text NOT NULL
  raw            bytea NOT NULL
  snr_db         real NULL                 -- rx only
  rssi_dbm       smallint NULL             -- rx only
  reason         text NULL                 -- unparsed: modem's reason; tx: outcome reason
  tx_result      text NULL                 -- tx only: TxResult value
  airtime_ms     real NULL
  entity_id      uuid NULL                 -- tx only; no FK: the archive outlives entities
  priority_class smallint NULL             -- tx only
  PRIMARY KEY (at, id)
  INDEX (packet_id)
```
- `kind='unparsed'` records bytes the modem couldn't frame. A framed frame that fails to decode
  is `rx`, because its bytes are the frame and re-decoding may succeed with a later decoder.
- The PK must include the partition key, so it is `(at, id)`, which is also the read order (D6).
  The per-partition btree on `(at, id)` serves range scans, so no BRIN is needed.
- No FK to `entity`. Deleting an identity must not rewrite or block history.

### D4 — Monthly partitions created by the runtime, no DEFAULT partition
Children are named `packet_archive_yYYYYmMM` and cover `[first of month 00:00 UTC, first of next
month)`. `ArchivePartitions.ensure(now)` runs `CREATE TABLE IF NOT EXISTS … PARTITION OF
packet_archive FOR VALUES FROM (…) TO (…)` for the current and next month. It runs at startup
before the archive writer starts, every 6 h after that, and from `Database.on_recovery`. Migration
0016 creates the current month's partition so a freshly migrated database accepts rows at once.

No DEFAULT partition. Once a DEFAULT holds rows for a month, creating that month's partition fails
until the rows are moved out. That would trade a counted gap for a manual repair. Without one, a
missing partition makes the insert fail: the batch is discarded and counted (spec), and the next
`ensure` fixes it. Creating one month ahead means this only happens if maintenance has failed for
a whole month.

`CREATE TABLE … PARTITION OF` briefly locks the parent. This happens a few times a day and
concurrent inserts wait milliseconds, well within the statement bound.

### D5 — Retention: single-row settings, whole-partition drops
`packet_archive_settings (id smallint PK CHECK (id=1), retention_days int NULL CHECK
(retention_days IS NULL OR retention_days BETWEEN 30 AND 3650))`. Migration 0016 seeds it with
NULL, meaning forever. The 30-day floor exists because removal is month-grained, so a shorter bound
would promise more than it delivers.

`ArchiveRetention.prune(now)` runs every 6 h alongside `ensure`. It reads the setting and returns
immediately when it is NULL. Otherwise it lists children through `pg_inherits` and drops each one
whose upper bound is `<= now - retention_days` (`ALTER TABLE … DETACH PARTITION` and then `DROP
TABLE`, each under `Database.run`). The upper bound is parsed from the child name, and names that
don't match are skipped and reported. Each drop logs `archive_partition_dropped` with the month and
row count. The setting is re-read every pass, so a web change takes effect without a restart.

*Alternative rejected:* row deletes by age. They cost vacuum and bloat, and whole-partition drops
are the reason partitioning was chosen.

### D6 — Ordered range read by keyset
`PacketArchiveRepository.stream(since, until, batch=1000) -> AsyncIterator[ArchiveRecord]` uses
keyset pagination on `(at, id) > (last_at, last_id)`. Each batch is a separate `Database.run`, so
every statement stays under the timeout and memory stays bounded. A failed batch raises
`ArchiveReadError` to the caller. The live path never calls this, so raising here is acceptable,
unlike in `Database.run` users on the reception path. A backfill wants to fail loudly rather than
silently skip a batch.

Records come back as `ArchiveRecord` holding the stored fields. `ArchiveRecord.as_modem_event()`
turns an rx or unparsed record into the `RxEvent` or `UnparsedEvent` that `decode_event` takes. A
backfill therefore reuses the exact live decode path.

### D7 — Export is capture JSONL, sharing record builders with `CaptureWriter`
The `rx_frame`, `unparsed` and `tx_frame` dict builders move out of `CaptureWriter` into
module-level functions in `radio/capture.py`, and both the writer and the export call them. That
keeps one format with one implementation, as §12 requires. Export header:
```json
{"ts": "<export time>", "kind": "capture_meta", "source": "packet_archive",
 "range": {"since": "...", "until": "..."}, "sighop": {"version": "...", "commit_hash": "..."},
 "probe": null, "probe_absent_reason": "exported from the packet archive; no board was probed"}
```
`tx_frame` is written only for `tx_result='transmitted'`. Replay already skips `tx_frame`, so
replaying an export reproduces the receptions exactly.

Web: `GET /system/archive/export?since=&until=` behind the existing signed-in guard. It returns a
`StreamingResponse` (`application/x-ndjson`, `Content-Disposition: attachment;
filename=sighop-archive-<since>-<until>.jsonl`) fed by `stream()`. If the read fails partway, the
stream ends with no trailing garbage and the failure is logged. A truncated file is still valid
JSONL up to its last line, and replay already reports a torn final line. Form inputs are UTC dates
where `until` is exclusive, and an inverted or empty range is refused with a 400 and the reason.

### D8 — System page and settings
There is a new "Packet archive" section on `/system`. It shows the record count (the
`pg_class.reltuples` sum over children, labelled approximate, because `count(*)` over years would
be slow), the oldest and newest `at` (min/max through the PK), the partition count, this run's
written and discarded counts, and a retention form (days or keep forever) that follows the
repeater form's validation helpers and wording. The export form sits below it.

## Risks / Trade-offs

- **Disk grows without limit by default.** About 0.3 GB/year at the measured rate, more on a busy
  mesh. → The system page shows the counts, the operator can set a bound, and DESIGN.md states it.
- **An outage loses records.** → Accepted (operator decision). The losses are counted and shown,
  so a backfill can tell whether its range was complete.
- **Runtime DDL.** The app role must keep CREATE rights on its schema. → The same role already runs
  migrations. If `ensure` fails, it logs and retries without crashing.
- **Schema drift test.** Runtime-created children would show up as unknown tables. → The drift
  test's allowlist names only `packet_archive` and `packet_archive_settings`. A dedicated test
  asserts that children are created and dropped by the maintainer.
- **`received_at` comes from the host clock.** If the clock steps back across a month boundary,
  rows land in the previous month's partition, which still exists. That's harmless.
- **The feed and the archive can disagree** during overflow, because they are independent lanes.
  → Accepted. Each has its own counter.

## Migration Plan

Migration `0016_packet_archive` (down_revision `0015`) does three things. It creates the
partitioned parent with PK and index, creates the current month's child (computed at migration
time, UTC), and creates and seeds `packet_archive_settings`. The downgrade drops the parent, which
cascades to every child, and the settings table, and its docstring states that this loses the
archive. The archive starts empty and nothing is backfilled.

## Open Questions

None blocking. Once the next month's partition exists, whether to tune the 6 h maintenance
interval can be decided from what the logs show.
