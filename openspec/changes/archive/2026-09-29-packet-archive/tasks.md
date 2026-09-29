# Tasks

## 1. Schema

- [x] 1.1 Add `PacketArchive` (partitioned parent, `postgresql_partition_by="RANGE (at)"`, PK `(at, id)`, `kind` check, `packet_id` index; design D3) and `PacketArchiveSettings` (single row, `retention_days` NULL or 30–3650; design D5) to `src/sighop/db/models.py`; verify `uv run pyright` is clean for the module
- [x] 1.2 Write `alembic/versions/0016_packet_archive.py` (down_revision `0015`): parent table, current UTC month's child `packet_archive_yYYYYmMM`, settings table seeded with `retention_days = NULL`; downgrade drops both, and the docstring says the archive is lost; bump `expected_revision` to `0016` in `src/sighop/db/migrations.py` and `tests/test_db_schema.py`; verify `test_upgrade_downgrade_upgrade_leaves_the_schema_at_head` passes
- [x] 1.3 Extend the model/migration drift check in `tests/test_db_schema.py` with an allowlist entry for `packet_archive` and `packet_archive_settings` (children excluded); verify the drift test passes and fails if a model column is removed

## 2. Repository and maintenance

- [x] 2.1 Add `ArchiveRow`/`ArchiveRecord` and `PacketArchiveRepository` in `src/sighop/db/repositories.py`: `write_many`, keyset `stream(since, until, batch)` raising `ArchiveReadError` on a failed batch (design D6), `summary()` (approx count from `reltuples`, min/max `at`, partition count), `get_retention`/`set_retention`; verify DB tests: rows spanning two months read back oldest-first with identical bytes, a range larger than one batch streams completely, and settings round-trip including NULL
- [x] 2.2 Add `src/sighop/db/archive.py` with `rx_archive_row(record)` (`kind='unparsed'` for `ModemUnparsed`, else `rx`), `tx_archive_row(submission, outcome, at)`, and `ArchiveRecord.as_modem_event()`; verify unit tests: an unparsed record keeps its reason, an undecodable framed record is `rx` with exact bytes, a suppressed TX keeps bytes and `tx_result='suppressed'`, a non-UUID entity id becomes NULL, and `decode_event(record.as_modem_event())` round-trips an archived rx record
- [x] 2.3 Add `ArchiveMaintainer` in `src/sighop/db/archive.py`: `ensure(now)` creates the current and next month's children (`CREATE TABLE IF NOT EXISTS … PARTITION OF`), `prune(now)` detaches and drops children whose upper bound is `<= now - retention_days`, skips unparseable names with a report, and logs `archive_partition_dropped` with month and rows; run it at startup, every 6 h, and on DB recovery, surviving failures (design D4/D5); verify DB tests: ensure is idempotent and creates two months, month-boundary insert lands in next month's child, NULL retention drops nothing, 90-day retention drops a fully expired month but keeps a straddling one, and a failing pass is reported and retried

## 3. Wiring

- [x] 3.1 Add `archive_writer: WriteBehind[ArchiveRow]` (capacity 4096, batch 256, drop_oldest) to `Persistence` in `src/sighop/db/persistence.py`, offer from `record_rx`/`record_tx`, flush counting `archive_written`/`archive_discarded` in `Database.stats` (`src/sighop/db/engine.py`), include it in start/stop, and nothing offered when `writes_enabled=False`; verify persistence tests: RX and TX each produce an archive row, a failed flush increments `archive_discarded`, overflow increments `discarded`, and a replay run writes nothing
- [x] 3.2 Start `ArchiveMaintainer` from `Persistence.start` (ensure awaited before the archive writer starts) and register it on `database.on_recovery`; verify a runtime/persistence test that a fresh database month gets its partition before the first archive write
- [x] 3.3 Add `arch_drop=` (overflow + failed writes) to the status line in `src/sighop/monitor/render.py`/`src/sighop/runtime.py`, always printed; verify a render test shows `arch_drop=0` and a non-zero count

## 4. Export

- [x] 4.1 Move the `rx_frame`/`unparsed`/`tx_frame` dict builders out of `CaptureWriter` into module functions in `src/sighop/radio/capture.py`, used by both the writer and export, and add `archive_meta_record(since, until)` (design D7 header); verify existing capture tests pass unchanged
- [x] 4.2 Add `export_lines(repository, since, until)` async generator in `src/sighop/db/archive.py` yielding header + records (only `transmitted` TX); verify a test that the exported file, read by `CaptureReplay`, yields every archived reception in order with identical bytes and SNR/RSSI, has `source: packet_archive` in its header, omits a suppressed TX, and counts one `transmitted_skipped` for a sent TX

## 5. System page

- [x] 5.1 Add the "Packet archive" section to `/system` (`src/sighop/web/routes/system.py` + template): summary (count labelled approximate, oldest/newest, partitions, written/discarded), retention form (days 30–3650 or keep forever, month-granularity note, blank refused, junk refused) reusing the repeater form's validation helpers, applied without restart; verify web tests for viewing state, saving 365, keep forever, 7 refused, blank refused, `abc` refused
- [x] 5.2 Add `GET /system/archive/export?since=&until=` behind the signed-in guard, streaming `application/x-ndjson` with an attachment filename, refusing inverted/empty ranges with 400; verify web tests: signed-in export of a one-day range returns header + records, anonymous request refused with no data, inverted range refused with reason

## 6. Docs and verification

- [x] 6.1 Update DESIGN.md §6: add `packet_archive` to the table list and explain how it differs from `packet_log` (bytes always, forever by default, discard-and-count, monthly partitions, capture-format export for backfill), and cross-reference §4.1's every-frame rule; verify `grep -n packet_archive DESIGN.md` finds the new text
- [x] 6.2 Run lint and the full test suite (`uv run --locked --env-file .env.dev` ruff/pyright/pytest as the project does) and confirm all pass
- [ ] 6.3 Run the node against the dev database with the modem, confirm `arch_drop=0` in the status line, archive counts rising on `/system`, and an exported hour replaying with the same reception count as the archive holds for that hour
