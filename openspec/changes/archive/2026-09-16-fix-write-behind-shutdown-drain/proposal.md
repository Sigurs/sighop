## Why

`WriteBehind.stop()` cancels the writer task while it is awaiting its sink, and the batch that
task had already taken off the buffer is destroyed with it. `flush_pending` pops a batch out of
`self._items` before awaiting `self._flush(batch)` (`src/sighop/db/writer.py:117-121`), so once
`stop()` cancels the task (`writer.py:159-163`) those rows exist nowhere: not in the deque the
final `flush_pending()` at line 164 drains, not in `written`, not in `failed`. They are lost
*and* uncounted, which is the worse half — `discarded` is `overflowed + failed`, so the status
line reports a clean shutdown while rows went missing.

The shape matches the symptom exactly. `dm_writer` and `channel_writer` batch at 32
(`persistence.py:226`, `persistence.py:238`), so a 33-row replay ending in a stop writes either
1 row (the in-flight 32 were cancelled) or 32 (the trailing 1 was), which is what was observed.
This hits every lane, including the two the design calls costly to lose: direct messages, whose
spec already requires that an unwritten record be counted and a gap stated, and contacts, whose
unpersisted marker is never re-set because neither branch of `_flush_contacts` runs.

## What Changes

- **`stop()` asks the writer to finish instead of killing it.** A stopping flag plus the existing
  wake event lets `run()` leave its loop after draining; `stop()` awaits that, and only cancels
  if a deadline expires.
- **Shutdown gets a bounded budget, shared across the lanes.** Draining without bound is the wrong
  trade here: the `database` capability already requires every operation to be bounded in time,
  and a full buffer at one `operation_timeout` per batch can outlast compose's 20 s
  `stop_grace_period` on its own. `Persistence.stop()` drains its five writers **concurrently**
  under one budget rather than serially under five.
- **A cancelled flush returns its batch to the buffer.** Independently of `stop()`, `flush_pending`
  puts an in-flight batch back at the head of the deque when it is cancelled, so no cancellation
  from any source can vaporise rows that were never written.
- **What the budget could not write is counted and said out loud.** Rows still buffered when the
  deadline expires are counted as `failed` — so `discarded` stays truthful — and logged per lane
  with the count. A shutdown that lost nothing logs nothing new.
- **Bot workers drain their current dispatch.** The sibling audit found one real instance of the
  same shape: `BotWorker.stop()` (`src/sighop/bots/runtime.py:432`) cancels mid-`dispatch`, and a
  dispatch can be holding the bounded `bot_state` write that DESIGN.md calls the sharpest write in
  the system. It gets the same finish-then-stop treatment under the same budget.
- **The other cancel-on-stop siblings are audited and left alone**, with the reasoning recorded in
  design: the packet-log pruner (`db/packetlog.py:162`), the room retention pruner
  (`net/room.py:1850`) and the room push loop (`net/room.py:1418`) each cancel work that is
  idempotent and retried on the next pass, so cancelling them loses nothing durable. The webhook
  dispatcher drops its queue on shutdown *deliberately* and counts what it dropped
  (`webhooks/dispatcher.py:228-239`); that stays as designed.
- **Out of scope:** changing any lane's capacity, batch size or `drop_oldest` flag; making the
  contact recovery flush survive a restart; retrying a failed write.

## Capabilities

### New Capabilities

None. The mechanism exists; this corrects its shutdown contract.

### Modified Capabilities

- `database`: a new requirement that a stopping writer drains what it has buffered under a bounded
  budget, that a write taken from the buffer is never lost without being counted, and that what
  the budget could not write is reported.
- `dm-history`: a scenario under the existing "A record that could not be written is counted and
  reported, not dropped silently" requirement covering shutdown — the one moment at which the
  count was silently wrong.
- `bot-runtime`: a stopping bot worker finishes the dispatch it is running, under a bound, rather
  than being cancelled inside a `bot_state` write.

## Impact

- **Code:** `src/sighop/db/writer.py` (the drain, the flag, the deadline, the counting),
  `src/sighop/db/persistence.py` (concurrent drain under one budget in `stop()`),
  `src/sighop/bots/runtime.py` (`BotWorker.stop`). The `channel_messages` lane inherits the fix
  without a spec delta of its own, because the in-flight `channel-messaging` change owns that
  capability's spec and this change must not collide with it.
- **Shutdown latency:** a healthy stop is unchanged (the drain completes in milliseconds); a stop
  against a degraded database now takes up to the budget instead of returning immediately having
  silently dropped a batch. The budget is chosen to fit inside compose's 20 s grace alongside the
  web server's 5 s and the modem close.
- **Counters:** `discarded` can now be non-zero after a shutdown that previously reported zero.
  That is the bug becoming visible, not a regression.
- **Tests:** the existing drain tests in `tests/test_db_engine.py`, `tests/test_durable_paths.py`,
  `tests/test_packet_log.py` and `tests/test_durable_contacts.py` stand. `tests/test_runtime_channels.py:151`
  waits for `wait_idle()` before stopping, which is what hid the bug from the suite; that wait
  becomes unnecessary and the test should stop directly so it covers the fixed path.
- **Dependencies:** none. No schema change, no migration.
