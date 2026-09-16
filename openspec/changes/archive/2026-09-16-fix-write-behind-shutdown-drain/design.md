## Context

See proposal.md — Why for the defect and how it produced the observed counts.

Four facts about the current code shape the approach:

- `WriteBehind.run()` is an unconditional `while True` (`writer.py:141-146`). It has no way to
  end other than cancellation, which is why `stop()` cancels.
- `flush_pending` takes a batch off `self._items` *before* awaiting the sink
  (`writer.py:117-121`). Between the `popleft` and the counter update the rows exist only in a
  local variable, and cancellation there destroys them.
- A single flush is already bounded: `Database.operation_timeout` is `connect_timeout +
  statement_timeout`, 10 s by default (`engine.py:288-295`, `config.py:73-75`). So the risk is
  not one unbounded await, it is the number of batches a stop might have to wait through — a full
  1024-row buffer at 128 per batch is eight of them.
- `Persistence.stop()` drains its five writers **serially** (`persistence.py:371-380`), so any
  per-writer bound would multiply by five.

The deployment constraint is `stop_grace_period: 20s` in `compose.yaml:46`, which already covers
the web server's 5 s graceful shutdown and the modem close. Milestone 9 recorded a connection
holding shutdown to that limit as a defect, not as acceptable behaviour — so a drain that can
outlast the grace period is not an option here.

The runtime's existing shutdown order matters and is already correct for this change
(`runtime.py:569-591`): the scheduler stops first — draining its queues and resolving every
pending packet with `DropReason.SHUTDOWN` — then rooms, then bots, then the bus, and persistence
last, "after the bus, so nothing is still producing rows".

## Goals / Non-Goals

**Goals:**

- No row that was taken off a buffer can end up in neither `written` nor `failed`.
- A stop against a healthy database writes everything and costs milliseconds.
- A stop against a sick database costs a known, bounded amount of time and tells the truth about
  what it could not write.
- The bound does not scale with the number of write lanes.

**Non-Goals:**

- Durability across a `SIGKILL`. Flush-on-shutdown stays best effort by construction; the design's
  answer to that has always been that contacts are written promptly rather than at shutdown.
- Retrying a failed write, or persisting the buffer across restarts.
- Touching any lane's capacity, batch size or `drop_oldest` flag — this change fixes when rows are
  lost, not which lanes may lose them.
- Changing the webhook dispatcher, which drops its queue at shutdown deliberately and counts it.

## Decisions

### D1: The writer ends its own loop; cancellation becomes the fallback, not the mechanism

`stop()` sets a `_stopping` flag and sets `_wake`. `run()` gains an exit condition: after each
`flush_pending()`, if `_stopping` is set and `_items` is empty, return. `stop()` then awaits the
task under a deadline and only cancels if the deadline expires. `start()` clears `_stopping`, so a
stopped writer can be started again without immediately exiting.

*Alternative considered — keep the cancel, and have `stop()`'s trailing `flush_pending()` pick up
the pieces.* This is what the code intends today and it cannot work: the in-flight batch is not in
the deque for the trailing flush to find. Fixing that means D2 anyway, and once D2 exists, letting
the loop end cleanly is strictly better than cancelling and repairing.

*Alternative considered — a sentinel item pushed onto the buffer.* Works for a queue, but this is a
deque plus an event, and a sentinel would have to be typed into `deque[T]`. A flag is smaller.

### D2: A cancelled flush returns its batch to the head of the buffer

Independently of `stop()`, `flush_pending` wraps the await so that on `CancelledError` it puts the
batch back with `extendleft(reversed(batch))` — head, in original order — clears `_idle`, and
re-raises. This is the actual invariant the spec asks for: a batch is in the deque, or counted, and
never neither. It holds for any cancellation, not just the one `stop()` issues, which matters
because the runtime cancels a lot of tasks during shutdown.

The return may push the deque transiently over `capacity`. That is accepted: the overage is bounded
by one batch, the next `offer` trims it through the existing overflow path, and refusing to return
rows in order to respect a capacity bound would reintroduce the loss this change exists to remove.

### D3: One deadline, passed in, shared across the lanes

`WriteBehind.stop()` takes an optional **absolute deadline** (an `asyncio` event-loop instant),
not a duration. `Persistence.stop()` computes one deadline and drains its five writers
concurrently with `asyncio.gather`, each under that same instant.

An absolute deadline rather than a duration is what makes the budget shared rather than per-lane:
a duration passed to five serial calls is five budgets, and a duration passed to five concurrent
calls is still five clocks that started at slightly different times. The gather is what keeps the
stop from taking five times as long as it needs to; the lanes write to different tables and do not
contend beyond the pool.

*Alternative considered — keep the serial drain and give each lane a fifth of the budget.* Wrong
shape: it gives the contacts lane a fifth of a budget even when it is empty and the packet log is
full, and it makes the per-lane bound depend on how many lanes exist.

