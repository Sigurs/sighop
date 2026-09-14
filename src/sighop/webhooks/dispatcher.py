"""Delivering events off the reception path (webhook-notifications D3, D4, D10).

Three properties carry the design:

* **The listener only offers.** `on_observation` runs inside the contact store's
  report, so it builds an event, puts it in a bounded queue and returns. It
  never awaits and never raises; a full queue drops the *oldest* pending event,
  counted and logged, because the newest sighting is the likelier to matter.
* **Configuration is read per event.** The enabled webhooks are read from the
  database when an event is taken off the queue, so a change made by
  `sighop webhook …` in another process or by the panel applies to the next
  event without a restart. A failed read falls back to the last good list.
* **One webhook never waits on another.** Each matching webhook gets its own
  task with its own retry schedule; a semaphore caps concurrent HTTP attempts,
  and is held only for an attempt, never across a backoff sleep.

Only a webhook's name and `url_host` ever reach a log event. The URL is opened
for the attempt and goes nowhere else.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from sighop.db.engine import Outcome, Succeeded
from sighop.db.repositories import OpenedWebhook, WebhookRecord
from sighop.logging import Logger, get_logger
from sighop.net.contacts import ContactObservation
from sighop.net.rx import RxRecord
from sighop.webhooks.events import (
    HopLookup,
    WebhookEvent,
    event_from_observation,
    sample_event,
)
from sighop.webhooks.render import CONTENT_TYPE, render
from sighop.webhooks.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    AttemptOutcome,
    AttemptResult,
    post,
)
from sighop.webhooks.triggers import Trigger, trigger_for

QUEUE_CAPACITY = 64
"""Pending events not yet handed to delivery. Events are a few a day once a mesh
is known; this is sized for the first-run burst, not for steady state."""

MAX_CONCURRENT_ATTEMPTS = 4
MAX_IN_FLIGHT_DELIVERIES = 256
"""Deliveries (with their retries) in progress at once. When reached, the queue
stops draining and fills, so an endpoint that is down costs dropped events —
counted — rather than unbounded tasks."""

RETRY_DELAYS_SECONDS: tuple[float, ...] = (2.0, 10.0, 60.0)
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS) + 1

type Transport = Callable[[str, bytes, str, float], AttemptResult]
type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], dt.datetime]


class WebhookSource(Protocol):
    """The part of `WebhookRepository` delivery uses. Faked in tests."""

    async def list_enabled(self, secret: bytes) -> Outcome[list[OpenedWebhook]]: ...

    async def record_delivery(self, webhook_id: uuid.UUID, at: dt.datetime) -> Outcome[bool]: ...

    async def record_failure(
        self, webhook_id: uuid.UUID, at: dt.datetime, reason: str
    ) -> Outcome[bool]: ...


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def matches(record: WebhookRecord, event: WebhookEvent) -> bool:
    """Whether a webhook subscribes to this event and its hop filter lets it pass.

    An unknown hop count passes only a webhook with no limit: a limit is a
    promise that nothing farther away is announced, and unknown might be.
    """
    if event.trigger.value not in record.triggers:
        return False
    if record.max_hops is None:
        return True
    return event.hop_count is not None and event.hop_count <= record.max_hops


def _webhook_fields(record: WebhookRecord) -> dict[str, object]:
    return {"webhook_name": record.name, "url_host": record.url_host}


class WebhookDispatcher:
    def __init__(
        self,
        *,
        repository: WebhookSource,
        secret: bytes,
        logger: Logger | None = None,
        clock: Clock = _utcnow,
        sleep: Sleep = asyncio.sleep,
        transport: Transport = post,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        queue_capacity: int = QUEUE_CAPACITY,
        max_in_flight: int = MAX_IN_FLIGHT_DELIVERIES,
        contacts: HopLookup | None = None,
    ) -> None:
        self._repository = repository
        self._contacts = contacts
        self._secret = secret
        self._log = logger or get_logger(component="webhooks")
        self._clock = clock
        self._sleep = sleep
        self._transport = transport
        self._timeout = timeout
        self._capacity = queue_capacity
        self._max_in_flight = max_in_flight
        self._queue: deque[WebhookEvent] = deque()
        self._wakeup = asyncio.Event()
        self._slot_freed = asyncio.Event()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTEMPTS)
        self._deliveries: set[asyncio.Task[None]] = set()
        self._bookkeeping: set[asyncio.Task[None]] = set()
        self._last_good: list[OpenedWebhook] = []
        self._dispatching = 0
        self.events_raised = 0
        self.delivered = 0
        self.failed = 0
        self.dropped = 0
        self.config_read_failures = 0
        self.outcome_record_failures = 0

    # --- The reception side: offer, never wait ------------------------------

    def on_observation(self, observation: ContactObservation, record: RxRecord) -> None:
        """Contact-store listener. Never awaits, never raises."""
        try:
            trigger = trigger_for(observation)
            if trigger is None:
                return
            event = event_from_observation(
                observation, record, trigger, self._clock(), self._contacts
            )
            self.events_raised += 1
            self._log.info("webhook_event_raised", outcome="success", **event.as_log_fields())
            self.offer(event)
        except Exception as exc:
            self._log.error(
                "webhook_event_failed",
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
            )

    def offer(self, event: WebhookEvent) -> None:
        """Queue an event for delivery, dropping the oldest pending one when full."""
        if len(self._queue) >= self._capacity:
            oldest = self._queue.popleft()
            self.dropped += 1
            self._log.error(
                "webhook_dropped",
                outcome="error",
                reason="queue full",
                dropped_total=self.dropped,
                **oldest.as_log_fields(),
            )
        self._queue.append(event)
        self._wakeup.set()

    @property
    def pending(self) -> int:
        return len(self._queue)

    # --- The delivery side ------------------------------------------------

    async def run(self) -> None:
        """Take events off the queue and deliver them until cancelled."""
        try:
            while True:
                while not self._queue:
                    self._wakeup.clear()
                    await self._wakeup.wait()
                while len(self._deliveries) >= self._max_in_flight:
                    self._slot_freed.clear()
                    await self._slot_freed.wait()
                event = self._queue.popleft()
                await self.dispatch(event)
        finally:
            await self._shutdown()

    async def dispatch(self, event: WebhookEvent) -> None:
        """Start a delivery of one event to each matching webhook."""
        self._dispatching += 1
        try:
            await self._dispatch(event)
        finally:
            self._dispatching -= 1

    async def _dispatch(self, event: WebhookEvent) -> None:
        for opened in await self._read_configuration():
            if opened.url is None or not matches(opened.record, event):
                continue
            task = asyncio.create_task(
                self._deliver(opened.record, opened.url, event),
                name=f"webhook:{opened.record.name}",
            )
            self._deliveries.add(task)
            task.add_done_callback(self._delivery_done)

    def _delivery_done(self, task: asyncio.Task[None]) -> None:
        self._deliveries.discard(task)
        self._slot_freed.set()

    async def wait_idle(self) -> None:
        """Until the queue is drained and every delivery and outcome write ended."""
        while self._queue or self._dispatching or self._deliveries or self._bookkeeping:
            await asyncio.gather(*self._deliveries, *self._bookkeeping, return_exceptions=True)
            await asyncio.sleep(0)

    async def _shutdown(self) -> None:
        pending = len(self._queue)
        if pending:
            self.dropped += pending
            self._queue.clear()
            self._log.info(
                "webhook_dropped",
                outcome="dropped",
                reason="shutdown",
                count=pending,
                dropped_total=self.dropped,
            )
        tasks = [*self._deliveries, *self._bookkeeping]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch(self) -> list[OpenedWebhook] | str:
        """The enabled webhooks, or why they could not be read."""
        try:
            outcome = await self._repository.list_enabled(self._secret)
        except Exception as exc:  # the repository returns faults as values; be sure
            return f"{type(exc).__name__}: {exc}"
        if isinstance(outcome, Succeeded):
            return self._adopt(outcome.value)
        return str(outcome.error)

    async def _read_configuration(self) -> list[OpenedWebhook]:
        fetched = await self._fetch()
        if not isinstance(fetched, str):
            return fetched
        self.config_read_failures += 1
        self._log.error(
            "webhook_config_read_failed",
            outcome="error",
            error=fetched,
            using_last_good=len(self._last_good),
            config_read_failures=self.config_read_failures,
        )
        return self._last_good

    async def startup_summary(self) -> str:
        """What the startup line says, read now — which also primes the last good list."""
        fetched = await self._fetch()
        if isinstance(fetched, str):
            return f"configuration could not be read ({fetched}); each event retries the read"
        usable = [opened for opened in fetched if opened.url is not None]
        summary = enabled_summary(usable)
        unusable = len(fetched) - len(usable)
        if unusable:
            summary += f"; {unusable} unusable — URL cannot be opened under SIGHOP_SECRET_KEY"
        return summary

    def _adopt(self, webhooks: list[OpenedWebhook]) -> list[OpenedWebhook]:
        self._last_good = list(webhooks)
        for opened in self._last_good:
            if opened.url is None:
                self._log.error(
                    "webhook_url_unsealable",
                    outcome="error",
                    error=opened.error,
                    **_webhook_fields(opened.record),
                )
        return self._last_good

    async def _attempt(self, url: str, body: bytes) -> AttemptResult:
        async with self._semaphore:
            return await asyncio.to_thread(self._transport, url, body, CONTENT_TYPE, self._timeout)

    async def _deliver(self, record: WebhookRecord, url: str, event: WebhookEvent) -> None:
        body = render(event, record.format)
        fields = {**_webhook_fields(record), **event.as_log_fields()}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = await self._attempt(url, body)
            if result.delivered:
                self.delivered += 1
                self._log.info(
                    "webhook_delivered",
                    outcome="success",
                    status=result.status,
                    attempts=attempt,
                    duration_ms=result.duration_ms,
                    **fields,
                )
                self._record(record.id, delivered=True, reason=None)
                return
            if result.outcome is AttemptOutcome.FINAL or attempt == MAX_ATTEMPTS:
                self.failed += 1
                reason = result.summary
                if attempt > 1:
                    reason = f"{reason} after {attempt} attempts"
                self._log.error(
                    "webhook_abandoned",
                    outcome="error",
                    status=result.status,
                    error=result.reason,
                    attempts=attempt,
                    failed_total=self.failed,
                    **fields,
                )
                self._record(record.id, delivered=False, reason=reason)
                return
            delay = RETRY_DELAYS_SECONDS[attempt - 1]
            if result.retry_after is not None:
                delay = min(max(delay, result.retry_after), MAX_RETRY_AFTER_SECONDS)
            self._log.info(
                "webhook_attempt_failed",
                outcome="retry",
                status=result.status,
                error=result.reason,
                attempt=attempt,
                next_delay_s=delay,
                duration_ms=result.duration_ms,
                **fields,
            )
            await self._sleep(delay)

    def _record(self, webhook_id: uuid.UUID, *, delivered: bool, reason: str | None) -> None:
        """Write the outcome columns without waiting; a failed write is only logged."""
        at = self._clock()

        async def write() -> None:
            try:
                if delivered:
                    outcome = await self._repository.record_delivery(webhook_id, at)
                else:
                    outcome = await self._repository.record_failure(webhook_id, at, reason or "")
                error = None if isinstance(outcome, Succeeded) else str(outcome.error)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            if error is not None:
                self.outcome_record_failures += 1
                self._log.error(
                    "webhook_outcome_record_failed",
                    outcome="error",
                    webhook_id=str(webhook_id),
                    error=error,
                )

        task = asyncio.create_task(write())
        self._bookkeeping.add(task)
        task.add_done_callback(self._bookkeeping.discard)

    # --- Reporting --------------------------------------------------------

    def status_segment(self) -> str:
        return f"wh ok={self.delivered} fail={self.failed} drop={self.dropped}"

    def as_json(self) -> dict[str, object]:
        return {
            "events_raised": self.events_raised,
            "delivered": self.delivered,
            "failed": self.failed,
            "dropped": self.dropped,
            "pending": self.pending,
            "config_read_failures": self.config_read_failures,
        }


def enabled_summary(webhooks: Iterable[OpenedWebhook | WebhookRecord]) -> str:
    """`2 enabled (new_repeater, new_companion)` for the startup line."""
    records = [item.record if isinstance(item, OpenedWebhook) else item for item in webhooks]
    triggers = [
        trigger.value
        for trigger in Trigger
        if any(trigger.value in record.triggers for record in records)
    ]
    if not records:
        return "0 enabled"
    return f"{len(records)} enabled ({', '.join(triggers)})"


async def send_sample(
    opened: OpenedWebhook,
    trigger: Trigger,
    *,
    logger: Logger | None = None,
    clock: Clock = _utcnow,
    transport: Transport = post,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AttemptResult:
    """One attempt with a sample event, no retry (design D8).

    Ignores enabled and the hop filter: a test is how an operator checks a
    webhook before turning it on.
    """
    log = logger or get_logger(component="webhooks")
    record = opened.record
    event = sample_event(trigger, clock())
    if opened.url is None:
        result = AttemptResult(
            AttemptOutcome.FINAL, reason=f"the stored URL cannot be opened: {opened.error}"
        )
    else:
        body = render(event, record.format)
        result = await asyncio.to_thread(transport, opened.url, body, CONTENT_TYPE, timeout)
    (log.info if result.delivered else log.error)(
        "webhook_test_sent",
        outcome="success" if result.delivered else "error",
        status=result.status,
        error=result.reason or None,
        duration_ms=result.duration_ms,
        **_webhook_fields(record),
        **event.as_log_fields(),
    )
    return result
