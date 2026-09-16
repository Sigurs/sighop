## 1. A failing test first

- [ ] 1.1 Add `tests/test_db_engine.py::test_a_stop_does_not_lose_the_batch_it_was_writing`: a sink
      that awaits an event before returning, 33 items offered to a writer with `batch_size=32`,
      `start()`, let the writer take its first batch, then `await writer.stop()` concurrently with
      releasing the sink. Verify it fails today with `written == 1` and `discarded == 0` — the
      exact shape of the replay observation — and that it will pass at `written == 33`.
- [ ] 1.2 Add `tests/test_db_engine.py::test_a_cancelled_flush_returns_its_batch`: cancel the
      writer task directly (not through `stop()`) while a batch is in flight, and verify
      `pending` accounts for the batch rather than it vanishing from `pending`, `written` and
      `failed` alike. Fails today.
- [ ] 1.3 Run `uv run --locked pytest -q tests/test_db_engine.py` and confirm both new tests fail
      for the stated reason and nothing else regressed.

## 2. The writer drains instead of being cancelled

- [ ] 2.1 In `src/sighop/db/writer.py`, add a `_stopping` flag; give `run()` an exit condition
      (after `flush_pending()`, return when `_stopping` is set and `_items` is empty) and have
      `start()` clear `_stopping` so a restarted writer runs. Verify the existing
      `tests/test_db_engine.py` drain tests still pass.
- [ ] 2.2 In `flush_pending`, catch `CancelledError` around the sink await, return the batch to the
      head of the deque with `extendleft(reversed(batch))`, clear `_idle`, and re-raise. Keep the
      existing `Exception` branch that counts a raising sink as failed. Verify 1.2 now passes.
- [ ] 2.3 Rewrite `stop()` to take an optional absolute event-loop deadline: set `_stopping`, set
      `_wake`, await the task under `asyncio.wait_for` against the deadline, and cancel only on
      timeout. Keep the trailing `flush_pending()` for a writer that was never started, and keep
      `stop()` safe to call twice. Verify 1.1 now passes.
- [ ] 2.4 On deadline expiry, add whatever remains in `_items` (including a batch returned by 2.2)
      to `failed`, clear the deque, and log one `write_behind_shutdown_incomplete` error event with
      `writer` and `rows`. Verify a new test with a sink that never returns: `stop()` completes
      within the deadline, `failed` equals the rows offered, `discarded` is non-zero, and the event
      is logged once.
- [ ] 2.5 Update the module docstring to say what `stop()` now guarantees and what it does not —
      a drain under a bound, still best effort against a `SIGKILL`.

## 3. One budget across the lanes

- [ ] 3.1 Add `shutdown_budget: float = DEFAULT_SHUTDOWN_BUDGET` (`5.0`) to the database
      configuration in `src/sighop/config.py`, beside the existing bounds, with the grace-period
      arithmetic from design D4 as its comment. Verify the existing configuration tests pass and
      the value round-trips from the environment the way the neighbouring bounds do.
- [ ] 3.2 Change `Persistence.stop()` in `src/sighop/db/persistence.py` to compute one deadline
      from `shutdown_budget` and drain the five writers concurrently under it with
      `asyncio.gather`, keeping the pruner stop before them and `database.dispose()` after.
      Verify with a test that stops a persistence whose five sinks each block: the whole stop
      completes within roughly one budget, not five.
- [ ] 3.3 Verify against a real database that a stop with rows buffered in more than one lane
      writes them all: `uv run --locked pytest -q -m database` with `SIGHOP_TEST_DATABASE_URL`
      set, and confirm the database tests run rather than skip.

## 4. Bot workers finish the dispatch they are running

- [ ] 4.1 Give `BotWorker.stop()` in `src/sighop/bots/runtime.py` the same shape under a 2 s
      budget: let the in-progress `dispatch` finish, do not start queued ones, cancel at the
      deadline and report the cut-short dispatch with the bot's name. Verify a new test in
      `tests/test_runtime_bots.py`: a driver that writes bot state and then yields, stopped
      mid-dispatch, has its state write land.
- [ ] 4.2 Add the counterpart test: a driver that never returns is cut short at the budget, the
      stop completes, and the event names the bot. Verify
      `tests/test_runtime_bots.py::test_bot_workers_start_and_stop_with_the_run` still passes.
- [ ] 4.3 Correct the comment at `src/sighop/runtime.py:581-583` — the scheduler's stop is what
      resolves a pending send, not the bot cancel — and confirm the shutdown order is unchanged
      (scheduler, rooms, bots, bus, persistence).

## 5. Remove the workarounds the bug forced

- [ ] 5.1 Drop the `await persistence.channel_writer.wait_idle()` at
      `tests/test_runtime_channels.py:151` so the test stops directly and covers the fixed path.
      Verify it passes without the wait.
- [ ] 5.2 Sweep `tests/` for the other pre-stop `wait_idle()` calls found in
      `tests/test_durable_contacts.py` and remove any that exist only to dodge this bug, keeping
      the ones that genuinely assert draining. Verify the full suite after each removal.

## 6. Verify the whole thing

- [ ] 6.1 Run `./build.sh lint`, `./build.sh types` and `./build.sh test` and confirm all three
      pass clean.
- [ ] 6.2 Replay a capture to the end and confirm the row count written equals the row count
      offered — the observation that opened this change. Use the replay invocation from
      `build.sh:118` against a configured database and compare the final status line's written and
      discarded counters against the capture's reception count.
- [ ] 6.3 Time a `docker compose stop` against a reachable database and against an unreachable one;
      confirm both exit 0 inside `stop_grace_period: 20s`, and that the unreachable case reports
      the rows it could not write rather than reporting nothing.
- [ ] 6.4 Run `openspec validate fix-write-behind-shutdown-drain --strict` and confirm the three
      spec deltas parse and every requirement carries a scenario.