### D4: The budget is 5 s for persistence and 2 s for bots, and it is configurable

`shutdown_budget` joins the other bounds on the database configuration (default `5.0`, beside
`connect_timeout`, `statement_timeout` and `pool_timeout` in `config.py`), so an operator who
knows their database is slow can raise it.

The default comes from the grace period, not from taste: 20 s of grace, minus the web server's 5 s,
minus the bot drain's 2 s, minus the modem close, leaves 5 s for the writers with margin to spare.
Note it is deliberately *below* one `operation_timeout` (10 s), so a genuinely hung database
cancels its in-flight batch rather than being waited out — the batch returns to the buffer under D2
and is counted as failed under D5, which is the honest outcome.

### D5: What the budget could not write is counted as `failed` and logged once per lane

When the deadline expires, `stop()` cancels the task, and whatever remains in `_items` (including
the batch D2 just returned) is added to `failed` and the deque cleared. `failed` is already half of
`discarded`, so the status line and `as_json()` become truthful with no new field. One error event
per lane that lost rows — naming the writer and the count — and nothing at all from a lane that
drained cleanly.

*Alternative considered — a new `lost_at_shutdown` counter.* Rejected: the dm-history and
packet-log specs both already require discarded writes to be counted and reported together, and a
second counter that the status line does not add to `discarded` would recreate the present bug in a
more legible form.

### D6: Bot workers drain their current dispatch under their own, smaller budget

`BotWorker.stop()` gets the same treatment — finish the dispatch in progress, do not start queued
ones, cancel at the deadline — because a dispatch can be inside the bounded `bot_state` write that
DESIGN.md calls the sharpest write in the system, and the greeter needs that write to have landed
before it transmits.

This is safe at its current position in the shutdown order precisely because the scheduler has
already stopped: `runtime.py:571` runs `scheduler.stop()` before `bots.stop()`, and the scheduler's
stop drains its queues and resolves every pending packet with `DropReason.SHUTDOWN`. A dispatch
waiting on a send therefore gets an answer rather than hanging, and the 2 s budget is a backstop for
a driver that is slow for its own reasons. The comment at `runtime.py:581-583` — that cancelling a
bot mid-dispatch "resolves that send rather than abandoning it" — needs updating: the scheduler's
stop is what resolves the send, and the cancel was costing the state write.

### D7: The other cancel-on-stop siblings are left alone, on the record

Audited and deliberately unchanged, because each cancels work that is idempotent and retried rather
than work that is lost:

- **Packet-log pruner** (`db/packetlog.py:156-166`) — a cancelled `DELETE` leaves rows to be pruned
  on the next pass. The buffer is a ring by design.
- **Room retention pruner** (`net/room.py:1846-1856`) — same shape, same reasoning.
- **Room push loop** (`net/room.py:1418-1424`) — a cancelled `push_once` loses at most one sync
  push, which the next pass repeats; the loop exists to converge, not to deliver once.
- **Webhook dispatcher** (`webhooks/dispatcher.py:228-244`) — drops its queue at shutdown *on
  purpose*, counts the drop into `dropped` and logs `webhook_dropped` with `reason="shutdown"`.
  That is the designed behaviour for an at-most-once outbound notification, not an instance of this
  bug.

## Risks / Trade-offs

- **A stop against a degraded database now takes up to 5 s instead of returning at once.** →
  That is the fix, not a regression: it previously returned fast by discarding a batch silently.
  The budget is sized against the 20 s grace with the web and bot drains subtracted, and it is
  configurable for an operator whose database is reliably slower.
- **Shutdown is now the sum of three budgets (web 5 s, bots 2 s, writers 5 s) plus the modem
  close.** → ~12–13 s against 20 s of grace. Verify the arithmetic in the compose run rather than
  trusting it; if the margin proves thin, the writers' budget is the configurable one.
- **The buffer can transiently exceed `capacity` by one batch when a flush is cancelled.** →
  Bounded and short-lived; the next `offer` trims it through the path that already exists.
- **`discarded` becomes non-zero after shutdowns that used to report zero.** → Intended. Anyone
  reading those counters as a health signal should be told that the zero was previously a lie.
- **Draining a bot dispatch could hold the stop if a driver awaits something the scheduler's stop
  does not resolve.** → The 2 s budget bounds it, and the cut-short case is reported with the bot's
  name so a badly behaved driver is identifiable rather than merely slow.
- **A test that stops immediately after offering now observes the rows written.** → Several tests
  currently work around the bug by awaiting `wait_idle()` first; those waits become unnecessary and
  at least one should be removed so the fixed path is what the suite exercises.

## Migration Plan

No schema change, no migration, no configuration required. `shutdown_budget` takes its default when
unset. Deploy is an ordinary restart; rollback is a revert, after which shutdown silently drops
batches again. Nothing persisted by this change needs undoing.
