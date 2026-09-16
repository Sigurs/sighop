"""The bounded write-behind worker (design D2, D16).

One mechanism, three users, and the differences between them are two flags:

* `packet_log` **drops oldest**: 555 receptions in 2 h 54 min measured live —
  the highest volume and the lowest value per row.
* `path` **drops oldest**: a route is relearned from the next reception, so a
  dropped write costs a counter and nothing else.
* `contact` **refuses**: re-acquiring one means waiting for the peer to advert
  again, and the advert floor is 24 h (design D15).

The contact queue's refusal is not a lossy path in disguise: `offer` returning
False is what leaves the contact's unpersisted marker set, and the marker is what
the recovery flush writes. Async does not become lossy; the overflow path
degrades into the mechanism that already exists.

`offer` never awaits and never raises. That is the whole point — it is called
from the reception path, whose defining property is that replaying a capture
reproduces every reception exactly, and a database that is slow, unreachable or
blackholing must not be able to add a millisecond to it.

What `stop()` guarantees: the writer is asked to finish and to drain what it has
buffered, under a caller-supplied deadline shared with the other lanes, and every
row taken off the buffer ends up written, counted as failed, or back in the
buffer — never in none of the three. What it does not guarantee: durability. A
row still buffered when the deadline expires is counted and reported, not
written, and a process killed outright never reaches `stop()` at all. That is why
contacts are written promptly rather than at shutdown (design D2).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

from sighop.logging import Logger, get_logger

DEFAULT_CAPACITY = 1024
DEFAULT_BATCH_SIZE = 64

Flush = Callable[[list[object]], Awaitable[bool]]


class WriteBehind[T]:
    """A bounded buffer drained by one writer task, batching as it goes."""

    def __init__(
        self,
        name: str,
        flush: Callable[[list[T]], Awaitable[bool]],
        *,
        capacity: int = DEFAULT_CAPACITY,
        batch_size: int = DEFAULT_BATCH_SIZE,
        drop_oldest: bool = True,
        logger: Logger | None = None,
    ) -> None:
        if capacity <= 0 or batch_size <= 0:
            raise ValueError("capacity and batch_size must be positive")
        self.name = name
        self.capacity = capacity
        self.batch_size = batch_size
        self.drop_oldest = drop_oldest
        self._flush = flush
        self._log = logger or get_logger(component="db-writer")
        self._items: deque[T] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False

        self.written = 0
        self.overflowed = 0
        """Rows dropped because the buffer was full. Zero, not absent, when none."""

        self.failed = 0
        """Rows whose write did not land. A gap in a feed, counted as one."""

        self.refused = 0
        """Offers a refusing buffer turned away. Zero for a `drop_oldest` one.

        Counted separately from `discarded` rather than added to it, because
        what a refusal costs depends on who was refused. A refused *contact* is
        not lost — the caller keeps its unpersisted marker set and the recovery
        flush writes it (design D16) — so counting it as "will never reach the
        database" would be a lie the status line tells. A refused direct message
        has no such second chance, which is exactly why the count is here to be
        read rather than folded into another number (milestone 8 design D8)."""

    # --- Offering ----------------------------------------------------------

    def offer(self, item: T) -> bool:
        """Queue one row. Never awaits. False when the buffer refused it.

        A `drop_oldest` buffer always accepts and reports what it displaced; a
        refusing one returns False and leaves the caller holding the item, which
        is what the contact writer's unpersisted marker is for.
        """
        if len(self._items) >= self.capacity:
            if not self.drop_oldest:
                self.refused += 1
                self._wake.set()
                return False
            self._items.popleft()
            self.overflowed += 1
        self._items.append(item)
        self._idle.clear()
        self._wake.set()
        return True

    @property
    def pending(self) -> int:
        return len(self._items)

    @property
    def discarded(self) -> int:
        """Rows that will never reach the database: displaced, or failed to write."""
        return self.overflowed + self.failed

    # --- Draining ----------------------------------------------------------

    async def flush_pending(self) -> None:
        """Write everything buffered, in batches, containing every failure."""
        while self._items:
            size = min(self.batch_size, len(self._items))
            batch = [self._items.popleft() for _ in range(size)]
            try:
                landed = await self._flush(batch)
            except asyncio.CancelledError:
                # The rows exist only in `batch` while the sink is awaited, so a
                # cancellation here would destroy them: put them back at the head,
                # in order, and let the cancellation continue (design D2).
                self._items.extendleft(reversed(batch))
                self._idle.clear()
                raise
            except Exception as exc:
                # The sink is meant to return an outcome rather than raise; one
                # that raises anyway must not take the writer task down with it.
                self._log.error(
                    "write_behind_flush_raised",
                    outcome="error",
                    writer=self.name,
                    rows=len(batch),
                    error=repr(exc),
                )
                landed = False
            if landed:
                self.written += len(batch)
            else:
                self.failed += len(batch)
        self._idle.set()

    async def run(self) -> None:
        """The writer task: wake, drain, sleep. Ends when asked to stop."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self.flush_pending()
            if self._stopping and not self._items:
                return

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self.run(), name=f"db-writer-{self.name}")

    async def stop(self, deadline: float | None = None) -> None:
        """Ask the writer to finish, and wait for it until `deadline`.

        `deadline` is an absolute event-loop instant (`loop.time()`), not a
        duration, so several writers stopped together share one budget rather
        than taking one each (design D3). Without one the drain is unbounded.

        The writer is asked to drain rather than cancelled where it stands, so
        the batch it had already taken off the buffer is written. What the
        deadline leaves unwritten is counted as `failed` and reported, so the
        discarded totals stay truthful (design D5).

        Flush-on-shutdown is still best effort by construction: a process killed
        outright never reaches here, which is exactly why contacts are written
        promptly rather than at shutdown (design D2).
        """
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                # On timeout `wait_for` cancels the task and waits for it, which
                # is where the in-flight batch returns to the buffer (design D2).
                await asyncio.wait_for(task, timeout)
            except TimeoutError:
                self._abandon_buffered()
            except asyncio.CancelledError:
                self._abandon_buffered()
                raise
        else:
            # A writer that was never started still holds whatever was offered.
            await self.flush_pending()

    def _abandon_buffered(self) -> None:
        """Count what the budget could not write, and say so once (design D5)."""
        rows = len(self._items)
        if not rows:
            return
        self._items.clear()
        self.failed += rows
        self._idle.set()
        self._log.error(
            "write_behind_shutdown_incomplete",
            outcome="error",
            writer=self.name,
            rows=rows,
        )

    async def wait_idle(self) -> None:
        """Block until the buffer has drained. For tests and for shutdown."""
        await self._idle.wait()

    def as_json(self) -> dict[str, object]:
        return {
            f"{self.name}_written": self.written,
            f"{self.name}_discarded": self.discarded,
            f"{self.name}_refused": self.refused,
            f"{self.name}_pending": self.pending,
        }
